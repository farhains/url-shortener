# Experiment 1 — Indexing

The classic full-table-scan vs B-tree-lookup demonstration.

## Hypothesis

Adding a B-tree index on `urls.short_code` will dramatically speed up
`WHERE short_code = ?` lookups, because Postgres will jump straight to the
matching row via an O(log n) tree walk instead of scanning every row.

## Setup

- 500,002 rows in `urls` (2 from earlier curls + 500,000 generated).
- Generated rows have predictable codes: `000001`, `000002`, ..., `500000`.
- Same Postgres instance, same query, same hardware — only the index changes.

## Measurement query

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT long_url FROM urls WHERE short_code = '250000';
```

We picked `250000` to land in the middle of the table — fair test, not biased
toward a row that happens to be at the start.

## Results

### Without an index

```
Parallel Seq Scan on urls
  Workers Planned: 2
  Workers Launched: 2
  Filter: ((short_code)::text = '250000'::text)
  Rows Removed by Filter: 166667     ← per worker
  Buffers: shared hit=5682           ← read 5682 pages
Execution Time: 12.735 ms
```

Postgres had no choice — without an index it does a sequential scan, parallelized
across 2 worker processes. Each worker examined ~166k rows. Total ~500k rows
read to find one match.

### With `CREATE UNIQUE INDEX idx_urls_short_code ON urls(short_code)`

```
Index Scan using idx_urls_short_code on urls
  Index Cond: ((short_code)::text = '250000'::text)
  Buffers: shared hit=1 read=3
Execution Time: 0.101 ms
```

Index Scan with `Index Cond` (not `Filter`) — Postgres jumped straight to the
match via the B-tree. 4 page reads total: a few B-tree levels plus the leaf.

### Headline numbers

| Metric                      | Without index | With index   | Change          |
| --------------------------- | ------------- | ------------ | --------------- |
| Plan node                   | Seq Scan      | Index Scan   | —               |
| Pages read                  | 5,682         | 4            | ~1,400× fewer   |
| Rows examined               | ~500,000      | 1            | 500,000× fewer  |
| Execution time              | **12.735 ms** | **0.101 ms** | **~125× faster**|

### Wall-clock through the API

5 calls each to `GET /250000` (which does an `UPDATE ... RETURNING`):

| Run         | Without index  | With index    |
| ----------- | -------------- | ------------- |
| First call  | 135 ms (cold)  | 38 ms (cold)  |
| Steady call | ~32 ms         | ~7-13 ms      |
| Speedup     | —              | **3-5×**      |

The wall-clock speedup is smaller than the DB-level speedup because the API call
includes fixed overhead (network, Python parsing, FastAPI routing, the UPDATE's
WAL write) that the index doesn't help with.

## Why the gap grows with table size

The unindexed Seq Scan is **O(n)** — time grows linearly with table size.
The B-tree Index Scan is **O(log n)** — time grows barely at all.

| Rows         | Seq Scan (extrapolated) | Index Scan (extrapolated) |
| ------------ | ----------------------- | ------------------------- |
| 500K (this)  | ~13 ms                  | ~0.1 ms                   |
| 5 million    | ~130 ms                 | ~0.15 ms                  |
| 50 million   | ~1300 ms                | ~0.2 ms                   |

This is why "add an index" is the cheapest, highest-leverage scaling move on
read-heavy paths.

## Side benefit

The index is `UNIQUE`, which now enforces uniqueness at the DB level. The
collision-retry loop in `app/main.py`'s `shorten()` function:

```python
for _ in range(5):
    code = generate_short_code()
    try:
        cur.execute("INSERT ...", (code, long_url))
        break
    except psycopg2.IntegrityError:
        continue
```

actually works as designed — duplicate codes raise `IntegrityError`, the loop
retries with a fresh code. Without the unique constraint, duplicates would have
been silently inserted.

## Cost paid

Indexes aren't free:

- **Disk space.** The index is its own B-tree stored alongside the table.
- **Slower writes.** Every `INSERT` and every `UPDATE` of `short_code` now also
  has to update the B-tree. For mostly-read workloads (like ours), this is a
  great trade.

## How to reproduce

```bash
# Bulk insert 500k rows
docker compose exec db psql -U shortener -c "
  INSERT INTO urls (short_code, long_url)
  SELECT lpad(s::text, 6, '0'), 'https://example.com/page/' || s
  FROM generate_series(1, 500000) s;
"

# Drop the existing index (this experiment requires not having it)
docker compose exec db psql -U shortener -c "
  DROP INDEX IF EXISTS idx_urls_short_code;
"

# Measure without index
docker compose exec db psql -U shortener -c "
  EXPLAIN (ANALYZE, BUFFERS) SELECT long_url FROM urls WHERE short_code = '250000';
"

# Add index
docker compose exec db psql -U shortener -c "
  CREATE UNIQUE INDEX idx_urls_short_code ON urls(short_code);
"

# Measure again
docker compose exec db psql -U shortener -c "
  EXPLAIN (ANALYZE, BUFFERS) SELECT long_url FROM urls WHERE short_code = '250000';
"
```
