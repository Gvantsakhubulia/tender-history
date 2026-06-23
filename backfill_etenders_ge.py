"""
One-time backfill script for etenders.ge.

For each listing page (100 tenders per page):
  - Checks which tender IDs are not yet in DB
  - Fetches detail pages only for missing ones
  - Saves them to DB (unique by source + external_id)

Always paginates to the last page — safe to re-run if interrupted.

Usage:
    python backfill_etenders_ge.py
"""

import re
import time
import logging
import requests
import urllib3
from bs4 import BeautifulSoup
import db as dbmodule

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL   = "https://etenders.ge"
LIST_URL   = BASE_URL + "/tenders"
PAGE_SIZE  = 100
SOURCE_SLUG = "etenders_ge"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
    "Accept-Language": "ka,en-US;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(text):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _parse_listing_page(html):
    """Return list of {id, url} from one listing page."""
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    for img in soup.select("img[src*='LogoHandler.aspx?tenderid=']"):
        m = re.search(r"tenderid=(\d+)", img.get("src", ""))
        if not m or m.group(1) in seen:
            continue
        tid = m.group(1)
        seen.add(tid)
        url = BASE_URL + f"/view/{tid}/"
        container = img.find_parent("td")
        if container:
            for a in container.find_all("a", href=re.compile(rf"/view/{tid}/")):
                url = BASE_URL + a.get("href")
                break
        items.append({"id": tid, "url": url})
    return items


def _parse_detail(html, tender_id, url):
    soup = BeautifulSoup(html, "lxml")
    t = {
        "id": f"etge_{tender_id}", "source": "etenders_ge", "url": url,
        "number": tender_id, "title": "", "purchaser": "", "status": "",
        "announced_date": "", "deadline": "", "budget": "", "category": "",
        "cpv_codes": [], "description": "", "object_name": "",
        "object_description": "", "doc_groups": {}, "file_urls": [],
    }

    meta = soup.find("meta", {"name": "description"})
    if meta and meta.get("content"):
        t["title"] = t["object_name"] = meta["content"].strip()

    company = soup.select_one("a[href^='/companytenders/']")
    if company:
        t["purchaser"] = company.get_text(strip=True)

    info = soup.select_one("div.col-md-8")
    if info:
        text = info.get_text(" ", strip=True)
        for pattern, field in [
            (r"გამოცხადების თარიღი[:\s]+(\d{2}/\d{2}/\d{4})", "announced_date"),
            (r"წინადადებების მიღების დასრულება[:\s]+(\d{2}/\d{2}/\d{4})", "deadline"),
        ]:
            m = re.search(pattern, text)
            if m:
                t[field] = _parse_date(m.group(1))
        m = re.search(r"მაქსიმალური ღირებულება[^:]*:[^\d]*([0-9\s.,]+(?:GEL|₾|USD|EUR)?)", text)
        if m:
            t["budget"] = m.group(1).strip()
        m = re.search(r"შესყიდვის ობიექტის კატეგორია[^:]*:\s*([^\n<]+)", text)
        if m:
            t["category"] = m.group(1).strip()

    status_div = soup.select_one("div.col-md-4")
    if status_div:
        m = re.search(r"სტატუსი[^:]*:\s*([^\n<]+)", status_div.get_text(" ", strip=True))
        if m:
            t["status"] = m.group(1).strip()[:50]

    files = {}
    for row in soup.select("table#FilesTable tr.IsObsolete0"):
        link = row.select_one("td:nth-child(2) a.IsObsolete0")
        if link and link.get("href"):
            href = link["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            files[link.get_text(strip=True) or href[-40:]] = href
            t["file_urls"].append(href)
    if files:
        t["doc_groups"]["დოკუმენტები"] = files

    desc_div = soup.select_one("div.additional-info-text")
    if desc_div:
        for tbl in desc_div.select("table"):
            tbl.decompose()
        desc = re.sub(r"^დამატებითი ინფორმაცია:\s*", "",
                      desc_div.get_text(" ", strip=True)).strip()
        if desc:
            t["description"] = t["object_description"] = desc

    if not t["description"] and info:
        t["description"] = t["object_description"] = info.get_text(" ", strip=True)
    if not t["description"]:
        t["description"] = t["object_description"] = t["title"]

    return t


def _save(conn, tender, source_id):
    tender_id, _ = dbmodule.upsert_tender(conn, tender, source_id)
    for i, cpv in enumerate(tender.get("cpv_codes", [])):
        cpv_id = dbmodule.upsert_cpv_code(conn, cpv)
        if cpv_id:
            dbmodule.insert_tender_cpv_codes(conn, tender_id, [(cpv_id, i == 0)])
    dbmodule.insert_tender_documents(conn, tender_id, tender.get("file_urls", []))
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    conn = dbmodule.connect()
    source_id = dbmodule.get_source_id(conn, SOURCE_SLUG)

    # Load all IDs already in DB — used to skip detail fetches only
    with conn.cursor() as cur:
        cur.execute("SELECT external_id FROM tenders WHERE source_id = %s", (source_id,))
        known_ids = {row[0] for row in cur.fetchall()}
    log.info(f"DB: {len(known_ids)} etenders.ge tenders already present.")

    session = requests.Session()
    session.verify = False
    session.headers.update(HEADERS)
    session.get(BASE_URL + "/", timeout=30)

    saved = skipped = failed = page = 0
    seen_listing_ids: set[str] = set()  # all IDs seen on listing pages so far

    while True:
        page += 1
        try:
            params = {}
            if page > 1:
                params["pg"] = page
            resp = session.get(LIST_URL, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Page {page} failed: {e}")
            break

        items = _parse_listing_page(resp.text)
        if not items:
            log.info(f"Page {page}: empty — reached end of portal.")
            break

        # Detect portal looping: if every ID on this page was already seen in a
        # previous listing page, the portal is repeating itself — stop.
        page_ids = {i["id"] for i in items}
        if page_ids.issubset(seen_listing_ids):
            log.info(f"Page {page}: portal is repeating content — done.")
            break
        seen_listing_ids.update(page_ids)

        # Only fetch details for IDs not in DB
        missing = [i for i in items if f"etge_{i['id']}" not in known_ids]
        skipped += len(items) - len(missing)
        log.info(f"Page {page}: {len(missing)} missing / {len(items)} total")

        for item in missing:
            try:
                detail = session.get(item["url"], timeout=30)
                detail.raise_for_status()
                tender = _parse_detail(detail.text, item["id"], item["url"])
                _save(conn, tender, source_id)
                known_ids.add(f"etge_{item['id']}")  # mark as known for this run
                saved += 1
                time.sleep(0.3)
            except Exception as e:
                conn.rollback()
                log.error(f"  Failed etge_{item['id']}: {e}")
                failed += 1

        time.sleep(0.5)

    conn.close()
    log.info(f"Done. Saved: {saved} | Skipped (already in DB): {skipped} | Failed: {failed}")


if __name__ == "__main__":
    run()
