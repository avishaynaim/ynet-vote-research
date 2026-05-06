#!/usr/bin/env python3
"""
Proxy Keeper — background daemon that keeps alive.json fresh.

Each cycle:
  1. Fetch candidates from GitHub/API sources
  2. Re-probe a random sample of existing master_pool entries
  3. Probe all candidates (asyncio, concurrency=300 — coroutines not threads)
  4. Write hits to alive.json immediately every FLUSH_EVERY hits + reload server
  5. Merge all survivors into master_pool.json
  6. Sleep CYCLE_MINUTES, repeat

Run in background:
    python3 proxy_keeper.py > /tmp/proxy_keeper.log 2>&1 &
"""

import asyncio
import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime

import aiohttp
from aiohttp_socks import ProxyConnector

# ── Paths ──────────────────────────────────────────────────────────────────
REPO   = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(REPO, "proxies", "master_pool.json")
ALIVE  = os.path.join(REPO, "proxies", "alive.json")

# ── Tuning ─────────────────────────────────────────────────────────────────
CYCLE_MINUTES   = 30
CONCURRENCY     = 300   # asyncio coroutines — NOT OS threads, safe under proot
PROBE_TIMEOUT   = 7.0
RESAMPLE_SIZE   = 600   # existing master entries to re-validate per cycle
MIN_SURVIVORS   = 30    # refuse to overwrite alive.json below this
FLUSH_EVERY     = 50    # write alive.json + reload server after this many new hits
SERVER_RELOAD   = "http://127.0.0.1:5001/admin/reload"

# ── Probe target — POST to vote endpoint with dummy payload ────────────────
# We test the VOTE endpoint (POST), not the list (GET), because ynet may allow
# proxy reads but block proxy votes. Any response from ynet (even 400/403) means
# the proxy can reach the vote endpoint — that's all we need.
VOTE_URL     = "https://www.ynet.co.il/iphone/json/api/talkbacks/vote"
VOTE_PAYLOAD = {"article_id": "yokra14737379", "talkback_id": 0,
                "talkback_like": True, "talkback_unlike": False, "vote_type": "2state"}
HEADERS  = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin":       "https://www.ynet.co.il",
    "Referer":      "https://www.ynet.co.il/",
    "Content-Type": "application/json",
}

SOURCES = [
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("http",   "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt"),
]


# ══════════════════════════════════════════════════════════════════════════
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_master():
    if not os.path.exists(MASTER):
        return []
    try:
        return json.load(open(MASTER))
    except Exception:
        return []


def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def reload_server():
    try:
        req = urllib.request.Request(SERVER_RELOAD, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            log(f"  server reloaded → {resp.get('loaded')} proxies")
    except Exception as e:
        log(f"  server reload skipped ({e})")


# ══════════════════════════════════════════════════════════════════════════
def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-keeper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _parse_lines(scheme, text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith("#"):
            parts = line.split(":")
            if len(parts) == 2:
                try:
                    int(parts[1])
                    out.append((scheme, line))
                except ValueError:
                    pass
    return out


def fetch_candidates(known_addrs):
    candidates = []
    for scheme, url in SOURCES:
        try:
            body = _http_get(url)
            candidates.extend(_parse_lines(scheme, body))
        except Exception:
            pass

    for proto in ("http", "socks4", "socks5"):
        try:
            url = (f"https://api.proxyscrape.com/v3/free-proxy-list/get"
                   f"?request=displayproxies&protocol={proto}"
                   f"&timeout=20000&country=all&proxy_format=ipport&format=text")
            body = _http_get(url, 30)
            candidates.extend(_parse_lines(proto, body))
        except Exception:
            pass

    seen = set(known_addrs)
    fresh = []
    for s, a in candidates:
        if a not in seen:
            seen.add(a)
            fresh.append((s, a))
    return fresh


# ══════════════════════════════════════════════════════════════════════════
async def probe_one(scheme, addr):
    url = f"{scheme}://{addr}"
    try:
        conn = ProxyConnector.from_url(url, rdns=True)
    except Exception:
        return None
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
    try:
        async with aiohttp.ClientSession(connector=conn, timeout=timeout, trust_env=False) as s:
            t0 = time.time()
            async with s.post(VOTE_URL, json=VOTE_PAYLOAD, headers=HEADERS) as r:
                # Any response from ynet (200, 400, 403, 429...) means proxy reaches vote endpoint
                if r.status == 0:
                    return None
                await r.content.read(200)
                ms = int((time.time() - t0) * 1000)
        return {"scheme": scheme, "addr": addr, "exit_ip": addr.split(":")[0], "ynet_ms": ms}
    except Exception:
        return None


async def probe_all(candidates, on_flush):
    """
    Probe all candidates. Calls on_flush(hits_so_far) every FLUSH_EVERY new hits
    so the server can be reloaded with partial results immediately.
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    hits = []
    done = 0
    total = len(candidates)
    last_flush = 0

    async def _probe(scheme, addr):
        nonlocal done, last_flush
        async with sem:
            rec = await probe_one(scheme, addr)
        done += 1
        if rec:
            hits.append(rec)
            if len(hits) - last_flush >= FLUSH_EVERY:
                last_flush = len(hits)
                await asyncio.get_event_loop().run_in_executor(None, on_flush, list(hits))
        if done % 500 == 0 or done == total:
            log(f"  probed {done}/{total}  hits: {len(hits)}")

    tasks = [asyncio.create_task(_probe(s, a)) for s, a in candidates]
    await asyncio.gather(*tasks, return_exceptions=True)
    return hits


# ══════════════════════════════════════════════════════════════════════════
def flush_alive(hits):
    """Write current hits to alive.json sorted by latency, then reload server."""
    if len(hits) < MIN_SURVIVORS:
        return
    sorted_hits = sorted(hits, key=lambda x: x["ynet_ms"])
    atomic_write(ALIVE, sorted_hits)
    log(f"  flushed {len(sorted_hits)} proxies to alive.json")
    reload_server()


# ══════════════════════════════════════════════════════════════════════════
async def run_cycle(cycle_num):
    log(f"=== Cycle #{cycle_num} start ===")

    master = load_master()
    known_addrs = {p["addr"] for p in master}
    log(f"  master_pool: {len(master)} entries")

    # Fetch new candidates
    log("Phase 1: fetching candidates...")
    t0 = time.time()
    new_candidates = fetch_candidates(known_addrs)
    log(f"  {len(new_candidates)} new candidates in {time.time()-t0:.0f}s")

    # Re-probe a sample of existing entries + all new candidates
    resample = random.sample(master, min(RESAMPLE_SIZE, len(master)))
    resampled_addrs = {p["addr"] for p in resample}
    resample_pairs = [(p["scheme"], p["addr"]) for p in resample]

    all_candidates = resample_pairs + new_candidates
    random.shuffle(all_candidates)
    log(f"Phase 2: probing {len(all_candidates)} total (concurrency={CONCURRENCY})...")

    t0 = time.time()
    hits = await probe_all(all_candidates, flush_alive)
    elapsed = time.time() - t0
    log(f"  done: {len(hits)} hits in {elapsed:.0f}s")

    if len(hits) < MIN_SURVIVORS:
        log(f"  only {len(hits)} survivors — skipping master save")
        return

    # Final alive.json flush
    flush_alive(hits)

    # Rebuild master: keep un-sampled entries + survivors from resample + new hits
    hit_map = {h["addr"]: h for h in hits}
    hit_addrs = set(hit_map)

    kept = []
    for p in master:
        if p["addr"] not in resampled_addrs:
            kept.append(p)
        elif p["addr"] in hit_addrs:
            kept.append(hit_map[p["addr"]])

    new_entries = [h for h in hits if h["addr"] not in known_addrs]
    merged = kept + new_entries

    pruned = len(resample) - sum(1 for p in resample if p["addr"] in hit_addrs)
    log(f"  pruned {pruned} dead | +{len(new_entries)} new | master total {len(merged)}")
    atomic_write(MASTER, merged)
    log(f"=== Cycle #{cycle_num} done ===\n")


def main():
    log(f"proxy_keeper starting  cycle={CYCLE_MINUTES}min  concurrency={CONCURRENCY}  resample={RESAMPLE_SIZE}")
    cycle = 1
    while True:
        try:
            asyncio.run(run_cycle(cycle))
        except Exception as e:
            log(f"cycle #{cycle} crashed: {e}")
        cycle += 1
        log(f"sleeping {CYCLE_MINUTES} min...")
        time.sleep(CYCLE_MINUTES * 60)


if __name__ == "__main__":
    main()
