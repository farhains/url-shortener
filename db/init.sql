-- Mini URL Shortener — schema

CREATE TABLE urls (
    id          BIGSERIAL PRIMARY KEY,
    short_code  VARCHAR(10) NOT NULL,
    long_url    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count   BIGINT NOT NULL DEFAULT 0
);

-- Unique B-tree index on short_code.
-- Two reasons:
--   1. Lookups by short_code (WHERE short_code = ?) become O(log n) instead of full table scan.
--   2. Enforces uniqueness so the collision-retry loop in shorten() actually triggers.
-- See experiments/01-indexing.md for the before/after measurements that motivated this.
CREATE UNIQUE INDEX idx_urls_short_code ON urls(short_code);
