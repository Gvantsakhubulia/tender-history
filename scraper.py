"""
Scraper for historical tenders from tenders.procurement.gov.ge.

Flow:
  1. Init session (get cookies).
  2. POST search with date range — no status filter, all tenders.
  3. Paginate through all result pages (resume from checkpoint if present).
  4. For each tender fetch:
       a. app_main  → basic fields (budget, status, CPV, purchaser, etc.)
       b. app_docs  → documentation sections (object name, description)
       c. app_bids  → list of companies that submitted bids
  5. Return list of tender dicts.

Checkpoint:
  After every page a checkpoint.json is written to REPORTS_DIR so that
  a interrupted run can be resumed from where it stopped.
"""

import json
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL        = "https://tenders.procurement.gov.ge/public/"
AJAX_URL        = BASE_URL + "library/controller.php"
REQUEST_TIMEOUT = 60   # seconds — raised from 30; portal can be slow under load
REPORTS_DIR     = "reports"
CHECKPOINT_FILE = os.path.join(REPORTS_DIR, "checkpoint.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL + "?lang=ge",
}

_LABEL_MAP = {
    "განცხადების ნომერი":                    "number",
    "შესყიდვის ტიპი":                        "tender_type",
    "შესყიდვის სტატუსი":                     "status",
    "შემსყიდველი":                           "purchaser",
    "შესყიდვის გამოცხადების თარიღი":         "announced_date",
    "წინადადებების მიღება მთავრდება":        "deadline",
    "პრეისკურანტის სავარაუდო ღირებულება":    "budget",
    "შესყიდვის სავარაუდო ღირებულება":        "budget",
    "შესყიდვის კატეგორია":                   "category",
    "მოწოდების ვადა":                        "delivery_period",
}


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def save_checkpoint(page: int, date_from: str, date_to: str) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_page": page, "date_from": date_from, "date_to": date_to}, f)


def _load_checkpoint(date_from: str, date_to: str) -> int:
    """
    Return the page to start from. If a checkpoint exists for the same
    date range, resume from last_page + 1. Otherwise start from 1.
    """
    if not os.path.exists(CHECKPOINT_FILE):
        return 1
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date_from") == date_from:
            resume = data["last_page"] + 1
            log.info(f"Checkpoint found — resuming from page {resume}")
            return resume
    except Exception:
        pass
    return 1


def clear_checkpoint() -> None:
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        log.info("Checkpoint cleared — run completed successfully.")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(BASE_URL + "?lang=ge", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    log.debug(f"Session initialised. Cookies: {dict(session.cookies)}")
    return session


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_tenders(date_from: str, date_to: str, max_pages: int = 10000):
    """
    Generator — yields one tender at a time so the caller can buffer and
    persist in batches (e.g. every 1000 tenders).

    Automatically resumes from a saved checkpoint if one exists for the
    same date range (i.e. a previous run was interrupted).

    Args:
        date_from : "YYYY-MM-DD"
        date_to   : "YYYY-MM-DD"
        max_pages : safety cap
    """
    start_page = _load_checkpoint(date_from, date_to)
    session    = _make_session()

    base_payload = {
        "action":                 "search_app",
        "app_t":                  "0",
        "search":                 "",
        "app_reg_id":             "",
        "app_shems_id":           "0",
        "org_a":                  "",
        "app_monac_id":           "0",
        "org_b":                  "",
        "app_particip_status_id": "0",
        "app_donor_id":           "0",
        "app_status":             "0",
        "app_agr_status":         "0",
        "app_type":               "0",
        "app_basecode":           "0",
        "app_codes":              "",
        "app_date_type":          "1",
        "app_date_from":          date_from,
        "app_date_tlll":          date_to,
        "app_amount_from":        "",
        "app_amount_to":          "",
        "app_currency":           "2",
        "app_pricelist":          "0",
    }

    for page in range(start_page, max_pages + 1):
        log.info(f"Fetching listing page {page} ({date_from} → {date_to})...")
        payload = {**base_payload, "page": page}

        try:
            resp = session.post(AJAX_URL, data=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Listing page {page} failed: {e}")
            save_checkpoint(page - 1, date_from, date_to)
            return

        rows = _parse_listing_rows(resp.text)
        if not rows:
            log.info(f"Empty page {page} — done.")
            clear_checkpoint()
            return

        page_count = 0
        for row in rows:
            tender = _fetch_full_tender(session, row["id"], row["key"])
            if tender:
                page_count += 1
                yield tender, page
            time.sleep(0.4)

        log.info(f"  Page {page}: {page_count} tenders fetched")
        time.sleep(1)

    clear_checkpoint()


# ---------------------------------------------------------------------------
# Listing page parser
# ---------------------------------------------------------------------------

def _parse_listing_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("tr[id]"):
        raw_id = tr.get("id", "")
        match = re.match(r"A(\d+)", raw_id)
        if not match:
            continue
        tender_id = match.group(1)
        onclick = tr.get("onclick", "")
        key_match = re.search(r"ShowApp\(\d+,'[^']*',\d+,'([^']+)'\)", onclick)
        key = key_match.group(1) if key_match else ""
        rows.append({"id": tender_id, "key": key})
    return rows


# ---------------------------------------------------------------------------
# Full tender fetch
# ---------------------------------------------------------------------------

def _fetch_full_tender(session: requests.Session, tender_id: str, key: str) -> dict | None:
    try:
        # 1. Basic fields
        main_resp = session.get(
            AJAX_URL,
            params={"action": "app_main", "app_id": tender_id, "key": key},
            timeout=REQUEST_TIMEOUT,
        )
        main_resp.raise_for_status()
        tender = _parse_main(main_resp.text, tender_id, key)

        # 2. Documentation
        docs_resp = session.get(
            AJAX_URL,
            params={"action": "app_docs", "app_id": tender_id, "key": key},
            timeout=REQUEST_TIMEOUT,
        )
        docs_resp.raise_for_status()
        object_name, object_description, file_urls = _parse_docs(docs_resp.text)
        tender["object_name"]        = object_name
        tender["object_description"] = object_description
        tender["file_urls"]          = file_urls

        # 3. Bids — who participated
        bids_resp = session.get(
            AJAX_URL,
            params={"action": "app_bids", "app_id": tender_id, "key": key},
            timeout=REQUEST_TIMEOUT,
        )
        bids_resp.raise_for_status()
        tender["bidders"] = _parse_bids(bids_resp.text)

        # 4. Contract / winner info (no key required)
        agr_resp = session.get(
            AJAX_URL,
            params={"action": "agr_docs", "app_id": tender_id},
            timeout=REQUEST_TIMEOUT,
        )
        agr_resp.raise_for_status()
        tender.update(_parse_agr_docs(agr_resp.text))

        log.info(
            f"  {tender_id}: {tender.get('number')} | "
            f"{tender.get('status')} | "
            f"bidders={len(tender['bidders'])} | "
            f"winner={tender.get('contract_winner') or '-'}"
        )
        return tender

    except Exception as e:
        log.error(f"Failed tender {tender_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# app_main parser
# ---------------------------------------------------------------------------

def _parse_main(html: str, tender_id: str, key: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    tender = {
        "id":             tender_id,
        "key":            key,
        "url":            f"{BASE_URL}?go={tender_id}&lang=ge",
        "number":         "",
        "tender_type":    "",
        "status":         "",
        "purchaser":      "",
        "announced_date": "",
        "deadline":       "",
        "budget":         "",
        "category":       "",
        "cpv_codes":      [],
        "description":     "",
        "delivery_period": "",
        # filled later by agr_docs
        "contract_winner":  "",
        "contract_number":  "",
        "contract_amount":  "",
        "contract_start":   "",
        "contract_end":     "",
        "contract_status":  "",
    }

    for tr in soup.select("table.ktable tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        label = tds[0].get_text(strip=True)
        value = tds[1].get_text(" ", strip=True)
        field = _LABEL_MAP.get(label)
        if field:
            tender[field] = value

    tender["cpv_codes"] = [
        li.get_text(" ", strip=True)
        for li in soup.select("ul li")
        if re.match(r"^\s*\d{8}", li.get_text(strip=True))
    ]

    blabla = soup.find("div", class_="blabla")
    if blabla:
        tender["description"] = blabla.get_text(" ", strip=True)

    return tender


# ---------------------------------------------------------------------------
# app_docs parser
# ---------------------------------------------------------------------------

def _parse_docs(html: str) -> tuple[str, str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    object_name = ""
    object_description = ""

    for tag in soup.select("p.color-1, section.question"):
        if tag.name == "section":
            span = tag.find("span")
            q_label = span.get_text(strip=True) if span else ""
            answer_div = tag.find("div", class_="a")
            a_text = ""
            if answer_div:
                for blk in answer_div.select(".hst-blk"):
                    blk.decompose()
                a_text = answer_div.get_text(" ", strip=True)
            if "1.1" in q_label and "დასახელება" in q_label:
                object_name = a_text
            elif "1.2" in q_label and ("ტექნიკური" in q_label or "აღწერა" in q_label):
                object_description = a_text

    file_urls = []
    for a in soup.select("a[href*='files.php']"):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = BASE_URL + href.lstrip("/")
        file_urls.append(href)

    return object_name, object_description, file_urls


# ---------------------------------------------------------------------------
# agr_docs parser — contract winner info
# ---------------------------------------------------------------------------

def _parse_agr_docs(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    result = {
        "contract_winner":  "",
        "contract_number":  "",
        "contract_amount":  "",
        "contract_start":   "",
        "contract_end":     "",
        "contract_status":  "",
    }

    highlight = soup.find("div", class_="ui-state-highlight")
    if not highlight:
        return result

    status_span = highlight.find("span", class_="agrfg10")
    if status_span:
        result["contract_status"] = status_span.get_text(strip=True)

    table = highlight.find("table")
    if not table:
        return result

    td = table.find("td")
    if not td:
        return result

    strong = td.find("strong")
    if strong:
        result["contract_winner"] = strong.get_text(strip=True)

    amount_span = td.find("span", class_="convertme")
    if amount_span:
        result["contract_amount"] = amount_span.get_text(strip=True)

    td_text = td.get_text(" ", strip=True)

    num_match = re.search(r"ნომერი/თანხა:\s*(\S+)\s*/", td_text)
    if num_match:
        result["contract_number"] = num_match.group(1).strip()

    date_match = re.search(
        r"ხელშეკრულება ძალაშია:\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})",
        td_text,
    )
    if date_match:
        result["contract_start"] = date_match.group(1)
        result["contract_end"]   = date_match.group(2)

    return result


# ---------------------------------------------------------------------------
# app_bids parser — list of companies that bid
# ---------------------------------------------------------------------------

def _parse_bids(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    companies = []
    seen: set[str] = set()

    for span in soup.select("td.activebid1 span.color-1"):
        name = span.get_text(strip=True)
        if name and name not in seen:
            companies.append(name)
            seen.add(name)

    return companies
