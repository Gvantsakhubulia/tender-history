"""
Historical tender fetcher.

Usage:
    python main.py

Fetches ALL tenders for the configured date range (no status filter),
then writes two CSV files to the reports/ folder:

    all_tenders_YYYY-MM-DD.csv      — every tender in the date range
    ngt_matched_YYYY-MM-DD.csv      — only tenders whose CPV codes or
                                      keywords match NGT's profile

Adjust DATE_MONTHS_BACK to change how far back to look.
Set DATE_MONTHS_BACK = 1 for quick testing, 24 for full 2-year run.
"""

import csv
import json
import logging
import os
import re
from datetime import date, timedelta

from scraper import fetch_tenders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATE_MONTHS_BACK = 24         # 24 = full 2-year run
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
        # Extract all 8-digit numbers from the string
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
    # CPV match — check tender's 8-digit codes against NGT set
    for cpv_entry in tender.get("cpv_codes", []):
        for code in re.findall(r"\d{8}", cpv_entry):
            if code in ngt_cpvs:
                return True

    # Keyword match — search in object_name + description + category
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
    "cpv_codes",
    "object_name",
    "object_description",
    "description",
    "delivery_period",
    "bidders",
    "contract_status",
    "contract_winner",
    "contract_number",
    "contract_amount",
    "contract_start",
    "contract_end",
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
        "cpv_codes":          " | ".join(tender.get("cpv_codes", [])),
        "object_name":        tender.get("object_name", ""),
        "object_description": tender.get("object_description", ""),
        "description":        tender.get("description", ""),
        "delivery_period":    tender.get("delivery_period", ""),
        "bidders":            " | ".join(tender.get("bidders", [])),
        "contract_status":    tender.get("contract_status", ""),
        "contract_winner":    tender.get("contract_winner", ""),
        "contract_number":    tender.get("contract_number", ""),
        "contract_amount":    tender.get("contract_amount", ""),
        "contract_start":     tender.get("contract_start", ""),
        "contract_end":       tender.get("contract_end", ""),
        "file_urls":          "\n".join(tender.get("file_urls", [])),
        "url":                tender.get("url", ""),
    }


def _write_csv(tenders: list[dict], filepath: str) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for t in tenders:
            writer.writerow(_to_row(t))
    log.info(f"Saved {len(tenders)} rows → {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _tender_year(tender: dict) -> str:
    """Extract 4-digit year from announced_date (handles dd.mm.yyyy and yyyy-mm-dd)."""
    d = tender.get("announced_date", "")
    m = re.search(r"(\d{4})", d)
    return m.group(1) if m else "unknown"


def run():
    today     = date.today()
    date_from = today - timedelta(days=DATE_MONTHS_BACK * 30)

    date_from_str = date_from.strftime("%Y-%m-%d")
    date_to_str   = today.strftime("%Y-%m-%d")

    log.info(f"Date range: {date_from_str} → {date_to_str}")

    tenders = fetch_tenders(date_from=date_from_str, date_to=date_to_str)

    if not tenders:
        log.info("No tenders found.")
        return

    ngt_cpvs, ngt_keywords = _load_ngt_profile()

    # Group by year
    by_year: dict[str, list[dict]] = {}
    for t in tenders:
        yr = _tender_year(t)
        by_year.setdefault(yr, []).append(t)

    total_matched = 0
    for yr, yr_tenders in sorted(by_year.items()):
        ngt_matched = [t for t in yr_tenders if _matches_ngt(t, ngt_cpvs, ngt_keywords)]
        total_matched += len(ngt_matched)
        _write_csv(yr_tenders,  os.path.join(REPORTS_DIR, f"all_tenders_{yr}.csv"))
        _write_csv(ngt_matched, os.path.join(REPORTS_DIR, f"ngt_matched_{yr}.csv"))
        log.info(f"  {yr}: {len(yr_tenders)} total | {len(ngt_matched)} NGT-matched")

    log.info(
        f"Done. Total: {len(tenders)} across {len(by_year)} year(s) | "
        f"NGT-matched: {total_matched}"
    )


if __name__ == "__main__":
    run()
