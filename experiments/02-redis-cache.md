# Experiment 2 — Redis cache (cache-aside + INCR)

How a Redis cache eliminates Postgres write load on the redirect hot path.

## Hypothesis

A read from Postgres is fast (now that we have an index — see experiment 01),
but **every redirect was also a write** (`UPDATE urls SET hit_count = hit_count + 1`).
At high RPS, that write storm becomes the bottleneck — and read replicas don't
help with writes.

If we move the URL lookup to a Redis cache (cache-aside) and the hit-count
update to a Redis counter (`INCR`), we expect:

1. The redirect path to be faster than the indexed Postgres path.
2. Postgres to receive **zero** load on the hot path until an explicit flush.
3. Total counts to remain correct (no lost increments, no double counts) when
   a periodic batch job moves accumulated counts from Redis into Postgres.

## Setup

- Added `redis:7-alpine` service to `docker-compose.yml` on port 6380 (host) /
  6379 (container) — 6380 was chosen to avoid colliding with another local
  Redis container.
- App connects via `REDIS_URL=redis://redis:6379/0`.
- New code in `app/main.py`:
  - `r = redis.from_url(REDIS_URL, decode_responses=True)` at module level.
  - `shorten()` pre-warms `url:{code}` -> `long_url` after `INSERT`, with TTL 1 day.
  - `follow()` does cache-aside lookup; on hit, just `INCR hits:{code}` and redirect.
  - `flush_hits()` endpoint walks `hits:*` keys with `SCAN`, atomically reads
    and deletes each with `GETDEL`, then `UPDATE urls SET hit_count = hit_count + n`.

### Negative-result caching

A cache miss for a non-existent code falls through to Postgres, finds nothing,
and writes a `__NULL__` sentinel back to Redis with a short TTL (60s). That way
a typo or scanner punching the same bad code doesn't keep punching Postgres.

## Procedure

```bash
# 1. Shorten one URL — pre-warms cache.
RESP=$(curl -s -X POST http://localhost:8000/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://en.wikipedia.org/wiki/Cache_(computing)"}')
CODE=$(echo "$RESP" | python3 -c 'import sys, json; print(json.load(sys.stdin)["short_code"])')

# 2. Confirm cache holds the URL.
docker compose exec redis redis-cli GET "url:$CODE"

# 3. Hit the redirect 100 times.
for i in $(seq 1 100); do curl -s -o /dev/null http://localhost:8000/$CODE; done

# 4. Confirm Redis counter is at 100.
docker compose exec redis redis-cli GET "hits:$CODE"

# 5. Confirm Postgres hit_count is still 0.
docker compose exec db psql -U shortener -t -c \
  "SELECT short_code, hit_count FROM urls WHERE short_code = '$CODE';"

# 6. Inspect /stats/{code} — totals come from Postgres + Redis combined.
curl -s http://localhost:8000/stats/$CODE | python3 -m json.tool

# 7. Flush.
curl -s -X POST http://localhost:8000/admin/flush-hits | python3 -m json.tool

# 8. Confirm Postgres now has 100 and Redis counter is gone.
```

## Results

### State before flush

```
Redis  url:pgX9jM   = "https://en.wikipedia.org/wiki/Cache_(computing)"
Redis  hits:pgX9jM  = 100
Postgres            hit_count = 0   ← never touched on the hot path
```

### `/stats/pgX9jM` while pending

```json
{
  "short_code": "pgX9jM",
  "long_url": "https://en.wikipedia.org/wiki/Cache_(computing)",
  "hit_count": 0,
  "pending_hits_in_redis": 100,
  "hit_count_total": 100
}
```

### Flush result

```json
{ "codes_flushed": 1, "total_hits_flushed": 100, "served_by": "app-1" }
```

### State after flush

```
Redis  hits:pgX9jM  = (deleted)
Postgres            hit_count = 100
```

### `/stats/pgX9jM` after flush

```json
{
  "short_code": "pgX9jM",
  "long_url": "https://en.wikipedia.org/wiki/Cache_(computing)",
  "hit_count": 100,
  "pending_hits_in_redis": 0,
  "hit_count_total": 100
}
```

Total stayed at 100 across the flush. **No lost increments. No double counts.**

### Latency comparison

10 sequential cache-hit redirects:

```
0.013s (cold-ish)
0.0018s
0.0018s
0.0016s
0.0038s
0.0018s
0.0019s
0.0038s
0.0036s
0.0017s
```

Median ~1.8 ms.

| Stage in this project | p50 latency | DB load per redirect |
| --------------------- | ----------- | --------------------- |
| No index, no cache    | ~32 ms      | full table scan + UPDATE |
| Indexed Postgres      | ~7-13 ms    | indexed scan + UPDATE |
| **Index + Redis**     | **~1.8 ms** | **zero on hot path**  |

Roughly 5× faster than indexed Postgres, and orders of magnitude lower DB
load — which matters far more than wall-clock latency at scale.

## Why this works (the design intuition)

- Reads at high RPS are dominated by a small set of "hot" codes.  Those fit in
  RAM trivially.  Cache-aside hits ~99% of the time once the cache warms up.
- Hit counts are **append-only and commutative**.  Adding 1 a hundred times
  in Redis, then doing `UPDATE hit_count = hit_count + 100` in Postgres yields
  the same final value as 100 individual UPDATEs.  Batch is exactly equivalent.
- `GETDEL` is the magic that makes the flush race-free: the counter is read
  and removed atomically.  Any new hits that arrive during/after the flush
  start a fresh counter from 0 and get picked up on the next flush.

## What we traded

- **Hit-count freshness.**  Postgres lags by up to one flush interval (manual
  in this build; would be a 60-second background task in production).  Fine
  for analytics.  Lethal for real-time billing or quotas.
- **A new dependency.**  Redis is now in the request path; if Redis dies and
  the cache is cold, the app falls through to Postgres and write load returns.
  Production setups would run Redis HA (Sentinel or Cluster).
- **Cache invalidation surface.**  We don't allow editing `long_url` in this
  project, so it's not a concern here.  In a real system, any update to the
  source of truth would need to invalidate the matching cache key.

## Next experiments this enables

- Cache stampede mitigation: deliberately expire a hot key, fire 1000
  concurrent requests, watch Postgres get hit 1000 times — then add
  single-flight (lock-on-miss) and watch Postgres get hit exactly once.
- Switch the flush from a manual endpoint to a background task on a 60s
  timer using FastAPI's `lifespan` events or `apscheduler`.
- Convert this into a real horizontal-scaling demo with nginx + 3 instances:
  show that all instances see the same cache state because they share Redis.
