"""
Historical tender fetcher.

Usage:
    python main.py

Fetches tenders for the last DATE_YEARS_BACK years from procurement.gov.ge
(no status filter), then writes two CSV files per year to the reports/ folder:

    procurement_all_YYYY.csv     — every tender for that year
    procurement_ngt_YYYY.csv     — only tenders whose CPV codes or
                                   keywords match NGT's profile

Adjust DATE_YEARS_BACK to change how far back to look.
"""

import csv
import json
import logging
import os
import re
import time
from datetime import date

from scraper import fetch_tenders, save_checkpoint
import db as dbmodule
# from scraper_tenders_ge import fetch_tenders_ge  # tenders.ge (disabled)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATE_YEARS_BACK  = 1       # how many years of tenders to scrape
MAX_PAGES        = 10000   # hard page cap (server-side date filter stops earlier)
START_PAGE       = 1000    # skip already-scraped pages (set to 1 to start from beginning)
REPORTS_DIR      = "reports"
NGT_PROFILE_PATH = "NGT Profile.txt"


# ---------------------------------------------------------------------------
# NGT profile CPV + keyword loader
# ---------------------------------------------------------------------------

def _load_ngt_profile() -> tuple[set[str], list[str]]:
    """
    Returns (cpv_set, keywords) extracted from NGT Profile.txt.
    cpv_set  : 8-digit CPV code strings, e.g. {"35220000", "72261000", ...}
    keywords : list of lowercase keyword strings
    """
    with open(NGT_PROFILE_PATH, encoding="utf-8") as f:
        raw = f.read()

    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw.strip())

    profile = json.loads(raw)
    cpv_data = profile["NGT_Group_Profile"]["CPV_codes"]

    cpv_set = set()
    for codes in list(cpv_data["primary"]) + list(cpv_data["secondary"]):
        for code in re.findall(r"\d{8}", codes):
            cpv_set.add(code)

    raw_keywords = profile["NGT_Group_Profile"]["Keywords"]
    keywords = []
    for kw in raw_keywords:
        # Each entry may be "English / Georgian" — split and keep all parts
        for part in re.split(r"\s*/\s*", kw):
            part = part.strip().lower()
            if part:
                keywords.append(part)

    log.info(f"NGT profile loaded: {len(cpv_set)} CPV codes, {len(keywords)} keywords")
    return cpv_set, keywords


# ---------------------------------------------------------------------------
# CPV / keyword matcher
# ---------------------------------------------------------------------------

def _matches_ngt(tender: dict, ngt_cpvs: set[str], ngt_keywords: list[str]) -> bool:
    """Return True if the tender's CPV codes or text match NGT's profile."""
    for cpv_entry in tender.get("cpv_codes", []):
        for code in re.findall(r"\d{8}", cpv_entry):
            if code in ngt_cpvs:
                return True

    text = " ".join([
        tender.get("object_name", ""),
        tender.get("object_description", ""),
        tender.get("description", ""),
        tender.get("category", ""),
    ]).lower()

    for kw in ngt_keywords:
        if kw in text:
            return True

    return False


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "number",
    "status",
    "tender_type",
    "purchaser",
    "announced_date",
    "deadline",
    "budget",
    "category",
    "cpv_codes",
    "object_name",
    "object_description",
    "description",
    "bidders",
    "contract_winner",
    "contract_number",
    "contract_amount",
    "contract_start",
    "contract_end",
    "contract_status",
    "file_urls",
    "url",
]


def _to_row(tender: dict) -> dict:
    return {
        "number":             tender.get("number", ""),
        "status":             tender.get("status", ""),
        "tender_type":        tender.get("tender_type", ""),
        "purchaser":          tender.get("purchaser", ""),
        "announced_date":     tender.get("announced_date", ""),
        "deadline":           tender.get("deadline", ""),
        "budget":             tender.get("budget", ""),
        "category":           tender.get("category", ""),
        "cpv_codes":          " | ".join(tender.get("cpv_codes", [])),
        "object_name":        tender.get("object_name", ""),
        "object_description": tender.get("object_description", ""),
        "description":        tender.get("description", ""),
        "bidders":            " | ".join(b["name"] for b in tender.get("bidders", [])),
        "contract_winner":    tender.get("contract_winner", ""),
        "contract_number":    tender.get("contract_number", ""),
        "contract_amount":    tender.get("contract_amount", ""),
        "contract_start":     tender.get("contract_start", ""),
        "contract_end":       tender.get("contract_end", ""),
        "contract_status":    tender.get("contract_status", ""),
        "file_urls":          "\n".join(tender.get("file_urls", [])),
        "url":                tender.get("url", ""),
    }


def _write_csv(tenders: list[dict], filepath: str) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    file_exists = os.path.exists(filepath)
    encoding = "utf-8" if file_exists else "utf-8-sig"
    mode     = "a"     if file_exists else "w"
    for attempt in range(12):  # retry for up to ~60 seconds
        try:
            with open(filepath, mode, newline="", encoding=encoding) as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if not file_exists:
                    writer.writeheader()
                for t in tenders:
                    writer.writerow(_to_row(t))
            break
        except PermissionError:
            log.warning(f"'{filepath}' is locked — close it in Excel and waiting 5s... (attempt {attempt + 1}/12)")
            time.sleep(5)
    else:
        raise PermissionError(f"Could not write to '{filepath}' after 60s — file still locked.")
    action = "Appended" if file_exists else "Saved"
    log.info(f"{action} {len(tenders)} rows → {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _tender_year(tender: dict) -> str:
    """
    Extract year from announced_date.
    Handles DD.MM.YYYY (procurement.gov.ge) and YYYY-MM-DD.
    Uses an explicit year pattern (19xx / 20xx) to avoid matching
    non-year 4-digit sequences.
    """
    d = tender.get("announced_date", "").strip()
    # DD.MM.YYYY  →  capture group 3
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](20\d{2}|19\d{2})", d)
    if m:
        return m.group(3)
    # YYYY-MM-DD  →  capture group 1
    m = re.match(r"(20\d{2}|19\d{2})-\d{2}-\d{2}", d)
    if m:
        return m.group(1)
    # Fallback: find any plausible year anywhere in the string
    m = re.search(r"\b(20\d{2}|19\d{2})\b", d)
    return m.group(1) if m else "unknown"


FLUSH_EVERY = 1000  # write to CSV after this many tenders are buffered

_db_conn = None  # single connection reused across flushes


def _get_db_conn():
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        try:
            _db_conn = dbmodule.connect()
            log.info("DB connection established.")
        except Exception as e:
            log.error(f"DB connection failed — skipping DB save: {e}")
            _db_conn = None
    return _db_conn


def _save_batch_to_db(tenders: list[dict]) -> None:
    conn = _get_db_conn()
    if conn is None:
        return
    saved = 0
    try:
        source_id = dbmodule.get_source_id(conn, "procurement_gov")
    except Exception as e:
        log.error(f"DB: could not get source_id: {e}")
        return

    for tender in tenders:
        try:
            tender_id, inserted = dbmodule.upsert_tender(conn, tender, source_id)
            for i, cpv_str in enumerate(tender.get("cpv_codes", [])):
                cpv_id = dbmodule.upsert_cpv_code(conn, cpv_str)
                if cpv_id:
                    dbmodule.insert_tender_cpv_codes(conn, tender_id, [(cpv_id, i == 0)])
            dbmodule.insert_tender_documents(conn, tender_id, tender.get("file_urls", []))
            if inserted:
                dbmodule.enqueue_rescrape(conn, tender_id, str(tender.get("id", "")),
                                          source_id, tender.get("deadline", ""))
            conn.commit()
            saved += 1
        except Exception as e:
            conn.rollback()
            log.error(f"DB: failed to save tender {tender.get('id')}: {e}")

    log.info(f"DB: {saved}/{len(tenders)} tender(s) saved.")


def _flush(buffer: list[dict], ngt_cpvs: set, ngt_keywords: list) -> int:
    """Write buffer to CSV files grouped by year and save to DB. Returns count of NGT matches."""
    by_year: dict[str, list[dict]] = {}
    for t in buffer:
        by_year.setdefault(_tender_year(t), []).append(t)

    matched = 0
    for yr, yr_tenders in by_year.items():
        ngt_matched = [t for t in yr_tenders if _matches_ngt(t, ngt_cpvs, ngt_keywords)]
        matched += len(ngt_matched)
        _write_csv(yr_tenders, os.path.join(REPORTS_DIR, f"procurement_all_{yr}.csv"))
        if ngt_matched:
            _write_csv(ngt_matched, os.path.join(REPORTS_DIR, f"procurement_ngt_{yr}.csv"))

    _save_batch_to_db(buffer)
    return matched


def run():
    today     = date.today()
    cutoff    = today.replace(year=today.year - DATE_YEARS_BACK)
    date_from = cutoff.strftime("%Y-%m-%d")
    date_to   = today.strftime("%Y-%m-%d")
    log.info(f"Scraping procurement.gov.ge from {date_from} to {date_to}")

    ngt_cpvs, ngt_keywords = _load_ngt_profile()

    buffer: list[dict] = []
    last_page = 0
    total = 0
    total_matched = 0

    for tender, page in fetch_tenders(date_from=date_from, date_to=date_to, max_pages=MAX_PAGES, start_page=START_PAGE):
        buffer.append(tender)
        last_page = page
        if len(buffer) >= FLUSH_EVERY:
            total_matched += _flush(buffer, ngt_cpvs, ngt_keywords)
            total += len(buffer)
            save_checkpoint(last_page, date_from, date_to)
            log.info(f"  Flushed {len(buffer)} tenders to CSV (checkpoint: page {last_page}) | total: {total}")
            buffer.clear()

    # Final flush for whatever remains (checkpoint is managed by the generator)
    if buffer:
        total_matched += _flush(buffer, ngt_cpvs, ngt_keywords)
        total += len(buffer)

    log.info(f"Done. Total: {total} | NGT-matched: {total_matched}")

    # Run description backfill after historical scraping completes (one-time only)
    import subprocess, sys, os
    done_flag = r"C:\Users\gvantsa.khubulia\Tender_Monitor\reports\description_backfill_done.flag"
    if not os.path.exists(done_flag):
        backfill = r"C:\Users\gvantsa.khubulia\Tender_Monitor\backfill_descriptions.py"
        log.info("Starting description backfill...")
        result = subprocess.run([sys.executable, backfill], check=False)
        if result.returncode == 0:
            open(done_flag, "w").close()  # create flag so it never runs again
    else:
        log.info("Description backfill already completed — skipping.")


if __name__ == "__main__":
    run()
