# Experiment 3 — Horizontal scaling with nginx + 3 backends

How a stateless service horizontally scales behind a reverse-proxy load
balancer, and how it survives one of the backends dying.

## Hypothesis

If the FastAPI app is genuinely stateless (no per-instance memory state — all
session/cache state lives in the shared Redis and Postgres), then:

1. We can run **N identical instances** of it.
2. Put nginx in front as an **L7 reverse proxy** with round-robin.
3. **Any instance can serve any request.** Traffic should distribute evenly.
4. Killing an instance should not break the service — nginx should detect
   the failure and route around the dead one.
5. When the killed instance recovers, traffic should rebalance.

## Setup

`docker-compose.yml` was changed from one `app` service to **three** services
(`app1`, `app2`, `app3`), each with a unique `APP_ID` env var so we can see
which instance answered.

`nginx/nginx.conf` defines an upstream pool:

```nginx
upstream backend {
    server app1:8000 max_fails=3 fail_timeout=10s;
    server app2:8000 max_fails=3 fail_timeout=10s;
    server app3:8000 max_fails=3 fail_timeout=10s;
}
```

- **Default algorithm:** round-robin.
- **Passive health check:** `max_fails=3 fail_timeout=10s`. If an upstream
  fails 3 requests within 10 seconds, nginx marks it down for the next
  10 seconds, then re-attempts.

Open-source nginx doesn't ship active probes — those are a feature of
nginx-plus or kubernetes-ingress.  Passive checks are good enough for this
demo.

The three apps share the same Postgres and Redis services, which is exactly
what makes them stateless from nginx's perspective.

### One DNS gotcha worth noting

Open-source nginx resolves upstream hostnames **at config-load time**.  If
any of `app1`, `app2`, `app3` aren't in Docker's DNS yet at that moment,
nginx refuses to start. We add `restart: on-failure:5` on the nginx service
so it retries until all three are resolvable. (`resolver 127.0.0.11` is set
in the config too, but that only governs runtime resolution, not startup.)

## Test 1 — Round-robin with all 3 backends healthy

```bash
for i in $(seq 1 30); do
  curl -s http://localhost:8000/health \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["served_by"])'
done | sort | uniq -c
```

Result:

```
  10 app-1
  10 app-2
  10 app-3
```

**Perfect round-robin.** 30 requests split exactly 3 ways. nginx is rotating
through the pool deterministically.

## Test 2 — Failover with a dead backend

```bash
docker compose stop app2
# 30 requests
```

Result:

```
  14 app-1
   0 app-2
  16 app-3
```

- app-2 received zero traffic.  After the first few requests timed out, nginx
  marked it down for `fail_timeout=10s` and stopped sending it requests.
- The 30 requests still all succeeded — nginx's default
  `proxy_next_upstream error timeout` automatically retries a failed request
  on a healthy upstream, so the client never saw an error.
- Distribution between app-1 and app-3 is round-robin within the remaining
  pool.

## Test 3 — Recovery

```bash
docker compose start app2
sleep 5
# 30 more requests
```

Result:

```
  14 app-1
   2 app-2
  14 app-3
```

- app-2 is back in the rotation but the `fail_timeout=10s` window means it
  takes a few seconds for nginx to fully trust it again.
- A subsequent run without the down/up cycle returns to the clean 10/10/10
  split.

## What this demonstrates

| Concept (from notes)          | What you see                                           |
|-------------------------------|--------------------------------------------------------|
| **Statelessness**             | All 3 backends share the same Redis/Postgres; any one can serve any request. |
| **Reverse proxy / L7 LB**     | nginx is the public entrypoint; apps are network-internal only. |
| **Round-robin**               | 30 requests -> 10/10/10 with all healthy.              |
| **Passive health check**      | `max_fails` + `fail_timeout` pull a bad upstream out automatically. |
| **`proxy_next_upstream`**     | Failures are retried on a healthy backend; clients see no errors. |
| **Failure isolation**         | One instance dying does not break the service.         |

## Why nginx, not the app, makes this work

Notice that `app/main.py` didn't change at all between sessions 2 and 3.

The horizontal-scaling story is **purely an infrastructure change**: adding
the LB and replicating the existing stateless app. This is the payoff of
making the app stateless from day 1 — scaling out is a deployment-time
decision, not an application-code rewrite.

## How to reproduce

```bash
# Bring up the stack — db, redis, app1, app2, app3, nginx
docker compose up -d

# Test 1 — even distribution
for i in $(seq 1 30); do
  curl -s http://localhost:8000/health \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["served_by"])'
done | sort | uniq -c

# Test 2 — kill app2, retest
docker compose stop app2
for i in $(seq 1 30); do
  curl -s http://localhost:8000/health \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["served_by"])'
done | sort | uniq -c

# Test 3 — recover
docker compose start app2
sleep 5
for i in $(seq 1 30); do
  curl -s http://localhost:8000/health \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["served_by"])'
done | sort | uniq -c
```
