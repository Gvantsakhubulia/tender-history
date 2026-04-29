"""
Scraper for tenders.ge

Flow:
  1. Init session (get XSRF/Laravel cookies).
  2. GET listing pages (?page=N) — newest first.
  3. For each tender link, fetch the detail page.
  4. Parse: title, purchaser, type, status, deadline, description, CPV codes, files.
  5. Stop when cutoff_date is reached, max_pages is hit, or a page returns no items.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import date

log = logging.getLogger(__name__)

BASE_URL = "https://tenders.ge"
LIST_URL = BASE_URL + "/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ka,en-US;q=0.9,en;q=0.8",
}

# Full Georgian month names (detail page: "1 მაისი 2026, 18:00")
_GEO_MONTHS_FULL = {
    "იანვარი": 1, "თებერვალი": 2, "მარტი": 3, "აპრილი": 4,
    "მაისი": 5, "ივნისი": 6, "ივლისი": 7, "აგვისტო": 8,
    "სექტემბერი": 9, "ოქტომბერი": 10, "ნოემბერი": 11, "დეკემბერი": 12,
}

# Abbreviated Georgian month names (listing badge: "16-აპრ")
_GEO_MONTHS_SHORT = {
    "იან": 1, "თებ": 2, "მარ": 3, "აპრ": 4, "მაი": 5, "ივნ": 6,
    "ივლ": 7, "აგვ": 8, "სექ": 9, "ოქტ": 10, "ნოე": 11, "დეკ": 12,
}


def _parse_geo_date_full(text: str) -> str:
    """Parse '1 მაისი 2026, 18:00' → '2026-05-01'."""
    if not text:
        return ""
    m = re.search(r"(\d+)\s+(\S+)\s+(\d{4})", text.strip())
    if not m:
        return ""
    day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = _GEO_MONTHS_FULL.get(month_str)
    if not month:
        return ""
    try:
        return date(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_geo_date_short(text: str) -> str:
    """Parse '16-აპრ' (current year assumed) → '2026-04-16'."""
    if not text:
        return ""
    m = re.match(r"(\d+)-(\S+)", text.strip())
    if not m:
        return ""
    day, month_str = int(m.group(1)), m.group(2)
    month = _GEO_MONTHS_SHORT.get(month_str)
    if not month:
        return ""
    today = date.today()
    year = today.year
    if month < today.month:
        year = today.year + 1
    try:
        return date(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(BASE_URL + "/", timeout=30)
    resp.raise_for_status()
    log.debug(f"Session initialised. Cookies: {dict(session.cookies)}")
    return session


# ---------------------------------------------------------------------------
# Listing page parser
# ---------------------------------------------------------------------------

def _parse_listing_page(html: str) -> list[dict]:
    """Extract tender URLs and basic info from a listing page."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen: set[str] = set()

    for a in soup.select("a.tenders-list-item"):
        url = a.get("href", "")
        if not url or url in seen:
            continue
        seen.add(url)
        if not url.startswith("http"):
            url = BASE_URL + url

        id_m = re.search(r"/tenders/(\d+)/", url)
        tender_id = id_m.group(1) if id_m else ""

        # Announced date only present on tenders with the "ახალი" (new) badge
        new_p = a.find("p", class_="new")
        announced_raw = new_p.get("title", "") if new_p else ""

        items.append({
            "id":            tender_id,
            "url":           url,
            "announced_raw": announced_raw,
        })

    return items


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _parse_detail(html: str, url: str, tender_id: str, announced_raw: str) -> dict:
    """Parse a tender detail page into a tender dict."""
    soup = BeautifulSoup(html, "lxml")

    tender: dict = {
        "id":             tender_id,
        "url":            url,
        "number":         "",
        "title":          "",
        "purchaser":      "",
        "tender_type":    "",
        "status":         "",
        "announced_date": _parse_geo_date_short(announced_raw),
        "deadline":       "",
        "description":    "",
        "cpv_codes":      [],
        "file_urls":      [],
    }

    # Title
    h1 = soup.select_one("h1.tender-details-title")
    if h1:
        title = h1.get_text(" ", strip=True)
        title = re.sub(r"\s*(ელ\. ტენდერი|ფასების ცხრილი|ცვლილება)\s*", " ", title).strip()
        tender["title"] = title
        num_m = re.match(r"(T\d+)", title)
        if num_m:
            tender["number"] = num_m.group(1)

    # Status
    status_span = soup.select_one("span.status-info")
    if status_span:
        tender["status"] = status_span.get_text(strip=True)

    # Structured fields from detail list
    for li in soup.select("ul.tender-details-section-list li"):
        strong = li.find("strong")
        if not strong:
            continue
        label = strong.get_text(strip=True)
        value = li.get_text(" ", strip=True)[len(label):].strip()

        if "გამომცხადებელი" in label:
            tender["purchaser"] = value
        elif "შესყიდვის ტიპი" in label:
            tender["tender_type"] = value
        elif "წინადადების მიღება" in label or "ჩაბარება" in label:
            tender["deadline"] = _parse_geo_date_full(value)
        elif "გამოცხადება" in label:
            # Announced date from detail page (fallback for older tenders without listing badge)
            d = _parse_geo_date_full(value)
            if d and not tender["announced_date"]:
                tender["announced_date"] = d

    # Description + CPV codes
    content_div = soup.select_one("div.tender-content")
    if content_div:
        for span in content_div.select("ul.list li span"):
            text = span.get_text(strip=True)
            if re.match(r"^\d{8}", text):
                tender["cpv_codes"].append(text)
        for ul in content_div.select("ul.list"):
            ul.decompose()
        tender["description"] = content_div.get_text(" ", strip=True)

    # Attached files
    for a in soup.select("a.tender-details-document"):
        href = a.get("href", "")
        if href:
            tender["file_urls"].append(href)

    return tender


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_tenders_ge(
    max_pages: int = 500,
    cutoff_date: date | None = None,
) -> list[dict]:
    """
    Scrape tenders from tenders.ge by paginating the listing (newest first).

    Args:
        max_pages   : hard cap on listing pages (each page ~20 tenders).
        cutoff_date : stop once all tenders on a page are older than this date.
                      Pages are newest-first, so this is an early-exit optimisation.

    Returns:
        List of tender dicts with announced_date >= cutoff_date (when known).
    """
    session = _make_session()
    all_tenders: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        log.info(f"Fetching tenders.ge listing page {page}...")
        try:
            resp = session.get(LIST_URL, params={"page": page}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Listing page {page} failed: {e}")
            break

        items = _parse_listing_page(resp.text)
        if not items:
            log.info(f"Empty page {page} — done.")
            break

        new_on_page = 0
        past_cutoff_on_page = 0

        for item in items:
            tid = item["id"]
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)

            try:
                detail_resp = session.get(item["url"], timeout=30)
                detail_resp.raise_for_status()
            except Exception as e:
                log.error(f"Detail fetch failed for {item['url']}: {e}")
                continue

            tender = _parse_detail(
                detail_resp.text,
                item["url"],
                tid,
                item["announced_raw"],
            )

            # Date-based cutoff check (only when date is known)
            if cutoff_date and tender.get("announced_date"):
                try:
                    t_date = date.fromisoformat(tender["announced_date"])
                    if t_date < cutoff_date:
                        log.info(
                            f"  {tid}: announced {tender['announced_date']} < cutoff "
                            f"{cutoff_date} — skipping"
                        )
                        past_cutoff_on_page += 1
                        continue
                except ValueError:
                    pass

            all_tenders.append(tender)
            new_on_page += 1

            log.info(
                f"  {tid}: {tender.get('number')} | {tender.get('status')} | "
                f"announced={tender.get('announced_date')} | "
                f"cpv={len(tender['cpv_codes'])} | deadline={tender.get('deadline')}"
            )
            time.sleep(0.3)

        log.info(f"  Page {page}: {new_on_page} fetched | total: {len(all_tenders)}")

        # Stop if every tender on this page was past the cutoff
        if cutoff_date and past_cutoff_on_page > 0 and new_on_page == 0:
            log.info(f"All tenders on page {page} are past cutoff — stopping.")
            break

        time.sleep(0.5)

    log.info(f"Finished. Total tenders fetched from tenders.ge: {len(all_tenders)}")
    return all_tenders
