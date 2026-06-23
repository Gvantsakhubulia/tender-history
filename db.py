"""
Database access layer — mirrors tender-aggregator/db.py exactly.

Every function takes an open psycopg2 connection as its first argument.
The caller is responsible for commit/rollback.

Upsert strategy:
  - companies         : ON CONFLICT (identification_code)
  - cpv_codes         : ON CONFLICT (code_normalized)
  - tenders           : ON CONFLICT (source_id, external_id)
  - tender_cpv_codes  : ON CONFLICT (tender_id, cpv_code_id)
  - tender_documents  : WHERE NOT EXISTS  (no unique constraint in schema)
  - company_contacts  : WHERE NOT EXISTS on (company_id, name)
  - company_cpv_codes : ON CONFLICT (company_id, cpv_code_id, source)
  - participations    : WHERE NOT EXISTS + UPDATE  (NULL lot_id breaks UNIQUE)
"""

import re
import json
import logging
import psycopg2
from config import DB

log = logging.getLogger(__name__)

# In-process cache: source slug → DB id
_SOURCE_CACHE: dict[str, int] = {}
# In-process cache: cpv code_normalized → DB id
_CPV_CACHE: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(**DB)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def get_source_id(conn, slug: str) -> int:
    if slug in _SOURCE_CACHE:
        return _SOURCE_CACHE[slug]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Unknown source slug '{slug}' — check seeds in 02_create_tables.sql")
        _SOURCE_CACHE[slug] = row[0]
    return _SOURCE_CACHE[slug]


# ---------------------------------------------------------------------------
# CPV codes
# ---------------------------------------------------------------------------

def upsert_cpv_code(conn, code_str: str) -> int | None:
    """
    Parse a CPV string such as '71210000 - description' or '71210000-3 - description'
    and upsert into cpv_codes.  Returns the row id, or None if unparseable.
    """
    code_str = code_str.strip()
    m = re.match(r"(\d{8})(-\d)?\s*[-–]\s*(.+)?", code_str)
    if not m:
        m2 = re.match(r"(\d{8})(-\d)?", code_str)
        if not m2:
            return None
        code_norm = m2.group(1)
        full_code  = code_str.split()[0]
        description = None
    else:
        code_norm   = m.group(1)
        full_code   = code_norm + (m.group(2) or "")
        description = m.group(3).strip() if m.group(3) else None

    if code_norm in _CPV_CACHE:
        return _CPV_CACHE[code_norm]

    division   = code_norm[:2]
    group_code = code_norm[:3]
    class_code = code_norm[:4]

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cpv_codes
                (code, code_normalized, description_ka, division, group_code, class_code)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (code_normalized) DO UPDATE
                SET description_ka = COALESCE(EXCLUDED.description_ka, cpv_codes.description_ka)
            RETURNING id
        """, (full_code, code_norm, description, division, group_code, class_code))
        cpv_id = cur.fetchone()[0]

    _CPV_CACHE[code_norm] = cpv_id
    return cpv_id


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def upsert_company(conn, company: dict) -> int:
    """
    Upsert a company by identification_code.  Returns companies.id.

    Required key : identification_code
    Optional keys: name_ka, company_type, is_purchaser, is_supplier,
                   country, city, address, phone, fax, email, website
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO companies (
                identification_code, name_ka, company_type,
                is_purchaser, is_supplier,
                country, city, address, phone, fax, email, website
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (identification_code) DO UPDATE SET
                name_ka      = COALESCE(EXCLUDED.name_ka,      companies.name_ka),
                company_type = COALESCE(EXCLUDED.company_type, companies.company_type),
                is_purchaser = companies.is_purchaser OR EXCLUDED.is_purchaser,
                is_supplier  = companies.is_supplier  OR EXCLUDED.is_supplier,
                country      = COALESCE(EXCLUDED.country,  companies.country),
                city         = COALESCE(EXCLUDED.city,     companies.city),
                address      = COALESCE(EXCLUDED.address,  companies.address),
                phone        = COALESCE(EXCLUDED.phone,    companies.phone),
                fax          = COALESCE(EXCLUDED.fax,      companies.fax),
                email        = COALESCE(EXCLUDED.email,    companies.email),
                website      = COALESCE(EXCLUDED.website,  companies.website),
                updated_at   = NOW()
            RETURNING id
        """, (
            company["identification_code"],
            company.get("name_ka") or "",
            company.get("company_type"),
            bool(company.get("is_purchaser", False)),
            bool(company.get("is_supplier",  False)),
            company.get("country")  or None,
            company.get("city")     or None,
            company.get("address")  or None,
            _normalize_phone(company.get("phone")),
            _normalize_phone(company.get("fax")),
            company.get("email")    or None,
            company.get("website")  or None,
        ))
        return cur.fetchone()[0]


def insert_company_contacts(conn, company_id: int, contacts: list[dict]) -> None:
    """Insert contact persons, skipping existing (matched by company_id + name)."""
    for i, c in enumerate(contacts):
        name = (c.get("name") or "").strip()
        if not name:
            continue
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO company_contacts
                    (company_id, name, position, phone, email, is_primary)
                SELECT %s, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM company_contacts
                    WHERE company_id = %s AND name = %s
                )
            """, (
                company_id, name,
                c.get("position") or None,
                _normalize_phone(c.get("phone")),
                c.get("email")    or None,
                i == 0,
                company_id, name,
            ))


def upsert_company_cpv_codes(conn, company_id: int,
                              cpv_ids: list[int], source: str) -> None:
    """Increment times_seen counter each time we see a CPV for this company."""
    for cpv_id in cpv_ids:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO company_cpv_codes
                    (company_id, cpv_code_id, source, times_seen, last_seen_at)
                VALUES (%s, %s, %s, 1, NOW())
                ON CONFLICT (company_id, cpv_code_id, source) DO UPDATE SET
                    times_seen   = company_cpv_codes.times_seen + 1,
                    last_seen_at = NOW()
            """, (company_id, cpv_id, source))


# ---------------------------------------------------------------------------
# Tenders
# ---------------------------------------------------------------------------

def _clean_amount(val) -> float | None:
    """Convert '258`026 ლარი', '39,473.00 GEL', '258026 ლარი' etc. to float."""
    if not val:
        return None
    cleaned = re.sub(r"[`\s,]", "", str(val))
    cleaned = re.sub(r"(GEL|₾|USD|EUR|ლარი|lari)", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_phone(raw: str) -> str | None:
    """Normalize Georgian phone to +995XXXXXXXXX (E.164). Returns None if unrecognizable."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw.strip())
    if not digits:
        return None
    if digits.startswith("995"):
        subscriber = digits[3:]
    else:
        subscriber = digits.lstrip("0")
    if re.match(r"^\d{9}$", subscriber):
        return f"+995{subscriber}"
    return raw.strip() or None


def _parse_date(val: str) -> str | None:
    """Accept DD.MM.YYYY or YYYY-MM-DD, return YYYY-MM-DD or None."""
    if not val:
        return None
    val = val.strip()
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](20\d{2}|19\d{2})", val)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if re.match(r"\d{4}-\d{2}-\d{2}", val):
        return val
    return None


def _build_full_description(tender: dict) -> str:
    """Build a complete description from doc_groups + description fields.

    doc_groups entries whose values are HTTP URLs (e.g. etenders.ge file links)
    are skipped — those belong in file_urls, not the description.
    """
    groups = tender.get("doc_groups", {})
    desc = tender.get("object_description") or tender.get("description") or ""

    section_parts = []
    for group_title, sections in groups.items():
        text_entries = [
            f"{label}: {text}"
            for label, text in sections.items()
            if text and not str(text).startswith("http")
        ]
        if text_entries:
            section_parts.append(group_title)
            section_parts.extend(text_entries)

    full_sections = "\n\n".join(section_parts)

    if full_sections and desc:
        return f"{desc}\n\n{full_sections}"
    return full_sections or desc


def upsert_tender(conn, tender: dict, source_id: int,
                  purchaser_id: int | None = None) -> int:
    """
    Upsert a tender row.  Returns tenders.id.
    On conflict (same source + external_id) updates status and contract fields only.
    """
    title = (
        tender.get("object_name")
        or tender.get("title")
        or tender.get("number")
        or tender.get("id", "")
    )
    description = _build_full_description(tender)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tenders (
                source_id, external_id, url,
                title, description,
                purchaser_id, purchaser_name,
                announced_date, deadline, contract_date,
                budget, contract_amount, currency,
                procedure_type, status,
                raw_data, scraped_at, updated_at
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, NOW(), NOW()
            )
            ON CONFLICT (source_id, external_id) DO UPDATE SET
                status          = EXCLUDED.status,
                description     = COALESCE(EXCLUDED.description, tenders.description),
                contract_amount = COALESCE(EXCLUDED.contract_amount, tenders.contract_amount),
                contract_date   = COALESCE(EXCLUDED.contract_date,   tenders.contract_date),
                purchaser_id    = COALESCE(EXCLUDED.purchaser_id,    tenders.purchaser_id),
                updated_at      = NOW()
            RETURNING id, (xmax = 0) AS inserted
        """, (
            source_id,
            str(tender.get("id", "")),
            tender.get("url", ""),
            title,
            description,
            purchaser_id,
            tender.get("purchaser") or None,
            _parse_date(tender.get("announced_date", "")),
            _parse_date(tender.get("deadline", "")),
            _parse_date(tender.get("contract_start", "")),
            _clean_amount(tender.get("budget")),
            _clean_amount(tender.get("contract_amount")),
            tender.get("currency") or "GEL",
            tender.get("tender_type") or None,
            tender.get("status") or None,
            json.dumps(tender, ensure_ascii=False),
        ))
        row = cur.fetchone()
        return row[0], row[1]  # (db_tender_id: int, inserted: bool)


def insert_tender_cpv_codes(conn, tender_id: int,
                             cpv_ids: list[tuple[int, bool]]) -> None:
    """cpv_ids: list of (cpv_code_id, is_primary)."""
    for cpv_id, is_primary in cpv_ids:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tender_cpv_codes (tender_id, cpv_code_id, is_primary)
                VALUES (%s, %s, %s)
                ON CONFLICT (tender_id, cpv_code_id) DO UPDATE
                    SET is_primary = EXCLUDED.is_primary
            """, (tender_id, cpv_id, is_primary))


def insert_tender_documents(conn, tender_id: int, file_urls: list[str]) -> None:
    for url in file_urls:
        if not url:
            continue
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tender_documents (tender_id, url)
                SELECT %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM tender_documents
                    WHERE tender_id = %s AND url = %s
                )
            """, (tender_id, url, tender_id, url))


# ---------------------------------------------------------------------------
# AI scores
# ---------------------------------------------------------------------------

def update_ai_scores(conn, tender_id: int,
                     ngt_score: int, ngt_reason: str,
                     doctra_score: int, doctra_reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tenders SET
                ai_scored     = TRUE,
                ngt_score     = %s,
                ngt_reason    = %s,
                doctra_score  = %s,
                doctra_reason = %s,
                updated_at    = NOW()
            WHERE id = %s
        """, (ngt_score, ngt_reason, doctra_score, doctra_reason, tender_id))


# ---------------------------------------------------------------------------
# Rescrape queue
# ---------------------------------------------------------------------------

def enqueue_rescrape(conn, tender_id: int, external_id: str,
                     source_id: int, deadline: str) -> None:
    """Add a tender to the rescrape queue if it has a future deadline."""
    deadline_date = _parse_date(deadline)
    if not deadline_date:
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tender_rescrape_queue
                (tender_id, external_id, source_id, deadline, rescrape_after)
            VALUES (%s, %s, %s, %s, %s::date + interval '1 day')
            ON CONFLICT (tender_id) DO NOTHING
        """, (tender_id, external_id, source_id, deadline_date, deadline_date))


def get_pending_rescrapes(conn, source_id: int) -> list[dict]:
    """Return queue items whose rescrape_after has passed and are still pending."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, tender_id, external_id, deadline
            FROM tender_rescrape_queue
            WHERE source_id = %s
              AND status = 'pending'
              AND rescrape_after < CURRENT_DATE
            ORDER BY deadline
        """, (source_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_rescrape_done(conn, queue_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tender_rescrape_queue WHERE id = %s", (queue_id,))


def mark_rescrape_failed(conn, queue_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tender_rescrape_queue
            SET status = 'failed', processed_at = NOW(), error = %s
            WHERE id = %s
        """, (error[:500], queue_id))


# ---------------------------------------------------------------------------
# Participations (bidders & winners)
# ---------------------------------------------------------------------------

def upsert_participation(conn, tender_id: int, company_id: int,
                          role: str, bid_amount=None,
                          is_winner: bool = False,
                          bid_rank: int | None = None) -> None:
    """
    Insert or update a participation row.
    Uses IS NOT DISTINCT FROM for NULL-safe lot_id comparison
    (the UNIQUE constraint alone does not cover NULL lot_id in PostgreSQL).
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tender_participations
                (tender_id, lot_id, company_id, role, bid_amount, is_winner, bid_rank)
            SELECT %s, NULL, %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM tender_participations
                WHERE tender_id = %s
                  AND company_id = %s
                  AND lot_id IS NULL
            )
        """, (
            tender_id, company_id, role, bid_amount, is_winner, bid_rank,
            tender_id, company_id,
        ))

        if cur.rowcount == 0:
            cur.execute("""
                UPDATE tender_participations SET
                    role       = %s,
                    bid_amount = COALESCE(%s, bid_amount),
                    is_winner  = %s,
                    bid_rank   = COALESCE(%s, bid_rank)
                WHERE tender_id = %s
                  AND company_id = %s
                  AND lot_id IS NULL
            """, (role, bid_amount, is_winner, bid_rank, tender_id, company_id))
