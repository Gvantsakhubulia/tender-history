-- Run this script connected to 'tender_aggregator' DB.
-- Creates the queue that tracks tenders needing a re-scrape after their deadline
-- to collect bidder and winner information.

CREATE TABLE tender_rescrape_queue (
    id              SERIAL        PRIMARY KEY,
    tender_id       INT           NOT NULL REFERENCES tenders (id) ON DELETE CASCADE,
    external_id     VARCHAR(100)  NOT NULL,
    source_id       SMALLINT      NOT NULL REFERENCES sources (id),
    deadline        DATE          NOT NULL,
    rescrape_after  DATE          NOT NULL,  -- deadline + 1 day; rescrape once this passes
    status          VARCHAR(20)   NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'failed')),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    error           TEXT,
    UNIQUE (tender_id)
);

COMMENT ON TABLE  tender_rescrape_queue IS 'Tenders queued for re-scraping after deadline to collect bidder/winner data';
COMMENT ON COLUMN tender_rescrape_queue.rescrape_after IS 'Re-scrape once this date is reached (deadline + 1 day)';
COMMENT ON COLUMN tender_rescrape_queue.status         IS 'pending → done/failed after processing';

CREATE INDEX idx_rescrape_queue_pending ON tender_rescrape_queue (rescrape_after)
    WHERE status = 'pending';
