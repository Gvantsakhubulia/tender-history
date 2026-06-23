"""
Historical scraper for etenders.ge (private procurement portal).

Flow:
  1. Fetch listing pages from /tenders?pg=N (20 tenders per page).
  2. For each tender ID not already in the known_ids set, fetch the detail page.
  3. Stop when an empty page is returned or max_pages is reached.

No is_seen / seen_tenders.json dependency — designed for full historical scraping.
"""

import re
import time
import logging
import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

BASE_URL = "https://etenders.ge"
LIST_URL = BASE_URL + "/tenders"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ka,en-US;q=0.9,en;q=0.8",
}


def _make_session() -> requests.Session:
    session = requests.Session()
    session.verify = False
    session.headers.update(HEADERS)
    session.get(BASE_URL + "/", timeout=30)
    return session


def _parse_date(text: str) -> str:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


def _parse_listing_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen_ids: set[str] = set()

    for img in soup.select("img[src*='LogoHandler.aspx?tenderid=']"):
        m = re.search(r"tenderid=(\d+)", img.get("src", ""))
        if not m:
            continue
        tender_id = m.group(1)
        if tender_id in seen_ids:
            continue
        seen_ids.add(tender_id)

        detail_url = BASE_URL + f"/view/{tender_id}/"
        container = img.find_parent("td")
        if container:
            for a in container.find_all("a", href=re.compile(rf"/view/{tender_id}/")):
                detail_url = BASE_URL + a.get("href")
                break

        items.append({"id": tender_id, "url": detail_url})

    return items


def _parse_detail(html: str, tender_id: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    tender: dict = {
        "id":                f"etge_{tender_id}",
        "source":            "etenders_ge",
        "url":               url,
        "number":            tender_id,
        "title":             "",
        "purchaser":         "",
        "tender_type":       "",
        "status":            "",
        "announced_date":    "",
        "deadline":          "",
        "budget":            "",
        "category":          "",
        "cpv_codes":         [],
        "description":       "",
        "object_name":       "",
        "object_description": "",
        "doc_groups":        {},
        "file_urls":         [],
        "delivery_period":   "",
    }

    meta_desc = soup.find("meta", {"name": "description"})
    if meta_desc and meta_desc.get("content"):
        tender["title"] = meta_desc["content"].strip()
        tender["object_name"] = tender["title"]

    company_link = soup.select_one("a[href^='/companytenders/']")
    if company_link:
        tender["purchaser"] = company_link.get_text(strip=True)

    info_div = soup.select_one("div.col-md-8")
    if info_div:
        text = info_div.get_text(" ", strip=True)

        m = re.search(r"გამოცხადების თარიღი[:\s]+(\d{2}/\d{2}/\d{4})", text)
        if m:
            tender["announced_date"] = _parse_date(m.group(1))

        m = re.search(r"წინადადებების მიღების დასრულება[:\s]+(\d{2}/\d{2}/\d{4})", text)
        if m:
            tender["deadline"] = _parse_date(m.group(1))

        m = re.search(r"მაქსიმალური ღირებულება[^:]*:[^\d]*([0-9\s.,]+(?:GEL|₾|USD|EUR)?)", text)
        if m:
            tender["budget"] = m.group(1).strip()

        m = re.search(r"შესყიდვის ობიექტის კატეგორია[^:]*:\s*([^\n<]+)", text)
        if m:
            tender["category"] = m.group(1).strip()

    status_div = soup.select_one("div.col-md-4")
    if status_div:
        m = re.search(r"სტატუსი[^:]*:\s*([^\n<]+)", status_div.get_text(" ", strip=True))
        if m:
            tender["status"] = m.group(1).strip()

    files = {}
    for row in soup.select("table#FilesTable tr.IsObsolete0"):
        link = row.select_one("td:nth-child(2) a.IsObsolete0")
        if link and link.get("href"):
            href = link["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            filename = link.get_text(strip=True) or href.split("=")[-1][:60]
            files[filename] = href
            tender["file_urls"].append(href)

    if files:
        tender["doc_groups"]["დოკუმენტები"] = files

    desc_div = soup.select_one("div.additional-info-text")
    if desc_div:
        for tbl in desc_div.select("table"):
            tbl.decompose()
        desc_text = desc_div.get_text(" ", strip=True)
        desc_text = re.sub(r"^დამატებითი ინფორმაცია:\s*", "", desc_text).strip()
        if desc_text:
            tender["description"] = desc_text
            tender["object_description"] = desc_text

    if info_div and not tender["description"]:
        tender["description"] = info_div.get_text(" ", strip=True)
        tender["object_description"] = tender["description"]

    if not tender["description"]:
        tender["description"] = tender["title"]
        tender["object_description"] = tender["title"]

    return tender


def fetch_etenders_ge_history(
    max_pages: int = 10000,
    known_ids: set[str] | None = None,
) -> list[dict]:
    """
    Scrape all tenders from etenders.ge (newest first).

    Args:
        max_pages : hard page cap.
        known_ids : set of external_ids already in DB (e.g. {'etge_123', ...}).
                    Detail pages are skipped for these — makes re-runs fast.

    Returns:
        List of tender dicts for tenders not in known_ids.
    """
    known_ids = known_ids or set()
    session = _make_session()
    tenders: list[dict] = []

    for page in range(1, max_pages + 1):
        log.info(f"[etenders.ge] Listing page {page}...")
        try:
            params = {"pg": page} if page > 1 else {}
            resp = session.get(LIST_URL, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"[etenders.ge] Page {page} failed: {e}")
            break

        items = _parse_listing_page(resp.text)
        if not items:
            log.info("[etenders.ge] Empty page — done.")
            break

        new_on_page = 0
        for item in items:
            external_id = f"etge_{item['id']}"
            if external_id in known_ids:
                continue

            new_on_page += 1
            try:
                detail_resp = session.get(item["url"], timeout=30)
                detail_resp.raise_for_status()
            except Exception as e:
                log.error(f"[etenders.ge] Detail fetch failed {item['url']}: {e}")
                continue

            tender = _parse_detail(detail_resp.text, item["id"], item["url"])
            tenders.append(tender)
            log.info(
                f"  {external_id}: {tender.get('title','')[:60]} | "
                f"deadline={tender.get('deadline')} | purchaser={tender.get('purchaser','')[:40]}"
            )
            time.sleep(0.3)

        log.info(f"  Page {page}: {new_on_page} new | total so far: {len(tenders)}")

        # If every tender on this page was already known, we've caught up — stop
        if new_on_page == 0 and len(known_ids) > 0:
            log.info("[etenders.ge] Full page already in DB — stopping early.")
            break

        time.sleep(0.5)

    log.info(f"[etenders.ge] Done. {len(tenders)} tender(s) fetched.")
    return tenders
