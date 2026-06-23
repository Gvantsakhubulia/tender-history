-- =============================================================
-- STEP 2: Run this script connected to 'tender_aggregator' DB.
-- =============================================================

-- =============================================================
-- GROUP 1: REFERENCE / LOOKUP TABLES
-- =============================================================

CREATE TABLE sources (
    id          SMALLSERIAL   PRIMARY KEY,
    slug        VARCHAR(30)   NOT NULL UNIQUE,
                                            -- 'procurement_gov' | 'tenders_ge' | 'etenders_ge'
    name        VARCHAR(100)  NOT NULL,     -- human-readable portal name
    base_url    VARCHAR(255)  NOT NULL,     -- root URL of the portal
    is_active   BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  sources          IS 'The three scraping sources (portals)';
COMMENT ON COLUMN sources.slug     IS 'Short machine-readable identifier used in code';
COMMENT ON COLUMN sources.base_url IS 'Root URL; used to build absolute links from relative paths';


-- Standard EU/Georgian CPV (Common Procurement Vocabulary) code catalogue.
-- Hierarchy: division (2 digits) → group (3) → class (4) → category (8).
CREATE TABLE cpv_codes (
    id              SERIAL        PRIMARY KEY,
    code            VARCHAR(12)   NOT NULL UNIQUE,   -- full code with check digit, e.g. '71210000-3'
    code_normalized VARCHAR(8)    NOT NULL UNIQUE,   -- without check digit, e.g. '71210000'
    description_ka  TEXT,                            -- Georgian description
    description_en  TEXT,                            -- English description
    parent_code     VARCHAR(8)    REFERENCES cpv_codes (code_normalized),
                                                     -- NULL for top-level divisions
    division        CHAR(2),                         -- e.g. '71'
    group_code      CHAR(3),                         -- e.g. '712'
    class_code      CHAR(4),                         -- e.g. '7121'
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  cpv_codes                 IS 'EU/Georgian CPV code catalogue (hierarchical)';
COMMENT ON COLUMN cpv_codes.code            IS 'Full CPV code including check digit';
COMMENT ON COLUMN cpv_codes.code_normalized IS 'CPV code without check digit — use this for joins';
COMMENT ON COLUMN cpv_codes.parent_code     IS 'References code_normalized of the parent node';

CREATE INDEX idx_cpv_division ON cpv_codes (division);
CREATE INDEX idx_cpv_parent   ON cpv_codes (parent_code);


-- =============================================================
-- GROUP 2: COMPANIES / ORGANISATIONS
-- =============================================================

-- Single table for all organisations: public purchasers AND private suppliers.
-- A company may be both (e.g., a state enterprise that also bids on contracts).
CREATE TABLE companies (
    id                  SERIAL        PRIMARY KEY,
    identification_code VARCHAR(20)   NOT NULL UNIQUE,
                                             -- Georgian tax ID (9 or 11 digits)
    name_ka             TEXT          NOT NULL,
    name_en             TEXT,
    company_type        VARCHAR(100), -- შპს · სს · ი/მ · ა(ა)იპ · სახელმწიფო · etc.

    -- Role flags
    is_purchaser        BOOLEAN       NOT NULL DEFAULT FALSE,
                                             -- TRUE if this org buys (appears as შემსყიდველი)
    is_supplier         BOOLEAN       NOT NULL DEFAULT FALSE,
                                             -- TRUE if this org bids/wins (appears as მიმწოდებელი)

    -- Contact details (populated from procurement.gov.ge supplier pages)
    country             VARCHAR(100),
    city                VARCHAR(100),
    address             TEXT,
    phone               VARCHAR(50),
    fax                 VARCHAR(50),
    email               VARCHAR(255),
    website             VARCHAR(500),

    -- Audit
    first_seen_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  companies                    IS 'All organisations — purchasers, suppliers, or both';
COMMENT ON COLUMN companies.identification_code IS 'Georgian tax/registration ID — primary deduplication key across portals';
COMMENT ON COLUMN companies.is_purchaser        IS 'TRUE if the company has appeared as a buyer in any tender';
COMMENT ON COLUMN companies.is_supplier         IS 'TRUE if the company has appeared as a bidder/winner in any tender';

CREATE INDEX idx_companies_is_purchaser ON companies (is_purchaser) WHERE is_purchaser;
CREATE INDEX idx_companies_is_supplier  ON companies (is_supplier)  WHERE is_supplier;
CREATE INDEX idx_companies_name_fts     ON companies USING gin (to_tsvector('simple', name_ka));


-- Contact persons for a company (scraped from procurement.gov.ge supplier profiles)
CREATE TABLE company_contacts (
    id          SERIAL        PRIMARY KEY,
    company_id  INT           NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    name        TEXT          NOT NULL,     -- full name, e.g. 'თამაზი მაჩიტიძე'
    position    VARCHAR(200),              -- e.g. 'დირექტორი'
    phone       VARCHAR(50),
    email       VARCHAR(255),
    is_primary  BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  company_contacts            IS 'Directors and procurement contacts for a company';
COMMENT ON COLUMN company_contacts.is_primary IS 'TRUE for the main contact person shown first';

CREATE INDEX idx_company_contacts_company ON company_contacts (company_id);


-- CPV codes a company has been linked to across all portals.
-- Source 'participation': derived automatically from tender bids and wins.
-- Source 'self_declared': taken from the CPV catalogue listed on their supplier profile page.
CREATE TABLE company_cpv_codes (
    company_id      INT           NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    cpv_code_id     INT           NOT NULL REFERENCES cpv_codes (id),
    source          VARCHAR(20)   NOT NULL DEFAULT 'participation',
                                           -- 'participation' | 'self_declared'
    times_seen      INT           NOT NULL DEFAULT 1,
                                           -- incremented each time we see this CPV for this company
    last_seen_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, cpv_code_id, source)
);

COMMENT ON TABLE  company_cpv_codes          IS 'CPV activity fingerprint per company — drives item-based matching';
COMMENT ON COLUMN company_cpv_codes.times_seen IS 'Higher count = stronger signal for the recommendation algorithm';

CREATE INDEX idx_company_cpv_company ON company_cpv_codes (company_id);
CREATE INDEX idx_company_cpv_code    ON company_cpv_codes (cpv_code_id);


-- =============================================================
-- GROUP 3: TENDERS
-- =============================================================

CREATE TABLE tenders (
    id              SERIAL        PRIMARY KEY,

    -- Source identification
    source_id       SMALLINT      NOT NULL REFERENCES sources (id),
    external_id     VARCHAR(100)  NOT NULL,   -- portal's own ID / tender number
    url             TEXT          NOT NULL,   -- canonical URL of the tender detail page

    -- Content
    title           TEXT          NOT NULL,
    description     TEXT,

    -- Purchaser (foreign key populated when identification_code is known)
    purchaser_id    INT           REFERENCES companies (id),
    purchaser_name  TEXT,                     -- raw text fallback if company not yet resolved

    -- Key dates
    announced_date  DATE,                     -- date the tender was published
    deadline        TIMESTAMPTZ,              -- submission / offer deadline
    contract_date   DATE,                     -- date contract was signed (procurement.gov only)

    -- Financial
    budget          NUMERIC(18, 2),           -- estimated / reserved budget
    contract_amount NUMERIC(18, 2),           -- actual contracted value (after award)
    currency        CHAR(3)       NOT NULL DEFAULT 'GEL',

    -- Classification
    procedure_type  VARCHAR(100),
                    -- 'open' | 'simplified' | 'direct' | 'competitive_dialogue' | etc.
    status          VARCHAR(50),
                    -- 'announced' | 'active' | 'evaluation' | 'completed' | 'cancelled' | 'failed'

    -- Scraping metadata
    raw_data        JSONB,
                    -- complete scraped payload stored for future re-parsing without re-scraping
    scraped_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    UNIQUE (source_id, external_id)
);

COMMENT ON TABLE  tenders              IS 'All tenders from all three portals, unified schema';
COMMENT ON COLUMN tenders.external_id  IS 'The tender ID or number as it appears on the source portal';
COMMENT ON COLUMN tenders.raw_data     IS 'Full scraped JSON — allows re-parsing when schema evolves';
COMMENT ON COLUMN tenders.contract_amount IS 'Populated only after award; NULL until then';

CREATE INDEX idx_tenders_source          ON tenders (source_id);
CREATE INDEX idx_tenders_announced       ON tenders (announced_date DESC);
CREATE INDEX idx_tenders_deadline        ON tenders (deadline);
CREATE INDEX idx_tenders_status          ON tenders (status);
CREATE INDEX idx_tenders_purchaser       ON tenders (purchaser_id);
CREATE INDEX idx_tenders_budget          ON tenders (budget);
CREATE INDEX idx_tenders_title_fts       ON tenders USING gin (to_tsvector('simple', title));
CREATE INDEX idx_tenders_description_fts ON tenders USING gin (to_tsvector('simple', coalesce(description, '')));


-- Bridge: one tender may have one primary CPV code + several additional ones.
CREATE TABLE tender_cpv_codes (
    tender_id   INT     NOT NULL REFERENCES tenders   (id) ON DELETE CASCADE,
    cpv_code_id INT     NOT NULL REFERENCES cpv_codes (id),
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
                        -- TRUE for exactly one row per tender (the main CPV)
    PRIMARY KEY (tender_id, cpv_code_id)
);

COMMENT ON TABLE  tender_cpv_codes           IS 'CPV codes assigned to a tender (one primary, many additional)';
COMMENT ON COLUMN tender_cpv_codes.is_primary IS 'Exactly one row per tender should have is_primary = TRUE';

CREATE INDEX idx_tender_cpv_by_code ON tender_cpv_codes (cpv_code_id);


-- Attached files: technical specifications, contract drafts, protocols, etc.
CREATE TABLE tender_documents (
    id            SERIAL        PRIMARY KEY,
    tender_id     INT           NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    name          TEXT,                      -- display name / filename
    url           TEXT          NOT NULL,   -- direct download or preview link
    document_type VARCHAR(100), -- 'specification' | 'contract_draft' | 'award_protocol' | 'other'
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tender_documents IS 'Files attached to a tender (specs, contracts, protocols)';

CREATE INDEX idx_tender_documents_tender ON tender_documents (tender_id);


-- Lots — procurement.gov.ge often splits a single tender into numbered lots.
-- Each lot is independently bid on and awarded.
CREATE TABLE tender_lots (
    id              SERIAL         PRIMARY KEY,
    tender_id       INT            NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    lot_number      SMALLINT       NOT NULL,    -- sequential number within the tender
    title           TEXT,
    budget          NUMERIC(18, 2),
    contract_amount NUMERIC(18, 2),
    status          VARCHAR(50),               -- mirrors tenders.status values
    UNIQUE (tender_id, lot_number)
);

COMMENT ON TABLE tender_lots IS 'Individual lots within a tender (procurement.gov.ge)';

CREATE INDEX idx_tender_lots_tender ON tender_lots (tender_id);


-- CPV codes at lot level (may differ from the parent tender's CPVs)
CREATE TABLE tender_lot_cpv_codes (
    lot_id      INT     NOT NULL REFERENCES tender_lots (id) ON DELETE CASCADE,
    cpv_code_id INT     NOT NULL REFERENCES cpv_codes   (id),
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (lot_id, cpv_code_id)
);

COMMENT ON TABLE tender_lot_cpv_codes IS 'CPV codes at lot level — can differ from parent tender CPVs';

CREATE INDEX idx_lot_cpv_by_code ON tender_lot_cpv_codes (cpv_code_id);


-- =============================================================
-- GROUP 4: PARTICIPATIONS (BIDDERS & WINNERS)
-- =============================================================

-- Records every company that participated in a tender:
--   role = 'bidder'        — submitted an offer but did not win
--   role = 'winner'        — awarded the contract
--   role = 'disqualified'  — submitted but was disqualified
--
-- lot_id is NULL when the participation covers the whole tender (no lots).
CREATE TABLE tender_participations (
    id                      SERIAL         PRIMARY KEY,
    tender_id               INT            NOT NULL REFERENCES tenders      (id) ON DELETE CASCADE,
    lot_id                  INT            REFERENCES tender_lots           (id),
    company_id              INT            NOT NULL REFERENCES companies     (id),

    role                    VARCHAR(20)    NOT NULL
                                           CHECK (role IN ('bidder', 'winner', 'disqualified')),
    bid_amount              NUMERIC(18, 2),             -- offered price
    bid_rank                SMALLINT,                   -- 1 = lowest/winning, 2 = runner-up, etc.
    is_winner               BOOLEAN        NOT NULL DEFAULT FALSE,
    disqualification_reason TEXT,                       -- populated when role = 'disqualified'
    submitted_at            TIMESTAMPTZ,               -- timestamp of bid submission

    created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    UNIQUE (tender_id, lot_id, company_id)
);

COMMENT ON TABLE  tender_participations                    IS 'All bidders and winners per tender / lot';
COMMENT ON COLUMN tender_participations.role               IS 'bidder | winner | disqualified';
COMMENT ON COLUMN tender_participations.bid_rank           IS '1 = winning (lowest) bid rank';
COMMENT ON COLUMN tender_participations.lot_id             IS 'NULL means the bid covers the whole tender (no lots)';

CREATE INDEX idx_participations_tender  ON tender_participations (tender_id);
CREATE INDEX idx_participations_company ON tender_participations (company_id);
CREATE INDEX idx_participations_winners ON tender_participations (tender_id)
    WHERE is_winner = TRUE;


-- =============================================================
-- GROUP 5: MATCHING ALGORITHM SUPPORT TABLES
-- =============================================================

-- Behavioural signals from SaaS users — used by both recommendation methods.
-- Signal strength (descending): won > bid > saved > viewed > dismissed
CREATE TABLE company_tender_interactions (
    id           SERIAL       PRIMARY KEY,
    company_id   INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id    INT          NOT NULL REFERENCES tenders   (id) ON DELETE CASCADE,
    interaction  VARCHAR(20)  NOT NULL
                              CHECK (interaction IN ('viewed', 'saved', 'bid', 'won', 'dismissed')),
    occurred_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (company_id, tender_id, interaction)
);

COMMENT ON TABLE  company_tender_interactions             IS 'Behavioural signals for collaborative-filtering model';
COMMENT ON COLUMN company_tender_interactions.interaction IS 'Signal strength: won > bid > saved > viewed > dismissed';

CREATE INDEX idx_interactions_company ON company_tender_interactions (company_id);
CREATE INDEX idx_interactions_tender  ON company_tender_interactions (tender_id);


-- Bookmarked / saved tenders (fast read path; also recorded in interactions)
CREATE TABLE saved_tenders (
    company_id  INT          NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id   INT          NOT NULL REFERENCES tenders   (id) ON DELETE CASCADE,
    note        TEXT,                  -- optional private note the user adds
    saved_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, tender_id)
);

COMMENT ON TABLE saved_tenders IS 'Tenders bookmarked by a company for quick access';

CREATE INDEX idx_saved_tenders_company ON saved_tenders (company_id);


-- Pre-computed recommendation cache, refreshed by a background scheduler.
--   method = 'item_based'    — CPV profile overlap (company_cpv_codes × tender_cpv_codes)
--   method = 'collaborative' — tenders liked by similar companies
--   method = 'hybrid'        — weighted blend of both
CREATE TABLE tender_recommendations (
    id              SERIAL        PRIMARY KEY,
    company_id      INT           NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    tender_id       INT           NOT NULL REFERENCES tenders   (id) ON DELETE CASCADE,
    score           FLOAT         NOT NULL CHECK (score >= 0),
    method          VARCHAR(20)   NOT NULL
                                  CHECK (method IN ('item_based', 'collaborative', 'hybrid')),
    reason_cpv_id   INT           REFERENCES cpv_codes (id),
                                  -- primary CPV that drove this score (for explanation UI)
    generated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,  -- NULL = never expires; set by the refresh job

    UNIQUE (company_id, tender_id)
);

COMMENT ON TABLE  tender_recommendations              IS 'Cached tender recommendations per company';
COMMENT ON COLUMN tender_recommendations.score        IS 'Higher = better match; used for ranking';
COMMENT ON COLUMN tender_recommendations.reason_cpv_id IS 'CPV code shown to user as explanation ("matched because: Architecture services")';
COMMENT ON COLUMN tender_recommendations.expires_at   IS 'Refresh job sets this; stale rows are rebuilt nightly';

CREATE INDEX idx_recommendations_company ON tender_recommendations (company_id, score DESC);
CREATE INDEX idx_recommendations_expires ON tender_recommendations (expires_at)
    WHERE expires_at IS NOT NULL;


-- =============================================================
-- GROUP 6: SEED DATA
-- =============================================================

INSERT INTO sources (slug, name, base_url) VALUES
    ('procurement_gov', 'procurement.gov.ge', 'https://procurement.gov.ge'),
    ('tenders_ge',      'tenders.ge',          'https://www.tenders.ge'),
    ('etenders_ge',     'etenders.ge',          'https://www.etenders.ge');


-- =============================================================
-- GROUP 7: CONVENIENCE VIEWS
-- =============================================================

-- Open tenders enriched with source name and primary CPV description
CREATE VIEW v_active_tenders AS
SELECT
    t.id,
    s.slug                               AS source,
    t.external_id,
    t.title,
    COALESCE(c.name_ka, t.purchaser_name) AS purchaser,
    t.announced_date,
    t.deadline,
    t.budget,
    t.currency,
    t.status,
    t.procedure_type,
    cpv.code_normalized                  AS primary_cpv,
    cpv.description_ka                   AS primary_cpv_ka,
    t.url
FROM       tenders           t
JOIN       sources            s   ON s.id = t.source_id
LEFT JOIN  companies          c   ON c.id = t.purchaser_id
LEFT JOIN  tender_cpv_codes   tc  ON tc.tender_id = t.id AND tc.is_primary
LEFT JOIN  cpv_codes          cpv ON cpv.id = tc.cpv_code_id
WHERE t.deadline > NOW()
   OR t.status IN ('announced', 'active');

COMMENT ON VIEW v_active_tenders IS 'Open/active tenders with purchaser name and primary CPV joined in';


-- Per-company win statistics (used for company profile page)
CREATE VIEW v_company_stats AS
SELECT
    co.id                                                          AS company_id,
    co.identification_code,
    co.name_ka,
    co.company_type,
    COUNT(DISTINCT p.tender_id)                                    AS total_bids,
    COUNT(DISTINCT p.tender_id) FILTER (WHERE p.is_winner)        AS total_wins,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE p.is_winner)
        / NULLIF(COUNT(*), 0), 1
    )                                                              AS win_rate_pct,
    SUM(p.bid_amount)                                              AS total_bid_value,
    SUM(p.bid_amount) FILTER (WHERE p.is_winner)                  AS total_won_value,
    MAX(t.announced_date)                                          AS last_participation_date
FROM       companies             co
LEFT JOIN  tender_participations  p  ON p.company_id = co.id
LEFT JOIN  tenders                t  ON t.id = p.tender_id
GROUP BY   co.id, co.identification_code, co.name_ka, co.company_type;

COMMENT ON VIEW v_company_stats IS 'Bid/win statistics per company — used on the company profile page';


-- Full participation history for a company (latest first)
CREATE VIEW v_company_tender_history AS
SELECT
    p.company_id,
    t.id                                                     AS tender_id,
    s.slug                                                   AS source,
    t.title,
    t.announced_date,
    t.deadline,
    t.budget,
    p.bid_amount,
    p.role,
    p.is_winner,
    p.bid_rank,
    cpv.code_normalized                                      AS primary_cpv,
    cpv.description_ka                                       AS primary_cpv_ka,
    t.url
FROM       tender_participations  p
JOIN       tenders                t   ON t.id = p.tender_id
JOIN       sources                s   ON s.id = t.source_id
LEFT JOIN  tender_cpv_codes       tc  ON tc.tender_id = t.id AND tc.is_primary
LEFT JOIN  cpv_codes              cpv ON cpv.id = tc.cpv_code_id
ORDER BY   t.announced_date DESC;

COMMENT ON VIEW v_company_tender_history IS 'Full tender participation history per company, newest first';


-- Grant read/write to the app user
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES    IN SCHEMA public TO tender_app;
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public TO tender_app;
