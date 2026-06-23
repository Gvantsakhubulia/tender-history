"""
Rescraper — processes the tender_rescrape_queue.

Runs nightly (after the main TenderAggregator run) and re-fetches
bidder and winner information for tenders whose deadline has passed.

Usage:
    python rescraper.py

Flow:
    1. Connect to DB, pull all 'pending' queue items where rescrape_after <= today.
    2. Open a session to procurement.gov.ge.
    3. For each item:
       a. Fetch app_bids  → list of bidders (name, org_id, bid amount)
       b. Fetch agr_docs  → contract winner, amount, dates
       c. For each bidder fetch their company profile → identification_code
       d. Upsert company + contacts + CPV codes
       e. Upsert participation (bidder / winner)
       f. Update tender contract fields
       g. Mark queue item as done (or failed on error)
    4. Log summary.
"""

import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db as dbmodule
from scraper import (
    _make_session,
    _parse_bids,
    _parse_agr_docs,
    fetch_company_profile,
    AJAX_URL,
    REQUEST_TIMEOUT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SOURCE_SLUG = "procurement_gov"
NOTIFY_TO   = os.getenv("NOTIFY_EMAIL", "gvantsa.khubulia@zeptos.ge")


def _send_error_email(failures: list[tuple[str, str]]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    if not smtp_user:
        log.warning("SMTP not configured — skipping error email.")
        return
    items_html = "".join(
        f"<li style='font-family:monospace;font-size:12px'><b>{ext}</b>: {err}</li>"
        for ext, err in failures
    )
    body = f"""
    <div style='font-family:Arial,sans-serif;padding:16px'>
      <h2 style='color:#c62828'>TenderRescraper — {len(failures)} Failure(s)</h2>
      <ul>{items_html}</ul>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[TenderRescraper] {len(failures)} tender(s) failed to rescrape"
    msg["From"]    = smtp_user
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(body, "html"))
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, [NOTIFY_TO], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, [NOTIFY_TO], msg.as_string())
        log.info("Error notification email sent.")
    except Exception as e:
        log.error(f"Failed to send error email: {e}")


def _get_tender_key(conn, tender_id: int) -> str:
    """Retrieve the auth key stored in raw_data — needed for app_bids."""
    with conn.cursor() as cur:
        cur.execute("SELECT raw_data->>'key' FROM tenders WHERE id = %s", (tender_id,))
        row = cur.fetchone()
        return (row[0] or "") if row else ""


def _rescrape_one(session, conn, item: dict, source_id: int) -> None:
    external_id = item["external_id"]
    tender_id   = item["tender_id"]
    queue_id    = item["id"]

    log.info(f"  Rescraping tender {external_id} (tenders.id={tender_id}) ...")

    key = _get_tender_key(conn, tender_id)

    # --- Bids ---
    try:
        bids_resp = session.get(
            AJAX_URL,
            params={"action": "app_bids", "app_id": external_id, "key": key},
            timeout=REQUEST_TIMEOUT,
        )
        bids_resp.raise_for_status()
        bidders = _parse_bids(bids_resp.text)
    except Exception as e:
        raise RuntimeError(f"app_bids fetch failed: {e}")

    # --- Contract / winner ---
    try:
        agr_resp = session.get(
            AJAX_URL,
            params={"action": "agr_docs", "app_id": external_id},
            timeout=REQUEST_TIMEOUT,
        )
        agr_resp.raise_for_status()
        contract = _parse_agr_docs(agr_resp.text)
    except Exception as e:
        raise RuntimeError(f"agr_docs fetch failed: {e}")

    winner_name = contract.get("contract_winner", "").strip()

    # --- Companies + participations ---
    saved_bidders = 0
    for rank, bidder in enumerate(bidders, 1):
        org_id = bidder.get("org_id")
        if not org_id:
            continue

        company_data = fetch_company_profile(session, org_id)
        if not company_data or not company_data.get("identification_code"):
            log.warning(f"    Skipping bidder org_id={org_id} — no identification_code")
            continue

        company_data["is_supplier"] = True
        company_data["is_purchaser"] = False

        company_id = dbmodule.upsert_company(conn, company_data)
        dbmodule.insert_company_contacts(conn, company_id, company_data.get("contacts", []))

        cpv_ids = []
        for cpv_str in company_data.get("cpv_codes", []):
            cpv_id = dbmodule.upsert_cpv_code(conn, cpv_str)
            if cpv_id:
                cpv_ids.append(cpv_id)
        if cpv_ids:
            dbmodule.upsert_company_cpv_codes(conn, company_id, cpv_ids, "self_declared")

        is_winner = bool(winner_name and bidder["name"].strip() == winner_name)
        dbmodule.upsert_participation(
            conn, tender_id, company_id,
            role="winner" if is_winner else "bidder",
            bid_amount=bidder.get("last_bid_amount"),
            is_winner=is_winner,
            bid_rank=rank,
        )
        saved_bidders += 1
        time.sleep(0.3)

    # --- Update tender contract fields ---
    contract_amount = dbmodule._clean_amount(contract.get("contract_amount"))
    contract_date   = dbmodule._parse_date(contract.get("contract_start", ""))
    if contract_amount or contract_date:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tenders SET
                    contract_amount = COALESCE(%s, contract_amount),
                    contract_date   = COALESCE(%s::date, contract_date),
                    updated_at      = NOW()
                WHERE id = %s
            """, (contract_amount, contract_date, tender_id))

    conn.commit()
    dbmodule.mark_rescrape_done(conn, queue_id)
    log.info(f"    Done — {saved_bidders}/{len(bidders)} bidder(s) saved. "
             f"Winner: {winner_name or '—'}")


def _ensure_conn(conn):
    """Return a live connection, reconnecting if the server dropped it."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        log.info("DB connection lost — reconnecting...")
        return dbmodule.connect()


def run() -> None:
    try:
        conn = dbmodule.connect()
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        return

    source_id = dbmodule.get_source_id(conn, SOURCE_SLUG)
    items = dbmodule.get_pending_rescrapes(conn, source_id)

    if not items:
        log.info("No pending rescrapes.")
        conn.close()
        return

    log.info(f"{len(items)} tender(s) to rescrape.")
    session = _make_session()
    done = failed = 0
    failures: list[tuple[str, str]] = []

    for item in items:
        conn = _ensure_conn(conn)
        try:
            _rescrape_one(session, conn, item, source_id)
            done += 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.error(f"  Failed {item['external_id']}: {e}")
            failures.append((item["external_id"], str(e)))
            try:
                conn = _ensure_conn(conn)
                dbmodule.mark_rescrape_failed(conn, item["id"], str(e))
                conn.commit()
            except Exception:
                pass
            failed += 1
        time.sleep(1)

    conn.close()
    log.info(f"Rescrape complete — {done} done, {failed} failed.")

    if failures:
        _send_error_email(failures)


if __name__ == "__main__":
    run()
