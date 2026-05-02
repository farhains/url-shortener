"""
Mini URL Shortener — single-instance backend.

Endpoints:
  POST /shorten     -> { short_code, short_url }
  GET  /{code}      -> 301 redirect to long_url
  GET  /health      -> liveness probe
  GET  /stats/{code} -> view hit count

This is the Day-1 build. We will add nginx + multiple instances + Redis
+ rate limiting in later sessions.
"""
import os
import random
import string
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

DATABASE_URL = os.environ["DATABASE_URL"]
APP_ID = os.environ.get("APP_ID", "app-?")
SHORT_CODE_LEN = 6
ALPHABET = string.ascii_letters + string.digits  # base62

app = FastAPI(title="Mini URL Shortener")


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

    # Generate a code; retry on the (very rare) collision.
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

    base = f"{request.url.scheme}://{request.url.netloc}"
    return ShortenResponse(
        short_code=code,
        short_url=f"{base}/{code}",
        served_by=APP_ID,
    )


@app.get("/{code}")
def follow(code: str):
    """Resolve a short code and redirect (301)."""
    with db_cursor() as cur:
        # Note: there's no index on short_code yet — this is a full table scan.
        # We will add the index later and time the difference.
        cur.execute(
            "UPDATE urls SET hit_count = hit_count + 1 "
            "WHERE short_code = %s "
            "RETURNING long_url",
            (code,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return RedirectResponse(url=row["long_url"], status_code=301)


@app.get("/stats/{code}")
def stats(code: str):
    """Inspect hit count and metadata for a code."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT short_code, long_url, hit_count, created_at "
            "FROM urls WHERE short_code = %s",
            (code,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row
