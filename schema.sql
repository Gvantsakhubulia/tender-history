-- =============================================================
-- TENDER AGGREGATION PLATFORM — PostgreSQL Schema
-- Sources: procurement.gov.ge · tenders.ge · etenders.ge
-- =============================================================

-- =============================================================
-- 1. REFERENCE / LOOKUP TABLES
-- =============================================================

CREATE TABLE sources (
    id          SMALLSERIAL PRIMARY KEY,
    slug        VARCHAR(30)  NOT NULL UNIQUE, -- 'procurement_gov', 'tenders_ge', 'etenders_ge'
    name        VARCHAR(100) NOT NULL,
    base_url    VARCHAR(255) NOT NULL,
    description TEXT
);

INSERT INTO sources (slug, name, base_url) VALUES
    ('procurement_gov', 'procurement.gov.ge', 'https://procurement.gov.ge'),
    ('tenders_ge',      'tenders.ge',         'https://www.tenders.ge'),
    ('etenders_ge',     'etenders.ge',         'https://www.etenders.ge');


-- Standard EU/Georgian CPV code catalogue (hierarchical: division > group > class > category)
CREATE TABLE cpv_codes (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(20)  NOT NULL UNIQUE,  -- e.g. '71210000-3'
    code_normalized VARCHAR(8)   NOT NULL UNIQUE,  -- stripped check digit: '71210000'
    description_ka  TEXT,
    description_en  TEXT,
    parent_code     VARCHAR(8)   REFERENCES cpv_codes (code_normalized),
    division        CHAR(2),    -- first 2 digits  (e.g. '71' = Architectural services)
    group_code      CHAR(3),    -- first 3 digits
    class_code      CHAR(4),    -- first 4 digits
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_cpv_division ON cpv_codes (division);
CREATE INDEX idx_cpv_parent   ON cpv_codes (parent_code);


-- =============================================================
-- 2. COMPANIES / ORGANISATIONS
--    Covers both purchasers (public bodies) and suppliers
-- =============================================================

CREATE TABLE companies (
    id                  SERIAL       PRIMARY KEY,
    identification_code VARCHAR(20)  NOT NULL UNIQUE,   -- Georgian tax/registration ID
    name_ka             TEXT         NOT NULL,
    name_en             TEXT,
    company_type        VARCHAR(100),                   -- შპს, სს, ი/მ, ა(ა)იპ, etc.

    -- Roles (a company can be both)
    is_purchaser        BOOLEAN      NOT NULL DEFAULT FALSE,
    is_supplier         BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Contact details (from procurement.gov.ge supplier profiles)
    country             VARCHAR(100),
    city                VARCHAR(100),
    address             TEXT,
    phone               VARCHAR(50),
    fax                 VARCHAR(50),
    email               VARCHAR(255),
    website             VARCHAR(500),

    -- Audit
    first_seen_at       TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_companies_name_ka ON companies USING gin (to_tsvector('simple', name_ka));
CREATE INDEX idx_companies_name_en ON companies USING gin (to_tsvector('simple', coalesce(name_en, '')));


-- Contact persons for a company (directors, procurement officers, etc.)
CREATE TABLE company_contacts (
    id          SERIAL       PRIMARY KEY,
    company_id  INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    name        TEXT         NOT NULL,
    position    VARCHAR(200),
    phone       VARCHAR(50),
    email       VARCHAR(255),
    is_primary  BOOLEAN      DEFAULT FALSE,
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_company_contacts_company ON company_contacts (company_id);


-- CPV codes a company has been associated with (activity fingerprint).
-- Populated automatically from participation data; also from self-declared CPV lists
-- visible on procurement.gov.ge supplier pages.
CREATE TABLE company_cpv_codes (
    company_id      INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    cpv_code_id     INT          NOT NULL REFERENCES cpv_codes (id),
    source          VARCHAR(20)  NOT NULL DEFAULT 'participation',  -- 'participation' | 'self_declared'
    times_seen      INT          NOT NULL DEFAULT 1,
    last_seen_at    TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (company_id, cpv_code_id, source)
);


-- =============================================================
-- 3. TENDERS
-- =============================================================

CREATE TABLE tenders (
    id              SERIAL        PRIMARY KEY,
    source_id       SMALLINT      NOT NULL REFERENCES sources (id),
    external_id     VARCHAR(100)  NOT NULL,        -- portal's own ID / number
    url             TEXT          NOT NULL,

    title           TEXT          NOT NULL,
    description     TEXT,

    -- Who is buying
    purchaser_id    INT           REFERENCES companies (id),
    purchaser_name  TEXT,                          -- raw text when company not yet resolved

    -- Dates
    announced_date  DATE,
    deadline        TIMESTAMPTZ,
    contract_date   DATE,                          -- date contract was signed (procurement.gov only)

    -- Financial
    budget          NUMERIC(18, 2),
    contract_amount NUMERIC(18, 2),               -- actual contracted value
    currency        CHAR(3)        NOT NULL DEFAULT 'GEL',

    -- Classification
    procedure_type  VARCHAR(100),                  -- open, simplified, direct, competitive_dialogue…
    status          VARCHAR(50),                   -- announced | active | completed | cancelled | failed

    -- Scraping metadata
    raw_data        JSONB,                         -- full scraped payload; allows re-parsing later
    scraped_at      TIMESTAMPTZ    DEFAULT NOW(),
    updated_at      TIMESTAMPTZ    DEFAULT NOW(),

    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_tenders_source         ON tenders (source_id);
CREATE INDEX idx_tenders_announced      ON tenders (announced_date DESC);
CREATE INDEX idx_tenders_deadline       ON tenders (deadline);
CREATE INDEX idx_tenders_status         ON tenders (status);
CREATE INDEX idx_tenders_purchaser      ON tenders (purchaser_id);
CREATE INDEX idx_tenders_title_fts      ON tenders USING gin (to_tsvector('simple', title));
CREATE INDEX idx_tenders_description_fts ON tenders USING gin (to_tsvector('simple', coalesce(description, '')));


-- Bridge: tender ↔ CPV codes  (one tender may have one primary + many additional)
CREATE TABLE tender_cpv_codes (
    tender_id   INT     NOT NULL REFERENCES tenders (id)   ON DELETE CASCADE,
    cpv_code_id INT     NOT NULL REFERENCES cpv_codes (id),
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tender_id, cpv_code_id)
);

CREATE INDEX idx_tender_cpv_cpv ON tender_cpv_codes (cpv_code_id);


-- Attached documents / specification files
CREATE TABLE tender_documents (
    id            SERIAL       PRIMARY KEY,
    tender_id     INT          NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    name          TEXT,
    url           TEXT         NOT NULL,
    document_type VARCHAR(100),   -- 'specification' | 'contract' | 'protocol' | 'other'
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_tender_documents_tender ON tender_documents (tender_id);


-- Lots — procurement.gov.ge splits many tenders into individual lots
CREATE TABLE tender_lots (
    id              SERIAL        PRIMARY KEY,
    tender_id       INT           NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    lot_number      SMALLINT      NOT NULL,
    title           TEXT,
    budget          NUMERIC(18, 2),
    contract_amount NUMERIC(18, 2),
    status          VARCHAR(50),
    UNIQUE (tender_id, lot_number)
);

CREATE INDEX idx_tender_lots_tender ON tender_lots (tender_id);


-- CPV codes at lot level (can differ from the parent tender's CPVs)
CREATE TABLE tender_lot_cpv_codes (
    lot_id      INT     NOT NULL REFERENCES tender_lots (id) ON DELETE CASCADE,
    cpv_code_id INT     NOT NULL REFERENCES cpv_codes (id),
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (lot_id, cpv_code_id)
);


-- =============================================================
-- 4. PARTICIPATIONS  (bidders & winners)
--    Primarily sourced from procurement.gov.ge; other portals
--    may add records when/if their data is available.
-- =============================================================

CREATE TABLE tender_participations (
    id                      SERIAL        PRIMARY KEY,
    tender_id               INT           NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    lot_id                  INT           REFERENCES tender_lots (id),       -- NULL = whole-tender bid
    company_id              INT           NOT NULL REFERENCES companies (id),

    role                    VARCHAR(20)   NOT NULL,   -- 'bidder' | 'winner' | 'disqualified'
    bid_amount              NUMERIC(18, 2),
    bid_rank                SMALLINT,                 -- 1 = lowest/winning bid, 2 = runner-up…
    is_winner               BOOLEAN       NOT NULL DEFAULT FALSE,
    disqualification_reason TEXT,
    submitted_at            TIMESTAMPTZ,

    created_at              TIMESTAMPTZ   DEFAULT NOW(),

    UNIQUE (tender_id, lot_id, company_id)
);

CREATE INDEX idx_participations_tender  ON tender_participations (tender_id);
CREATE INDEX idx_participations_company ON tender_participations (company_id);
CREATE INDEX idx_participations_winner  ON tender_participations (tender_id) WHERE is_winner;


-- =============================================================
-- 5. SaaS PLATFORM — USERS & COMPANY PROFILES
-- =============================================================

CREATE TABLE users (
    id              SERIAL        PRIMARY KEY,
    email           VARCHAR(255)  NOT NULL UNIQUE,
    password_hash   TEXT          NOT NULL,
    full_name       TEXT,
    company_id      INT           REFERENCES companies (id),
    role            VARCHAR(20)   NOT NULL DEFAULT 'member',  -- 'admin' | 'member'
    is_verified     BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ   DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ
);

CREATE INDEX idx_users_company ON users (company_id);


-- Explicit CPV preferences a company sets in the UI
-- (augments the auto-detected profile from participation history)
CREATE TABLE company_cpv_preferences (
    company_id      INT    NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    cpv_code_id     INT    NOT NULL REFERENCES cpv_codes (id),
    weight          FLOAT  NOT NULL DEFAULT 1.0,  -- user-adjusted importance
    set_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (company_id, cpv_code_id)
);


-- =============================================================
-- 6. MATCHING ALGORITHM SUPPORT
-- =============================================================

-- Every interaction a company (or its users) has with a tender.
-- Feeds both item-based and collaborative filtering.
CREATE TABLE company_tender_interactions (
    id           SERIAL       PRIMARY KEY,
    company_id   INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id    INT          NOT NULL REFERENCES tenders (id)   ON DELETE CASCADE,
    -- interaction types ranked by signal strength:
    -- 'won' > 'bid' > 'saved' > 'viewed' > 'dismissed'
    interaction  VARCHAR(20)  NOT NULL,
    occurred_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_interactions_company ON company_tender_interactions (company_id);
CREATE INDEX idx_interactions_tender  ON company_tender_interactions (tender_id);
CREATE UNIQUE INDEX idx_interactions_unique
    ON company_tender_interactions (company_id, tender_id, interaction);


-- Saved / bookmarked tenders (shortcut over interactions table)
CREATE TABLE saved_tenders (
    company_id  INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id   INT          NOT NULL REFERENCES tenders (id)   ON DELETE CASCADE,
    saved_at    TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (company_id, tender_id)
);


-- Pre-computed recommendation cache — refreshed by a background job.
-- method:
--   'item_based'    — overlap of company CPV profile with tender CPV codes
--   'collaborative' — tenders that similar companies interacted with
--   'hybrid'        — weighted blend of both
CREATE TABLE tender_recommendations (
    id              SERIAL        PRIMARY KEY,
    company_id      INT           NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id       INT           NOT NULL REFERENCES tenders (id)   ON DELETE CASCADE,
    score           FLOAT         NOT NULL,
    method          VARCHAR(20)   NOT NULL,
    reason_cpv_id   INT           REFERENCES cpv_codes (id),   -- primary CPV that drove the score
    generated_at    TIMESTAMPTZ   DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    UNIQUE (company_id, tender_id)
);

CREATE INDEX idx_recommendations_company ON tender_recommendations (company_id, score DESC);
CREATE INDEX idx_recommendations_expires ON tender_recommendations (expires_at);


-- =============================================================
-- 7. HELPER VIEWS
-- =============================================================

-- Active tenders with their primary CPV code description (for quick display)
CREATE VIEW v_active_tenders AS
SELECT
    t.id,
    s.slug                           AS source,
    t.external_id,
    t.title,
    t.purchaser_name,
    t.announced_date,
    t.deadline,
    t.budget,
    t.currency,
    t.status,
    t.procedure_type,
    c.code_normalized                AS primary_cpv_code,
    c.description_ka                 AS primary_cpv_ka,
    t.url
FROM tenders t
JOIN sources s ON s.id = t.source_id
LEFT JOIN tender_cpv_codes tc ON tc.tender_id = t.id AND tc.is_primary
LEFT JOIN cpv_codes c         ON c.id = tc.cpv_code_id
WHERE t.deadline > NOW() OR t.status = 'active';


-- Company participation summary (win rate, total bids, last active)
CREATE VIEW v_company_stats AS
SELECT
    co.id                                   AS company_id,
    co.identification_code,
    co.name_ka,
    COUNT(DISTINCT p.tender_id)             AS total_bids,
    COUNT(DISTINCT p.tender_id) FILTER (WHERE p.is_winner) AS total_wins,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE p.is_winner)
        / NULLIF(COUNT(*), 0), 1
    )                                       AS win_rate_pct,
    MAX(t.announced_date)                   AS last_participation_date
FROM companies co
LEFT JOIN tender_participations p ON p.company_id = co.id
LEFT JOIN tenders t               ON t.id = p.tender_id
GROUP BY co.id, co.identification_code, co.name_ka;
