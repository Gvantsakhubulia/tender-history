"""
Historical tenders.ge scraper.

Usage:
    python main_tenders_ge.py                # scrape last 2 years (default)
    python main_tenders_ge.py --years 3      # scrape last 3 years

Fetches all tenders from tenders.ge from DATE_YEARS_BACK years ago to today,
saves them to the DB and writes a CSV report to reports/.
"""

import argparse
import csv
import logging
import os
from datetime import date

import db as dbmodule
from scraper_tenders_ge import fetch_tenders_ge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

REPORTS_DIR   = "reports"
SOURCE_SLUG   = "tenders_ge"
FLUSH_EVERY   = 200   # save to DB after this many tenders
DEFAULT_YEARS = 2

CSV_FIELDS = [
    "id", "number", "title", "purchaser", "tender_type", "status",
    "announced_date", "deadline", "cpv_codes", "description", "file_urls", "url",
]


def _write_csv(tenders: list[dict], filepath: str) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    file_exists = os.path.exists(filepath)
    mode     = "a" if file_exists else "w"
    encoding = "utf-8" if file_exists else "utf-8-sig"
    with open(filepath, mode, newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for t in tenders:
            writer.writerow({
                "id":             t.get("id", ""),
                "number":         t.get("number", ""),
                "title":          t.get("title", ""),
                "purchaser":      t.get("purchaser", ""),
                "tender_type":    t.get("tender_type", ""),
                "status":         t.get("status", ""),
                "announced_date": t.get("announced_date", ""),
                "deadline":       t.get("deadline", ""),
                "cpv_codes":      " | ".join(t.get("cpv_codes", [])),
                "description":    t.get("description", ""),
                "file_urls":      "\n".join(t.get("file_urls", [])),
                "url":            t.get("url", ""),
            })
    log.info(f"  CSV: {'appended' if file_exists else 'saved'} {len(tenders)} rows → {filepath}")


def _save_to_db(tenders: list[dict], conn, source_id: int) -> int:
    saved = 0
    for tender in tenders:
        try:
            tender_id, _ = dbmodule.upsert_tender(conn, tender, source_id)
            for i, cpv_str in enumerate(tender.get("cpv_codes", [])):
                cpv_id = dbmodule.upsert_cpv_code(conn, cpv_str)
                if cpv_id:
                    dbmodule.insert_tender_cpv_codes(conn, tender_id, [(cpv_id, i == 0)])
            dbmodule.insert_tender_documents(conn, tender_id, tender.get("file_urls", []))
            conn.commit()
            saved += 1
        except Exception as e:
            conn.rollback()
            log.error(f"  DB: failed to save tender {tender.get('id')}: {e}")
    return saved


def run(years_back: int = DEFAULT_YEARS) -> None:
    today      = date.today()
    cutoff     = today.replace(year=today.year - years_back)
    csv_path   = os.path.join(REPORTS_DIR, f"tenders_ge_all.csv")

    log.info(f"Scraping tenders.ge from {cutoff} to {today} ...")

    # --- DB connection ---
    try:
        conn = dbmodule.connect()
        source_id = dbmodule.get_source_id(conn, SOURCE_SLUG)
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        return

    # --- Scrape ---
    all_tenders = fetch_tenders_ge(max_pages=5000, cutoff_date=cutoff)

    if not all_tenders:
        log.info("No tenders fetched.")
        conn.close()
        return

    log.info(f"Fetched {len(all_tenders)} tender(s). Saving to DB and CSV...")

    # --- Flush in batches ---
    total_saved = 0
    for i in range(0, len(all_tenders), FLUSH_EVERY):
        batch = all_tenders[i : i + FLUSH_EVERY]
        saved = _save_to_db(batch, conn, source_id)
        total_saved += saved
        _write_csv(batch, csv_path)
        log.info(f"  Batch {i // FLUSH_EVERY + 1}: {saved}/{len(batch)} saved to DB")

    conn.close()
    log.info(f"Done. {total_saved}/{len(all_tenders)} tender(s) saved to DB.")
    log.info(f"CSV report: {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Historical tenders.ge scraper")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"How many years back to scrape (default: {DEFAULT_YEARS})")
    args = parser.parse_args()
    run(years_back=args.years)
