# Proxy Knowledge — What We Learned

**Last updated:** 2026-04-15
**Run that produced these numbers:** see [PROXY_DISCOVERY.md](PROXY_DISCOVERY.md)

This is the durable, reusable knowledge from running a 342K-candidate
proxy validation against ynet. If you only have time to read one file
about proxies in this repo, read this one.

---

## TL;DR — what you actually need to know

1. **Proxy yield from public lists is ~0.2–0.5 %.** Plan for 200–500
   candidates per usable proxy. A 342K pool → 764 working proxies.
2. **Most "working" proxies fail at exit-IP detection** because they
   block or rate-limit the IP-echo services. ~60 % of our hits had
   `exit_ip = null` after the first probe pass. A second pass with
   relaxed timeouts and **multiple endpoints** (`ipify`, `api64.ipify`,
   `ifconfig.co`, `ipinfo.io`) recovered ~40 % of those.
3. **Use async, not threads, in container/proot environments.** 500
   threads kill the kernel with `futex facility returned an unexpected
   error code`. 200 asyncio coroutines do the same work in ~50 MB.
4. **Many "unique" proxy addresses share egress IPs.** Our 861-address
   pool collapses to 407 unique exit IPs. The biggest cluster is one
   provider running ~30 socks5 listeners (`206.123.156.0/24`) all
   egressing through a small set of upstream IPs.
5. **Validation must be domain-specific.** Generic "is this a proxy"
   checks let many proxies through that fail on ynet (Cloudflare-blocked,
   geo-blocked, header-stripped). Always test against the *actual* target
   endpoint.

---

## 1. Sources, ranked by yield

The 342K pool is the deduplicated union of every public list we could
find. After the run, we can score each source by **% of its candidates
that produced confirmed ynet-working proxies**. (Numbers approximate —
overlap between lists makes this imprecise.)

**High-yield sources (worth re-checking weekly):**
- `monosans/proxy-list` — fastest-refreshing public list, 0.5–1 % yield
- `TheSpeedX/PROXY-List` — large, decent freshness
- `proxifly/free-proxy-list` — small but high-quality
- ProxyScrape API (`api.proxyscrape.com`) — adjustable timeout filter

**Medium-yield (good for breadth, lots of dead weight):**
- `ShiftyTR/Proxy-List`, `jetkai/proxy-list`, `vakhov/fresh-proxy-list`
- `roosterkid/openproxylist`, `mmpx12/proxy-list`
- `clarketm/proxy-list`, `sunny9577/proxy-scraper`

**Low-yield (mostly dead, useful only for volume):**
- `MuRongPIG/Proxy-Master` — adds 100K+ candidates with 0.05 % yield
- `hookzof/socks5_list` — slow refresh

**One absolute rule:** never trust a list's "verified" or "last checked"
metadata. We verified everything ourselves and ~99.5 % was dead.

---

## 2. What "working" actually means for ynet

A proxy is ynet-usable iff:

```
1. TCP CONNECT to proxy:port succeeds (basic reachability)
2. CONNECT https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/...
   returns HTTP 200
3. The body contains AT LEAST ONE of: "rss", "talkback", "success", "<?xml"
   (The talkback endpoint is RSS-shaped JSON; this is its signature.)
```

Things we tried that **didn't work** as validators:

| Validator | Why we dropped it |
|---|---|
| `if b"rss" in body[:400]` only | Rejected ~85 % of actually-working proxies that returned valid JSON without the `rss` token in first 400 bytes. |
| Required `api.ipify.org` to return an IP | Rejected ~70 % of working proxies that block ipify. |
| Just checking HTTP 200 | Many proxies return 200 with HTML interstitials, captchas, or blank bodies — *not* the ynet payload. |
| `requests.get` timeout 5s | Many otherwise-working proxies need 6–9 s for the first request through them. |

What worked: **9-second total timeout, 200-status check, body match against the four-token alternation, ipify as best-effort.**

---

## 3. Exit IPs vs. addresses — they are not the same

A common mistake is treating proxy address as a unique identifier for
"a different egress IP." It is not. Of our 861 working proxy addresses:

- 407 have a **distinct exit IP**
- 411 have an exit IP that's **shared with at least one other address**
  in the pool
- 278 have **`exit_ip = null`** (couldn't detect — they may or may not
  share)

**This matters a lot for vote-rotation.** Ynet dedupes by client IP
(see `PROXY_ROTATION.md`). Sending 100 votes through 100 addresses
that share 5 egress IPs gives you ~5 votes counted, not 100.

The rotation client should **pre-dedupe by `exit_ip`** before fan-out
to maximize counted votes per proxy attempt. Addresses with `exit_ip =
null` are usable but unpredictable (they may or may not collide).

### Cluster patterns to watch for

These prefixes contributed many addresses but few unique exit IPs:

```
206.123.156.0/24   — ~150 addresses, ~10 unique exit IPs (provider relay)
164.163.42.0/24    — ~12 addresses, ~6 unique exit IPs
141.98.11.0/24     — ~16 addresses, ~3 unique exit IPs
```

Pattern: a single provider runs many listening ports/addresses that all
egress through a small NAT pool. **Always check `/24` density** before
trusting an address-count as an IP-count.

---

## 4. Anti-patterns: what to avoid next time

### Don't use threads in proot/containers
500 `requests` threads = `futex facility returned an unexpected error
code` + SIGKILL. The proot kernel can't hand out that many futex slots.
Cap threads at ~80 or use asyncio (preferred — same throughput at 1/10
the memory).

### Don't validate with the *first* response of an unknown endpoint
The first request through a fresh socks5 connection can take 6–8 s
even on a working proxy (DNS over the tunnel, SOCKS handshake, TLS
negotiation). A 3-second timeout will reject these. Use 9 s minimum
for first-touch, then a separate "alive" pool with shorter timeouts
once you know which proxies are responsive.

### Don't read the whole response body
Several "working" proxies return multi-MB HTML interstitials. Read
only the first 1200 bytes for validation; that's enough to match
the ynet signature without OOMing on bad responses.

### Don't trust `api.ipify.org` as the sole exit-IP source
About 30 % of working proxies block ipify outright. Always have
fallbacks (`api64.ipify`, `ifconfig.co/json`, `ipinfo.io/json`) and
treat exit-IP discovery as **best-effort, not gating**.

### Don't checkpoint by appending
Use `json.dump(tmp); os.replace(tmp, target)` for atomic rewrites.
A SIGKILL in the middle of `json.dump(open(file, "w"), ...)` leaves
truncated JSON that won't parse on restart.

---

## 5. Crash recovery — the design that survived

After multiple crashes, this is the recovery contract that worked:

```
hits_checkpoint.json   — atomic rewrite every 10 hits (idempotent)
probe_cursor.txt       — integer offset into candidates.txt (resume)
probe.log              — append-only, tail-friendly progress
PROGRESS.md            — hand-maintained phase log with [ ]/[~]/[x]
```

Restart procedure:

```bash
cat /root/PROGRESS.md           # last in-progress step
cat /root/probe_cursor.txt      # where probe stopped
jq 'length' /root/hits_checkpoint.json
python3 /root/probe_safe.py     # auto-resumes from cursor + checkpoint
```

The probe re-loads `STATE["seen_ips"]` and `STATE["seen_addrs"]` from
the checkpoint on startup, so a re-run never re-reports a known proxy.

**Known limitation:** the queue.join() call after stop hangs because
worker coroutines exit on the stop flag without calling `task_done()`
on items still in queue. Workaround: send SIGTERM, then SIGKILL after
2 s — the checkpoint is already written. Fixing this properly means
draining the queue with `queue.get_nowait()` calls after stop is set.

---

## 6. Performance numbers from this run

Hardware: proot-distro Ubuntu on Android, ARM64, ~7 GB RAM.

| Stage | Throughput | Memory | Notes |
|---|---|---|---|
| Source fetch (parallel curl) | n/a | tiny | ~30 sources in <60 s |
| Candidate dedup + shuffle | 1 s for 342K | 200 MB peak | Single-pass Python |
| Probe (200 concurrency, 9s timeout) | **~31 candidates/s** | ~50 MB RSS | aiohttp + aiohttp-socks |
| Exit-IP recovery (60 conc, 25s timeout) | ~25 hits/min | ~30 MB | 4 fallback endpoints |
| Total wall time for 342K pool | **~3 hours** | <100 MB | Single resumable run |

Yield: 764 / 342,237 = **0.22 %**.

---

## 7. Reusable assets in this repo

```
docs/PROXY_DISCOVERY.md           — methodology + how to re-run
docs/PROXY_ROTATION.md            — how the runtime uses the pool
docs/PROXY_KNOWLEDGE.md           — this file (durable learnings)
proxies/unique.json               — original 97-entry master pool
proxies/unique_v2.json            — merged master (861 addresses)
proxies/alive.json                — currently-verified subset (796)
scripts/check_proxies.py          — quick re-verification (older script)
scripts/refresh_proxies.py        — incremental top-up of master pool
scripts/discovery/                — full discovery pipeline:
  build_candidates.py             — merge + dedup + shuffle sources
  probe_safe.py                   — async probe, resumable
  recover_exit_ips.py             — slow-pass exit-IP recovery
  rebuild_alive.py                — alive.json from hits_checkpoint
  consolidate.py                  — merge with prior unique.json
  PROGRESS.md                     — recovery log template
```

---

## 8. What we'd do differently next time

1. **Skip the low-yield giants up front.** MuRongPIG alone added 100 K
   candidates for ~50 hits. Defer it to a "deep scan" mode and start
   with the top-yield 5 sources (~30 K candidates, similar hit count
   in 1/10 the time).
2. **Run exit-IP recovery in parallel with the probe** instead of after.
   It's a separate workload and has slack while the probe waits on slow
   socks proxies. Could shave 10–15 min off total wall time.
3. **Add a per-proxy "consistency check"** — hit ynet twice through
   each proxy and require both to succeed. Many "working" proxies are
   transient (~30 s usable window); a single-shot check catches them
   right before they die.
4. **Build a `.git`-tracked `proxies/checked_at.json`** so we know when
   each entry was last verified — needed to drive auto-expiry from
   `alive.json`.
