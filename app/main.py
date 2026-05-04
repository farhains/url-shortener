"""
Mini URL Shortener — single-instance backend with Redis cache.

Endpoints:
  POST /shorten              -> { short_code, short_url }
  GET  /{code}               -> 301 redirect to long_url
  GET  /health               -> liveness probe
  GET  /stats/{code}         -> hit count + metadata
  POST /admin/flush-hits     -> sweep Redis hit counters into Postgres

Caching strategy:
  - URL lookups use cache-aside (Redis).  Most reads stop at Redis.
  - Hit counts use Redis INCR.  We do NOT update Postgres on each redirect.
  - A periodic flush job moves accumulated counts from Redis to Postgres
    in a batch.  For this learning project the flush is exposed as an
    explicit endpoint instead of a background task.
"""
import os
import random
import string
from contextlib import contextmanager

import psycopg2
import redis as redis_client
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
APP_ID = os.environ.get("APP_ID", "app-?")

SHORT_CODE_LEN = 6
ALPHABET = string.ascii_letters + string.digits  # base62

URL_CACHE_TTL = 86400          # cache short_code -> long_url for 1 day
URL_KEY = "url:{code}"         # cache key for the URL itself
HITS_KEY = "hits:{code}"       # counter key for accumulated hits since last flush
NULL_MARKER = "__NULL__"       # sentinel for "this code does not exist"
NULL_TTL = 60                  # cache 404s briefly to absorb 404 stampedes

app = FastAPI(title="Mini URL Shortener")

# Single Redis connection — the client is thread-safe and pools internally.
r = redis_client.from_url(REDIS_URL, decode_responses=True)


# ---------- DB helpers ----------

@contextmanager
def db_cursor():
    """Yield a psycopg2 cursor. Commits on success, rolls back on error."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generate_short_code() -> str:
    return "".join(random.choices(ALPHABET, k=SHORT_CODE_LEN))


# ---------- API ----------

class ShortenRequest(BaseModel):
    url: HttpUrl


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str
    served_by: str


@app.get("/health")
def health():
    """Liveness probe — proves the process is running."""
    return {"status": "ok", "served_by": APP_ID}


@app.post("/shorten", response_model=ShortenResponse, status_code=201)
def shorten(req: ShortenRequest, request: Request):
    """Create a short code for the given long URL."""
    long_url = str(req.url)

    # Generate a code; retry on collision (the unique index will surface it).
    for _ in range(5):
        code = generate_short_code()
        try:
            with db_cursor() as cur:
                cur.execute(
                    "INSERT INTO urls (short_code, long_url) VALUES (%s, %s)",
                    (code, long_url),
                )
            break
        except psycopg2.IntegrityError:
            continue
    else:
        raise HTTPException(status_code=500, detail="could not generate unique code")

    # Pre-warm the cache so the first redirect skips Postgres.
    r.set(URL_KEY.format(code=code), long_url, ex=URL_CACHE_TTL)

    base = f"{request.url.scheme}://{request.url.netloc}"
    return ShortenResponse(
        short_code=code,
        short_url=f"{base}/{code}",
        served_by=APP_ID,
    )


@app.get("/{code}")
def follow(code: str):
    """Resolve a short code and redirect (301).  Cache-aside on lookup, INCR on hit."""
    cache_key = URL_KEY.format(code=code)
    long_url = r.get(cache_key)

    if long_url is None:
        # Cache miss -> ask Postgres.
        with db_cursor() as cur:
            cur.execute(
                "SELECT long_url FROM urls WHERE short_code = %s",
                (code,),
            )
            row = cur.fetchone()

        if not row:
            # Cache the 404 briefly so a typo doesn't keep punching Postgres.
            r.set(cache_key, NULL_MARKER, ex=NULL_TTL)
            raise HTTPException(status_code=404, detail="not found")

        long_url = row["long_url"]
        r.set(cache_key, long_url, ex=URL_CACHE_TTL)

    elif long_url == NULL_MARKER:
        raise HTTPException(status_code=404, detail="not found")

    # Increment hit count in Redis.  Postgres is NOT touched on the hot path.
    r.incr(HITS_KEY.format(code=code))

    return RedirectResponse(url=long_url, status_code=301)


@app.get("/stats/{code}")
def stats(code: str):
    """Hit count + metadata. Combines Postgres (flushed) + Redis (pending) counts."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT short_code, long_url, hit_count, created_at "
            "FROM urls WHERE short_code = %s",
            (code,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    pending = int(r.get(HITS_KEY.format(code=code)) or 0)
    row["pending_hits_in_redis"] = pending
    row["hit_count_total"] = int(row["hit_count"]) + pending
    return row


@app.post("/admin/flush-hits")
def flush_hits():
    """Move all accumulated Redis hit counters into Postgres in a batch."""
    flushed = 0
    total_hits = 0

    # SCAN iterates without blocking the whole keyspace.
    for key in r.scan_iter(match="hits:*"):
        code = key.split(":", 1)[1]
        # GETDEL is atomic: read the count and remove the key in one step.
        n = r.getdel(key)
        if n is None:
            continue
        n = int(n)

        with db_cursor() as cur:
            cur.execute(
                "UPDATE urls SET hit_count = hit_count + %s WHERE short_code = %s",
                (n, code),
            )
        flushed += 1
        total_hits += n

    return {
        "codes_flushed": flushed,
        "total_hits_flushed": total_hits,
        "served_by": APP_ID,
    }
