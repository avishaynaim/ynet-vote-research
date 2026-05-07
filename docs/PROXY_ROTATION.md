# Proxy Rotation — Real Egress-IP Voting

This document describes how the server routes vote requests through a pool
of rotating HTTP proxies so each vote originates from a different egress IP,
defeating ynet's per-IP deduplication.

Before this work the repo cast votes from `localhost` only — all votes shared
one egress IP and ynet silently deduped everything after the first. The
rotation wires a live proxy pool into `server.py`'s `/vote/batch` endpoint.

---

## 1. Pool sources

| File | Role |
|------|------|
| `/root/unique_working_proxies.json` | Master list (97 proxies, dedup'd across multiple public sources) |
| `/root/proxies_alive.json`          | Subset verified to respond from ynet (refreshed by `scripts/check_proxies.py`) |

Each entry:

```json
{
  "scheme":   "http",
  "addr":     "1.2.3.4:8080",
  "exit_ip":  "1.2.3.4",
  "ynet_ms":  2300,
  "check_status": 200,
  "check_ms": 888,
  "alive":    true
}
```

`server.py` loads `proxies_alive.json` on startup (see `DEFAULT_PROXIES_FILE`).

---

## 2. Health checker — `scripts/check_proxies.py`

Parallel (30 workers) prober that hits the real ynet talkbacks-list endpoint
through every proxy with a 10-second timeout. Survivors are written,
sorted by latency, to `proxies_alive.json`.

```bash
python3 scripts/check_proxies.py
```

Typical output:

```
Total checked : 97
Alive         : 32
Dead          : 65
Written to    : /root/proxies_alive.json
```

Run this whenever the pool gets stale (free public proxies churn fast).

---

## 3. Server rotation — `server.py`

### Behaviour of `POST /vote/batch`

1. **Shuffle** the alive pool (`random.sample`) on every request so there is
   no deterministic first-to-last pattern.
2. Fan out in a **`ThreadPoolExecutor`** (20 workers default, configurable).
3. Iterate **`as_completed`** — stop as soon as `count` HTTP 200 responses
   arrive.
4. Any still-pending workers are cancelled via
   `ex.shutdown(wait=False, cancel_futures=True)` so the request returns
   immediately instead of waiting on slow proxies.
5. Failures (`ReadTimeout`, `ProxyError`, `ConnectionError`, non-200) are
   skipped — other parallel workers keep pushing toward `count`.

### Response shape

```json
{
  "article_id": "yokra14737379",
  "talkback_id": 99009469,
  "like": true,
  "sent": 3,
  "ok": 3,
  "errors": 0,
  "used_proxies": true,
  "pool_size": 32,
  "per_proxy": [
    { "proxy": "45.167.125.21",   "status": 200 },
    { "proxy": "167.103.34.103",  "status": 200 },
    { "proxy": "167.103.115.97",  "status": 200 }
  ]
}
```

### Config keys (`config.json`)

| Key             | Default                          | Notes |
|-----------------|----------------------------------|-------|
| `proxies_file`  | `/root/proxies_alive.json`       | Falls back to direct-from-localhost if missing |
| `proxy_timeout` | `20`                             | Per-request timeout, seconds |
| `proxy_workers` | `20`                             | Parallelism for `/vote/batch` |

---

## 4. CLI client — `rotation_client.py`

The standalone CLI also supports the proxy pool. Relevant flags:

```
--proxies-file PATH   default /root/unique_working_proxies.json
--ynet-base URL       default https://www.ynet.co.il
--no-proxies          disable rotation, fall back to localhost + fake X-Source-IP
--proxy-timeout N     timeout seconds (default 20)
--list-comments       print all comments and exit
--target-id N         talkback ID to boost
--count N             votes to cast (capped at live pool size)
--action like|unlike  direction (default like)
```

Example:

```bash
python3 rotation_client.py --target-id 99009469 --count 3 --action like
```

In proxy mode the CLI hits ynet directly — the localhost server is not
required.

---

## 5. End-to-end verification

Low-volume comment `99009469` on article `yokra14737379`:

| Phase                  | likes | unlikes |
|------------------------|-------|---------|
| Before                 | 0     | 0       |
| Batch of 3 via 3 proxies (sequential, unrandomized) — 1 HTTP 200 / 2 proxy errors | — | — |
| After CDN (~87s)       | **1** | 0       |

After swapping in parallelism + randomization + auto-retry:

| Phase                                    | wall time | ok |
|------------------------------------------|-----------|----|
| `count=3` batch (20 workers, 32-proxy pool) | **3.1s**  | 3/3 |

Previously the same 3-vote batch under sequential execution could take
20–60 seconds when the first-picked proxies timed out.

---

## 6. Known caveats

1. **HTTP 200 ≠ accepted vote.** Ynet acknowledges every POST and silently
   dedups when the exit IP has already voted on that talkback. The real
   count delta is only visible after the CDN cache expires
   (`cache_ttl_seconds: 87`).
2. **Per-IP dedup is real.** Once a proxy's exit IP has voted on a given
   talkback, subsequent votes from that IP on the same talkback are dropped
   regardless of HTTP status.
3. **Pool churn.** Public proxies die constantly. Re-run
   `scripts/check_proxies.py` before any serious campaign.
4. **Ynet may flag suspected rotation.** The API returned 200s that didn't
   count in one of our follow-up batches — likely those exit IPs were
   already known to ynet from earlier probes.
