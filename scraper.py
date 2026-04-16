"""
Scraper for historical tenders from tenders.procurement.gov.ge.

Flow:
  1. Init session (get cookies).
  2. POST search with date range — no status filter, all tenders.
  3. Paginate through all result pages.
  4. For each tender fetch:
       a. app_main  → basic fields (budget, status, CPV, purchaser, etc.)
       b. app_docs  → documentation sections (object name, description)
       c. app_bids  → list of companies that submitted bids
  5. Return list of tender dicts.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL  = "https://tenders.procurement.gov.ge/public/"
AJAX_URL  = BASE_URL + "library/controller.php"
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
# Session
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(BASE_URL + "?lang=ge", timeout=30)
    resp.raise_for_status()
    log.debug(f"Session initialised. Cookies: {dict(session.cookies)}")
    return session


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_tenders(date_from: str, date_to: str, max_pages: int = 10000) -> list[dict]:
    """
    Scrape all tenders published between date_from and date_to.

    Args:
        date_from : "YYYY-MM-DD"
        date_to   : "YYYY-MM-DD"
        max_pages : safety cap

    Returns:
        List of tender dicts.
    """
    session = _make_session()
    all_tenders = []

    # Base POST payload — no status filter, date range applied
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
        "app_status":             "0",       # 0 = all statuses
        "app_agr_status":         "0",
        "app_type":               "0",
        "app_basecode":           "0",
        "app_codes":              "",
        "app_date_type":          "1",
        "app_date_from":          date_from,
        "app_date_tlll":          date_to,   # note: field name from live capture
        "app_amount_from":        "",
        "app_amount_to":          "",
        "app_currency":           "2",
        "app_pricelist":          "0",
    }

    for page in range(1, max_pages + 1):
        log.info(f"Fetching listing page {page} ({date_from} → {date_to})...")
        payload = {**base_payload, "page": page}

        try:
            resp = session.post(AJAX_URL, data=payload, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Listing page {page} failed: {e}")
            break

        rows = _parse_listing_rows(resp.text)
        if not rows:
            log.info(f"Empty page {page} — done.")
            break

        for row in rows:
            tender = _fetch_full_tender(session, row["id"], row["key"])
            if tender:
                all_tenders.append(tender)
            time.sleep(0.4)

        log.info(f"  Page {page}: {len(rows)} tenders | total so far: {len(all_tenders)}")
        time.sleep(1)

    log.info(f"Finished. Total tenders fetched: {len(all_tenders)}")
    return all_tenders


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
            timeout=30,
        )
        main_resp.raise_for_status()
        tender = _parse_main(main_resp.text, tender_id, key)

        # 2. Documentation
        docs_resp = session.get(
            AJAX_URL,
            params={"action": "app_docs", "app_id": tender_id, "key": key},
            timeout=30,
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
            timeout=30,
        )
        bids_resp.raise_for_status()
        tender["bidders"] = _parse_bids(bids_resp.text)

        # 4. Contract / winner info (no key required)
        agr_resp = session.get(
            AJAX_URL,
            params={"action": "agr_docs", "app_id": tender_id},
            timeout=30,
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
    """
    Parse contract/winner information from the agr_docs endpoint.

    The winner block looks like:
        <div class="ui-state-highlight ...">
            <span class="agrfg10">მიმდინარე ხელშეკრულება</span>
            ...
            <strong>შპს კარბო</strong>
            ნომერი/თანხა: N650... / <span class="convertme">17549.01 ლარი</span>
            ხელშეკრულება ძალაშია: 08.04.2026 - 30.06.2026
        </div>

    Note: some tenders have no contract yet — returns empty strings in that case.
    """
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

    # Winner name: first <strong> inside the <td>
    strong = td.find("strong")
    if strong:
        result["contract_winner"] = strong.get_text(strip=True)

    # Contract amount: span.convertme
    amount_span = td.find("span", class_="convertme")
    if amount_span:
        result["contract_amount"] = amount_span.get_text(strip=True)

    td_text = td.get_text(" ", strip=True)

    # Contract number: text between "ნომერი/თანხა:" and "/"
    num_match = re.search(r"ნომერი/თანხა:\s*(\S+)\s*/", td_text)
    if num_match:
        result["contract_number"] = num_match.group(1).strip()

    # Contract validity: "ხელშეკრულება ძალაშია: DD.MM.YYYY - DD.MM.YYYY"
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
    """
    Parse company names from the შეთავაზებები (bids) tab.

    The portal renders each bidder as:
        <td class="activebid1">
            <span class="color-1">Company Name</span>
        </td>

    Returns a list of company names, empty list if no bids yet.
    """
    soup = BeautifulSoup(html, "lxml")
    companies = []
    seen: set[str] = set()

    for span in soup.select("td.activebid1 span.color-1"):
        name = span.get_text(strip=True)
        if name and name not in seen:
            companies.append(name)
            seen.add(name)

    return companies
