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
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
    session.verify = False
    session.headers.update(HEADERS)
    resp = session.get(BASE_URL + "?lang=ge", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    log.debug(f"Session initialised. Cookies: {dict(session.cookies)}")
    return session


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_tenders(date_from: str, date_to: str, max_pages: int = 10000, start_page: int = 0):
    """
    Generator — yields one tender at a time so the caller can buffer and
    persist in batches (e.g. every 1000 tenders).

    Automatically resumes from a saved checkpoint if one exists for the
    same date range (i.e. a previous run was interrupted).

    Args:
        date_from  : "YYYY-MM-DD"
        date_to    : "YYYY-MM-DD"
        max_pages  : safety cap
        start_page : if > 0, skip directly to this page (overrides checkpoint)
    """
    start_page = start_page if start_page > 0 else _load_checkpoint(date_from, date_to)
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
        doc_groups, object_name, object_description, file_urls = _parse_docs(docs_resp.text)
        tender["doc_groups"]         = doc_groups
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

def _parse_docs(html: str) -> tuple[dict, str, str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    doc_groups: dict = {}
    current_group = "ზოგადი"
    object_name = ""
    object_description = ""

    for tag in soup.select("p.color-1, section.question"):
        if tag.name == "p" and "color-1" in tag.get("class", []):
            strong = tag.find("strong")
            if strong:
                current_group = strong.get_text(strip=True)
        elif tag.name == "section":
            span = tag.find("span")
            q_label = span.get_text(strip=True) if span else ""
            answer_div = tag.find("div", class_="a")
            a_text = ""
            if answer_div:
                for blk in answer_div.select(".hst-blk"):
                    blk.decompose()
                a_text = answer_div.get_text(" ", strip=True)
            if q_label:
                doc_groups.setdefault(current_group, {})[q_label] = a_text
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

    return doc_groups, object_name, object_description, file_urls


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
# app_bids parser — bidders with org_id and bid amounts
# ---------------------------------------------------------------------------

def _parse_bids(html: str) -> list[dict]:
    """
    Returns list of dicts:
      org_id          – portal's internal company ID (used to fetch profile)
      name            – company display name
      last_bid_amount – final offer as float (or None)
      last_bid_time   – "DD.MM.YYYY HH:MM" string
    """
    soup = BeautifulSoup(html, "lxml")
    bidders = []
    seen_ids: set[str] = set()

    for row in soup.select("tr[id^='B']"):
        tds = row.find_all("td", class_="activebid1")
        if not tds:
            continue

        # org_id from onclick="ShowProfile(12345)"
        profile_link = tds[0].find("a", onclick=re.compile(r"ShowProfile\("))
        org_id = ""
        if profile_link:
            m = re.search(r"ShowProfile\((\d+)\)", profile_link.get("onclick", ""))
            if m:
                org_id = m.group(1)

        name_span = tds[0].find("span", class_="color-1")
        name = name_span.get_text(strip=True) if name_span else ""

        if not name or org_id in seen_ids:
            continue
        seen_ids.add(org_id)

        # last bid amount (column 2, inside <strong>)
        last_amount = None
        last_time = ""
        if len(tds) > 1:
            strong = tds[1].find("strong")
            if strong:
                raw = strong.get_text(strip=True).replace("`", "").replace(",", "").replace(" ", "")
                try:
                    last_amount = float(raw)
                except ValueError:
                    pass
            date_span = tds[1].find("span", class_="date")
            if date_span:
                last_time = date_span.get_text(strip=True)

        bidders.append({
            "org_id":          org_id,
            "name":            name,
            "last_bid_amount": last_amount,
            "last_bid_time":   last_time,
        })

    return bidders


# ---------------------------------------------------------------------------
# Company profile fetcher + parser  (action=profile&org_id=X)
# ---------------------------------------------------------------------------

_PROFILE_LABEL_MAP = {
    "საიდენტიფიკაციო კოდი": "identification_code",
    "ქვეყანა":               "country",
    "ქალაქი/დაბა/სოფელი":   "city",
    "მისამართი":             "address",
    "ტელეფონი":              "phone",
    "ფაქსი":                 "fax",
}


def fetch_company_profile(session: requests.Session, org_id: str) -> dict | None:
    """
    Fetch and parse the supplier profile page.
    Returns a company dict ready for db.upsert_company(), or None on failure.
    """
    try:
        resp = session.get(
            AJAX_URL,
            params={"action": "profile", "org_id": org_id},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Profile fetch failed for org_id={org_id}: {e}")
        return None

    return _parse_company_profile(resp.text, org_id)


def _parse_company_profile(html: str, org_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    company: dict = {
        "org_id":             org_id,
        "identification_code": "",
        "name_ka":            "",
        "company_type":       "",
        "country":            "",
        "city":               "",
        "address":            "",
        "phone":              "",
        "fax":                "",
        "email":              "",
        "website":            "",
        "contacts":           [],   # list of {name, position, phone, email}
        "cpv_codes":          [],   # list of "XXXXXXXX - description" strings
        "is_supplier":        True,
        "is_purchaser":       False,
    }

    # Main info table (class="ktable with-label")
    main_table = soup.find("table", class_=lambda c: c and "with-label" in c)
    if main_table:
        for tr in main_table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = tds[0].get_text(strip=True)
            field = _PROFILE_LABEL_MAP.get(label)

            if label == "მიმწოდებელი":
                label_tag = tds[1].find("label")
                if label_tag:
                    company["company_type"] = label_tag.get_text(strip=True)
                strong = tds[1].find("strong")
                if strong:
                    company["name_ka"] = strong.get_text(strip=True)
            elif label == "ელ-ფოსტა":
                a = tds[1].find("a")
                company["email"] = a.get_text(strip=True) if a else tds[1].get_text(strip=True)
            elif label == "ვებ-გვერდი":
                a = tds[1].find("a")
                href = a.get("href", "") if a else ""
                company["website"] = href if href not in ("http://", "https://", "") else ""
            elif field:
                company[field] = tds[1].get_text(strip=True)

    # Contacts table (tbody id="c")
    contacts_tbody = soup.find("tbody", id="c")
    if contacts_tbody:
        for tr in contacts_tbody.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            name_td = tds[0]
            strong = name_td.find("strong")
            name = strong.get_text(strip=True) if strong else name_td.get_text(strip=True)
            position = ""
            # position is text after <br> inside the td
            br = name_td.find("br")
            if br and br.next_sibling:
                position = str(br.next_sibling).strip()

            phone = tds[1].get_text(strip=True) if len(tds) > 1 else ""
            email_td = tds[2] if len(tds) > 2 else None
            email = ""
            if email_td:
                a = email_td.find("a")
                email = a.get_text(strip=True) if a else email_td.get_text(strip=True)

            if name:
                company["contacts"].append({
                    "name": name, "position": position,
                    "phone": phone, "email": email,
                })

    # Self-declared CPV codes (ui-state-highlight div)
    cpv_div = soup.find("div", class_="ui-state-highlight")
    if cpv_div:
        for li in cpv_div.find_all("li"):
            strong = li.find("strong")
            if strong:
                code = strong.get_text(strip=True)
                rest = li.get_text(" ", strip=True)
                rest = re.sub(r"^\d+\s*-\s*", "", rest).strip()
                company["cpv_codes"].append(f"{code} - {rest}" if rest else code)

    return company
