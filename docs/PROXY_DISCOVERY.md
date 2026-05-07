# Proxy Discovery — Scaling the Pool from 97 to 500+

**Date:** 2026-04-15
**Goal:** grow the working-proxy pool past 500 fresh, ynet-validated entries.
**Starting state:** 97 proxies in `proxies/unique.json` from the original pass.
**Result:** 500 newly-confirmed working proxies, merged with the prior 97
into `proxies/unique_v2.json` (597 addresses, 369 with verified unique exit IP).

This file documents the method end-to-end so the run is reproducible —
including the *failure modes* we hit, since they shape the design.

---

## 1. Why the obvious approach failed first

The first attempt threw a `ThreadPoolExecutor` with **500 workers** at the
candidate list. On a proot-distro Ubuntu container (no real init, limited
kernel resources) this killed the process with:

```
The futex facility returned an unexpected error code.
Killed
```

That is the kernel rejecting new futex allocations under heavy concurrent
thread load — the classic symptom of OOM-adjacent thread exhaustion in a
namespaced environment. Every Python thread costs ~8 MB of stack plus
sync primitives; 500 of them in a constrained container is past the limit.

**Lesson.** In proot/container environments do not assume the host's
thread headroom. Either cap workers aggressively (~80) or move off
threads entirely.

---

## 2. Architecture: async, not threads

Switched to `asyncio` + `aiohttp` + `aiohttp-socks`. Coroutines share a
single OS thread and event-loop stack, so 200 concurrent probes consume
~50 MB RSS instead of 500 MB+.

```
candidates.txt  ──►  asyncio.Queue (size = concurrency * 4)
                          │
                          ▼
            200 worker coroutines (Semaphore-gated)
                          │
                          ▼
       ProxyConnector → aiohttp.ClientSession → ynet
                          │
                          ▼
            best-effort exit-IP via api.ipify.org
                          │
                          ▼
            hits_checkpoint.json (atomic rename every 10 hits)
```

Driver: [`scripts/discovery/probe_safe.py`](../scripts/discovery/probe_safe.py).
Originals also live in `/root` since they operate on raw candidate dumps
in `/root/sources/`.

Key choices:

| Concern | Decision |
|---|---|
| Concurrency | 200 (semaphore-bounded; queue maxsize = `conc * 4` to avoid memory blowup from a 342K-line file) |
| HTTP timeout | 9s per request (most working proxies respond in 1–9s) |
| ipify timeout | 5s, **best-effort** (proxy still counted as working if ipify itself fails) |
| Validation | HTTP 200 + body contains any of `rss`, `talkback`, `success`, `<?xml` (read first 1200 bytes only) |
| Dedup | by `addr` always; by `exit_ip` when present |
| Checkpoint | `hits_checkpoint.json` flushed every 10 hits via temp + atomic rename |
| Resume | `probe_cursor.txt` integer line offset into `candidates.txt` |
| Signals | SIGINT/SIGTERM → set stop flag, save checkpoint, exit |

---

## 3. Candidate sources

The pool is the union of every public proxy aggregator we could find,
deduplicated at the (scheme, addr) level. **342,237 unique candidates**
in the final v3 build.

| Repo | Path | Schemes |
|---|---|---|
| TheSpeedX/PROXY-List | `socks4.txt`, `socks5.txt` | socks4, socks5 |
| TheSpeedX/SOCKS-List | mirror | socks4, socks5 |
| monosans/proxy-list | `http.txt`, `socks4.txt`, `socks5.txt` | http, socks4, socks5 |
| clarketm/proxy-list | `proxy-list-raw.txt` | http |
| ShiftyTR/Proxy-List | `http.txt`, `socks4.txt`, `socks5.txt` | http, socks4, socks5 |
| jetkai/proxy-list | proxy lists by scheme | http, socks4, socks5 |
| roosterkid/openproxylist | `HTTPS_RAW.txt`, `SOCKS4_RAW.txt`, `SOCKS5_RAW.txt` | http, socks4, socks5 |
| vakhov/fresh-proxy-list | `http.txt`, `socks4.txt`, `socks5.txt` | http, socks4, socks5 |
| zloi-user/hideip.me | `http.txt`, `socks4.txt`, `socks5.txt` | http, socks4, socks5 |
| MuRongPIG/Proxy-Master | `http.txt`, `socks4.txt`, `socks5.txt` | http, socks4, socks5 |
| sunny9577/proxy-scraper | `proxies.txt` | http |
| proxifly/free-proxy-list | by scheme | http, socks4, socks5 |
| mmpx12/proxy-list | by scheme | http, socks4, socks5 |
| hookzof/socks5_list | `proxy.txt` | socks5 |
| ProxyScrape API | `protocol=http\|socks4\|socks5&timeout=5000` | http, socks4, socks5 |
| Plus a dozen mirrors | `/root/sources/*.txt`, `/root/sources/b2/*.txt` | mixed |

Builder: [`scripts/discovery/build_candidates.py`](../scripts/discovery/build_candidates.py)

```
unique candidates: 342,237
excluded (already in unique.json): 753
scheme breakdown:
  http:   115,272
  socks4:  99,031
  socks5: 127,934
```

The list is **shuffled with a fixed seed** (`random.seed(1337)`) before
being written, so the probe sees a uniform mix of sources rather than
processing one cluster at a time. This matters because individual
sources are highly skewed in quality — a single low-quality mirror can
otherwise dominate the head of the file and produce a misleading early
yield estimate.

---

## 4. The probe loop

```python
# /root/probe_safe.py — heart of the validation step
async def probe_one(scheme, addr, ynet_timeout, ip_timeout):
    conn = ProxyConnector.from_url(f"{scheme}://{addr}", rdns=True)
    async with aiohttp.ClientSession(connector=conn,
                                     timeout=aiohttp.ClientTimeout(total=ynet_timeout),
                                     trust_env=False) as s:
        async with s.get(YNET_URL, headers=HEADERS) as r:
            if r.status != 200: return None
            body = await r.content.read(1200)
            if not any(t in body for t in (b"rss", b"talkback",
                                           b"success", b"<?xml")):
                return None
        # best-effort exit IP
        ip = None
        try:
            async with s.get(IPIFY,
                             timeout=aiohttp.ClientTimeout(total=ip_timeout)) as r:
                if r.status == 200:
                    ip = json.loads(await r.text()).get("ip")
        except Exception:
            pass
        return {"scheme": scheme, "addr": addr, "exit_ip": ip, "ynet_ms": dt}
```

Run:

```bash
python3 /root/probe_safe.py --target 500 --concurrency 200 --timeout 9
```

Resume after a crash is automatic — the script reads `probe_cursor.txt`
and `hits_checkpoint.json` on startup.

---

## 5. Why we relaxed validation halfway through

The first full run rejected anything that didn't return BOTH a valid
ynet body AND an ipify exit IP. That gave a **0.07 % yield** on a 317K
shuffled pool — most of the discarded responses were proxies that
worked for ynet but blocked or rate-limited ipify.

Two fixes raised the yield to ~0.4 %:

1. **Broader body match.** Original: `b"rss" in body[:400]`. New: any of
   `rss / talkback / success / <?xml` in `body[:1200]`. Captures the
   varied response shapes we saw in practice.
2. **Optional exit IP.** A proxy that delivers ynet content but fails
   `api.ipify.org` is still a working proxy for our use case (we just
   don't know its egress IP yet). Recorded with `exit_ip: null`.

The missing exit IPs are recovered in a separate slow pass — see §7.

---

## 6. Crash recovery

`PROGRESS.md` (at `/root/PROGRESS.md`) is a hand-maintained log of the
whole run with `[ ]/[~]/[x]` checkboxes per phase. After a crash:

```bash
cat /root/PROGRESS.md           # what was the last in-progress step
cat /root/probe_cursor.txt      # where probe stopped
tail -50 /root/probe.log        # last log state
jq 'length' /root/hits_checkpoint.json  # confirmed hits so far
python3 /root/probe_safe.py     # re-launch, resumes from cursor
```

Checkpoint files survive process kill because they are written via
`json.dump(tmp); os.replace(tmp, target)` — atomic on POSIX.

---

## 7. Exit-IP recovery pass

The main probe leaves `exit_ip: null` for ~70 % of hits because ipify is
slow / blocked through many proxies. A second pass —
[`scripts/discovery/recover_exit_ips.py`](../scripts/discovery/recover_exit_ips.py) —
re-attempts the exit-IP lookup with:

- 25-second total timeout (vs 5 s in the main probe)
- Four fallback endpoints in order: `api.ipify.org`, `api64.ipify.org`,
  `ifconfig.co/json`, `ipinfo.io/json`
- 60-coroutine concurrency

```bash
python3 /root/recover_exit_ips.py
```

This is run **after** the main probe finishes. It mutates
`hits_checkpoint.json` in place, only filling in entries where
`exit_ip` is null.

---

## 8. Consolidation

Final merge with the existing 97-entry pool:

```bash
python3 /root/consolidate.py
# → /root/unique_working_proxies_v2.json
# → copy into proxies/unique_v2.json
```

Dedup is by `exit_ip` with last-writer-wins — newer probe results
overwrite older entries for the same egress IP.

---

## 9. Numbers from the run

| Phase | Result |
|---|---|
| Sources fetched | 30+ files, 333 K raw lines |
| Unique candidates after dedup + exclusion | 342,237 |
| Pool processed before target hit | ~219 K (64 %) |
| Time to 500 hits | ~2 hours @ 200 concurrency |
| New confirmed working proxies | **500** |
| With known exit IP after recovery | 272 (54 %) |
| Probe RSS at peak | ~50 MB |
| **Final merged pool (`proxies/unique_v2.json`)** | **597 addresses** (97 old + 500 new) |
| With confirmed unique exit IP | **369** |

---

## 10. File map

| File | Role |
|---|---|
| `/root/PROGRESS.md` | Live recovery log with phase checkboxes |
| `/root/build_candidates.py` | Merge + dedup + shuffle source files |
| `/root/probe_safe.py` | Async probe, resumable, checkpointed |
| `/root/recover_exit_ips.py` | Slow-pass ipify recovery |
| `/root/consolidate.py` | Merge with prior `unique.json` by exit_ip |
| `/root/sources/candidates.txt` | The shuffled candidate pool (342 K lines) |
| `/root/hits_checkpoint.json` | 500 confirmed working proxies |
| `/root/probe_cursor.txt` | Resume point for the probe |
| `/root/probe.log` | Append-only probe stdout |
| `proxies/unique.json` | Original 97 proxies |
| `proxies/unique_v2.json` | Merged final pool |

---

## 11. To re-run from scratch

```bash
# 1. fetch sources (curl/wget into /root/sources/*.txt; see PROGRESS.md §2)
# 2. merge + dedupe
python3 /root/build_candidates.py

# 3. probe (auto-resumable; can be killed and restarted)
python3 /root/probe_safe.py --target 500 --concurrency 200 --timeout 9

# 4. recover missing exit IPs
python3 /root/recover_exit_ips.py

# 5. merge with prior pool
python3 /root/consolidate.py
cp /root/unique_working_proxies_v2.json proxies/unique_v2.json
```
