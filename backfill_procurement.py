"""
One-time backfill script for procurement.gov.ge.

Fetches all tenders from the last 1 year and saves any not already in DB.
Safe to re-run — skips tenders already present via known_ids check.

Usage:
    python backfill_procurement.py
"""

import logging
from datetime import date

import db as dbmodule
from scraper import fetch_tenders

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SOURCE_SLUG = "procurement_gov"


def run():
    conn = dbmodule.connect()
    source_id = dbmodule.get_source_id(conn, SOURCE_SLUG)

    # Load IDs already in DB — used to skip detail saves, not to stop pagination
    with conn.cursor() as cur:
        cur.execute("SELECT external_id FROM tenders WHERE source_id = %s", (source_id,))
        known_ids = {row[0] for row in cur.fetchall()}
    log.info(f"DB: {len(known_ids)} procurement.gov.ge tenders already present.")

    today     = date.today()
    date_from = today.replace(year=today.year - 1).strftime("%Y-%m-%d")
    date_to   = today.strftime("%Y-%m-%d")
    log.info(f"Fetching all tenders from {date_from} → {date_to}...")

    saved = skipped = failed = total_checked = 0

    for tender, page in fetch_tenders(date_from=date_from, date_to=date_to, max_pages=10000):
        total_checked += 1

        if str(tender["id"]) in known_ids:
            skipped += 1
        else:
            try:
                tender_id, inserted = dbmodule.upsert_tender(conn, tender, source_id)
                for i, cpv_str in enumerate(tender.get("cpv_codes", [])):
                    cpv_id = dbmodule.upsert_cpv_code(conn, cpv_str)
                    if cpv_id:
                        dbmodule.insert_tender_cpv_codes(conn, tender_id, [(cpv_id, i == 0)])
                dbmodule.insert_tender_documents(conn, tender_id, tender.get("file_urls", []))
                if inserted:
                    dbmodule.enqueue_rescrape(conn, tender_id, str(tender["id"]),
                                              source_id, tender.get("deadline", ""))
                conn.commit()
                known_ids.add(str(tender["id"]))
                saved += 1
                log.info(f"  Saved: {tender.get('number')} | {tender.get('purchaser','')[:50]}")
            except Exception as e:
                conn.rollback()
                log.error(f"  Failed {tender['id']}: {e}")
                failed += 1

        if total_checked % 100 == 0:
            log.info(f"Progress: {total_checked} checked | {saved} saved | {skipped} skipped")

    conn.close()
    log.info(f"Done. Checked: {total_checked} | Saved: {saved} | Skipped: {skipped} | Failed: {failed}")


if __name__ == "__main__":
    run()
