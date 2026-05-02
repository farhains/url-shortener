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

**Day 1 build — single backend + Postgres only.**

- `POST /shorten` -> creates a short code, returns it
- `GET  /{code}` -> 301 redirect to the original URL
- `GET  /health` -> liveness probe
- `GET  /stats/{code}` -> view hit count and metadata

Postgres is up. The `urls.short_code` column is **intentionally not indexed** —
we will add the index later and measure the speedup.

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

# Follow (returns a 301)
curl -i http://localhost:8000/<short_code>

# Stats
curl http://localhost:8000/stats/<short_code>
```

## Roadmap

| Session | Adds | Concept |
|---|---|---|
| 1 (now) | Postgres + 1 FastAPI | Stateless backend, basic CRUD |
| 2 | nginx + 3 FastAPI instances | L7 reverse proxy + horizontal scaling + LB |
| 3 | Redis cache (cache-aside) | Caching patterns, hit rate measurement |
| 4 | The index experiment | Full table scan -> indexed lookup speedup |
| 5 | Rate limiting + idempotency keys | API gateway patterns |
| 6 | TLS via self-signed cert | TLS handshake observable in `curl -v` |
| 7 | Health probes + graceful shutdown | LB health checks, rolling deploys |
