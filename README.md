# Mini URL Shortener

A URL shortener built incrementally to test specific system-design hypotheses.
Not just CRUD — every layer is added with a measurement attached.

**Stack:** Python 3.12 / FastAPI / PostgreSQL 16 / Redis 7 / nginx / Docker Compose.

## Architecture

```
   curl / browser
        |
        v   HTTP :8000
   [   nginx   ]                <-  L7 reverse proxy, round-robin LB
        |
        v   round-robin
[ app-1, app-2, app-3 ]         <-  3 stateless FastAPI backends
        |
        +-----------+
        |           |
        v           v
   [  Redis  ]   [ Postgres ]   <-  shared cache + source of truth
```

## Latency ladder (measured, not guessed)

| Iteration | p50 redirect latency | DB load per redirect |
|-----------|---------------------:|----------------------|
| Day 1 — no index, no cache | ~32 ms | full table scan + UPDATE |
| Day 2 — added unique B-tree index on `short_code` | ~7-13 ms | indexed scan + UPDATE |
| Day 3 — added Redis cache + `INCR` counter | **~1.8 ms** | **0 on hot path** |

Each iteration is documented end-to-end in [`/experiments`](experiments/):
- [`01-indexing.md`](experiments/01-indexing.md) — `Seq Scan` 12.7ms → `Index Scan` 0.1ms (~125× DB-level speedup) on a 500K-row table.
- [`02-redis-cache.md`](experiments/02-redis-cache.md) — cache-aside + `INCR` + atomic `GETDEL` flush; 100 redirects, zero Postgres writes on the hot path.
- [`03-horizontal-scaling.md`](experiments/03-horizontal-scaling.md) — 30 requests → 10/10/10 round-robin; one backend killed → traffic rebalances; client sees no errors.

## Endpoints

| Method | Path                  | Purpose                                                         |
|--------|-----------------------|-----------------------------------------------------------------|
| POST   | `/shorten`            | Create a short code; pre-warms Redis cache.                     |
| GET    | `/{code}`             | 301 redirect; cache-aside lookup, `INCR` for hit count.         |
| GET    | `/health`             | Liveness probe (used by nginx for passive health checks).       |
| GET    | `/stats/{code}`       | Hit count: combines Postgres (flushed) + Redis (pending).       |
| POST   | `/admin/flush-hits`   | Move accumulated Redis counters into Postgres in a batch.       |

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
| 4 | nginx + 3 FastAPI backends | L7 reverse proxy + horizontal scaling + LB | done |
| 5 | Rate limiting + idempotency keys | API gateway patterns | |
| 6 | Cache stampede + single-flight | Failure modes you've designed against | |
| 7 | TLS via self-signed cert | TLS handshake observable in `curl -v` | |
| 8 | Health/readiness split + graceful shutdown | K8s-grade probes | |
