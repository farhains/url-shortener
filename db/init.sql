-- Mini URL Shortener — schema
-- IMPORTANT: short_code is intentionally NOT indexed yet.
-- We will add the index later and measure the speedup. That's the experiment.

CREATE TABLE urls (
    id          BIGSERIAL PRIMARY KEY,
    short_code  VARCHAR(10) NOT NULL,
    long_url    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count   BIGINT NOT NULL DEFAULT 0
);

-- We deliberately do NOT add an index on short_code here.
-- Lookup queries (WHERE short_code = ?) will do a full table scan.
-- Run `EXPLAIN ANALYZE` to see this in action.
