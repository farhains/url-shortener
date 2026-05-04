# Mini URL Shortener

A small URL shortener built incrementally to demonstrate system design concepts in
practice — networking, scaling, load balancing, caching, indexing, and more.

This is a **learning project**. Each session adds one production-grade layer and
shows the concept in action.

## Architecture (target)

```
  Browser / curl
       |
       v   HTTPS
   [ nginx ]                <- reverse proxy + L7 LB + TLS
       |
       v   HTTP, round-robin
[ FastAPI x3 ]              <- horizontally-scaled stateless backends
       |
       v
   [ Redis ]                <- cache (cache-aside)
       |
       v   on miss
  [ Postgres ]              <- source of truth + index experiment
```

## Where we are now

**Day 3 build — Postgres (with index) + Redis cache.**

- `POST /shorten` -> creates a short code, pre-warms Redis cache
- `GET  /{code}` -> cache-aside lookup; `INCR` on a Redis counter; **never touches Postgres on the hot path**
- `GET  /health` -> liveness probe
- `GET  /stats/{code}` -> combines Postgres count + Redis pending count (true total)
- `POST /admin/flush-hits` -> sweeps Redis counters into Postgres in a batch

Redirects now serve in **~1-2 ms** (cache hit) vs ~7-13 ms before (indexed Postgres) vs ~32 ms before that (full table scan).
See [experiments/01-indexing.md](experiments/01-indexing.md) and
[experiments/02-redis-cache.md](experiments/02-redis-cache.md) for the
before/after measurements.

## Run locally

```bash
docker compose up --build
```

The API is on http://localhost:8000.

## Try it

```bash
# Shorten
curl -s -X POST http://localhost:8000/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/some/long/path"}'

# Follow (returns a 301) — served from Redis after the first call
curl -i http://localhost:8000/<short_code>

# Stats — see the postgres / redis split
curl http://localhost:8000/stats/<short_code>

# Move pending hit counts from Redis into Postgres
curl -X POST http://localhost:8000/admin/flush-hits
```

## Roadmap

| Session | Adds | Concept | Status |
|---|---|---|---|
| 1 | Postgres + 1 FastAPI | Stateless backend, basic CRUD | done |
| 2 | The index experiment | Full table scan -> indexed lookup speedup | done |
| 3 | Redis cache + INCR + flush | Cache-aside, write-back counters | done |
| 4 | nginx + 3 FastAPI instances | L7 reverse proxy + horizontal scaling + LB | next |
| 5 | Rate limiting + idempotency keys | API gateway patterns | |
| 6 | TLS via self-signed cert | TLS handshake observable in `curl -v` | |
| 7 | Health probes + graceful shutdown | LB health checks, rolling deploys | |
