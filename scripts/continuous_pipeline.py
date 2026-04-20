#!/usr/bin/env python3
"""
Continuous Proxy Pipeline
=========================
Loops forever:
  1. Harvest fresh proxies from 80+ sources
  2. Health-check ALL known proxies (master + new)
  3. Save alive.json with only working ones
  4. Report stats
  5. Pause, repeat

All discovered proxies accumulate in master_pool.json (even dead ones —
they may come back). alive.json is rebuilt each cycle with only verified-
working proxies. The server reads alive.json.

Usage:
    python3 scripts/continuous_pipeline.py
    python3 scripts/continuous_pipeline.py --target 30000  # stop after 30K total discovered
"""

import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType
import json
import time
import random
import re
import os
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(BASE, "proxies", "master_pool.json")
ALIVE  = os.path.join(BASE, "proxies", "alive.json")
LOG    = os.path.join(BASE, "scripts", "discovery", "sources", "pipeline.log")

os.makedirs(os.path.dirname(LOG), exist_ok=True)
os.makedirs(os.path.join(BASE, "proxies"), exist_ok=True)

# ── Test URLs ──────────────────────────────────────────────────────────────
YNET_URL  = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
IPIFY_URL = "https://api.ipify.org?format=json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# ── Concurrency ────────────────────────────────────────────────────────────
HARVEST_CONC = 120   # probing concurrency
HEALTH_CONC  = 120   # health-check concurrency
TIMEOUT      = 8     # seconds per probe
IPIFY_TIMEOUT = 5

# ── Proxy sources ─────────────────────────────────────────────────────────
# Same comprehensive list from mega_harvest.py

GITHUB_SOURCES = [
    # TheSpeedX
    ("http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    # monosans
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    # MuRongPIG
    ("http", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    # ErcinDedeoglu
    ("http", "https://raw.githubusercontent.com/ErcinDedeworker/proxies/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    # zloi-user
    ("http", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"),
    # proxifly
    ("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt"),
    ("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),
    # sunny9577
    ("http", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"),
    # roosterkid
    ("http", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    # hookzof
    ("http", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    # caliphdev
    ("http", "https://raw.githubusercontent.com/caliphdev/Starter-Proxy/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/caliphdev/Starter-Proxy/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/caliphdev/Starter-Proxy/main/socks5.txt"),
    # Zaeem20
    ("http", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXY_LIST/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXY_LIST/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXY_LIST/master/socks5.txt"),
    # officialputuid
    ("http", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),
    # rdavydov
    ("http", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),
    # Anonym0usWork1221
    ("http", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    # FLAVOR additional large lists
    ("http", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/ObcbO/getproxy/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/socks5.txt"),
]

ADDR_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})")

# ── Logging ────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

# ── Load/save master pool ─────────────────────────────────────────────────
def load_master():
    if os.path.exists(MASTER):
        with open(MASTER) as f:
            return json.load(f)
    return []

def save_master(pool):
    with open(MASTER, "w") as f:
        json.dump(pool, f, indent=1)

def save_alive(pool):
    with open(ALIVE, "w") as f:
        json.dump(pool, f, indent=1)

# ── Fetch sources ─────────────────────────────────────────────────────────
def fetch_one(args):
    import requests
    scheme, url = args
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": UA})
        addrs = ADDR_RE.findall(r.text)
        return [(scheme, a) for a in addrs]
    except:
        return []

def fetch_proxyscrape():
    import requests
    out = []
    for proto in ("http", "socks4", "socks5"):
        for url in [
            f"https://api.proxyscrape.com/v2/?request=displayproxies&protocol={proto}&timeout=10000&country=all",
            f"https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol={proto}&timeout=10000",
        ]:
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": UA})
                for a in ADDR_RE.findall(r.text):
                    out.append((proto, a))
            except:
                pass
    return out

def fetch_geonode():
    import requests
    out = []
    for page in range(1, 20):
        try:
            r = requests.get(
                f"https://proxylist.geonode.com/api/proxy-list?limit=500&page={page}&sort_by=lastChecked&sort_type=desc",
                timeout=15, headers={"User-Agent": UA})
            data = r.json().get("data", [])
            if not data:
                break
            for p in data:
                ip = p.get("ip")
                port = p.get("port")
                protos = p.get("protocols", [])
                for pr in protos:
                    out.append((pr.lower(), f"{ip}:{port}"))
        except:
            break
    return out

def fetch_freeproxylist():
    import requests
    from html.parser import HTMLParser
    out = []
    urls = [
        "https://free-proxy-list.net/",
        "https://free-proxy-list.net/anonymous-proxy.html",
        "https://www.sslproxies.org/",
        "https://www.us-proxy.org/",
        "https://free-proxy-list.net/uk-proxy.html",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": UA})
            for a in ADDR_RE.findall(r.text):
                out.append(("http", a))
        except:
            pass
    return out

def gather_all():
    """Fetch from all sources, return deduplicated set of (scheme, addr)."""
    results = set()

    # GitHub sources in parallel
    with ThreadPoolExecutor(max_workers=25) as ex:
        for batch in ex.map(fetch_one, GITHUB_SOURCES):
            results.update(batch)
    log(f"  GitHub sources: {len(results)} raw")

    # API sources
    for name, fn in [("ProxyScrape", fetch_proxyscrape),
                     ("Geonode", fetch_geonode),
                     ("FreeProxyList", fetch_freeproxylist)]:
        try:
            batch = fn()
            results.update(batch)
            log(f"  {name}: +{len(batch)}, total {len(results)}")
        except Exception as e:
            log(f"  {name}: error {e}")

    return results

# ── Probe proxy against ynet ──────────────────────────────────────────────
PROXY_TYPE_MAP = {"http": ProxyType.HTTP, "socks4": ProxyType.SOCKS4, "socks5": ProxyType.SOCKS5}

async def probe_one(sem, scheme, addr):
    """Test proxy against ynet. Returns dict if working, None if not."""
    proxy_url = f"{scheme}://{addr}"
    ptype = PROXY_TYPE_MAP.get(scheme, ProxyType.HTTP)
    host, port_s = addr.rsplit(":", 1)

    async with sem:
        try:
            conn = ProxyConnector(proxy_type=ptype, host=host, port=int(port_s), rdns=True)
            timeout = aiohttp.ClientTimeout(total=TIMEOUT)
            async with aiohttp.ClientSession(connector=conn, timeout=timeout,
                                              headers={"User-Agent": UA}) as session:
                async with session.get(YNET_URL) as r:
                    if r.status != 200:
                        return None
                    # Try to get exit IP
                    exit_ip = None
                    try:
                        t2 = aiohttp.ClientTimeout(total=IPIFY_TIMEOUT)
                        async with session.get(IPIFY_URL, timeout=t2) as r2:
                            if r2.status == 200:
                                d = await r2.json()
                                exit_ip = d.get("ip")
                    except:
                        pass
                    return {"scheme": scheme, "addr": addr, "exit_ip": exit_ip}
        except:
            return None

# ── Health check existing proxies ─────────────────────────────────────────
async def health_check(sem, proxy):
    """Re-validate an existing proxy entry."""
    scheme = proxy.get("scheme", "http")
    addr = proxy["addr"]
    result = await probe_one(sem, scheme, addr)
    if result:
        # Preserve exit_ip if we already know it
        if proxy.get("exit_ip") and not result.get("exit_ip"):
            result["exit_ip"] = proxy["exit_ip"]
    return result

# ── Main pipeline cycle ──────────────────────────────────────────────────
async def run_cycle(cycle_num, master_pool):
    """One full cycle: harvest → probe new → health-check all → save."""
    cycle_start = time.time()
    log(f"═══ Cycle {cycle_num} start ═══")

    # Build index of known proxies
    known = {(p["scheme"], p["addr"]) for p in master_pool}
    log(f"Master pool: {len(master_pool)} entries")

    # 1. Harvest fresh candidates
    log("Phase 1: Fetching sources...")
    raw = gather_all()
    new_candidates = [(s, a) for s, a in raw if (s, a) not in known]
    log(f"  Raw: {len(raw)} total, {len(new_candidates)} NEW candidates")

    # 2. Probe new candidates
    new_hits = []
    if new_candidates:
        log(f"Phase 2: Probing {len(new_candidates)} new candidates...")
        sem = asyncio.Semaphore(HARVEST_CONC)
        random.shuffle(new_candidates)

        done = 0
        tasks = [probe_one(sem, s, a) for s, a in new_candidates]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            done += 1
            if result:
                new_hits.append(result)
                master_pool.append(result)
                known.add((result["scheme"], result["addr"]))
            if done % 200 == 0:
                elapsed = time.time() - cycle_start
                log(f"  Probed {done}/{len(new_candidates)} — {len(new_hits)} new hits ({done/elapsed:.0f}/s)")

        log(f"  New hits: {len(new_hits)} from {len(new_candidates)} candidates ({100*len(new_hits)/max(1,len(new_candidates)):.1f}%)")
        save_master(master_pool)
    else:
        log("Phase 2: No new candidates, skipping probe")

    # 3. Health-check ALL known proxies
    log(f"Phase 3: Health-checking {len(master_pool)} proxies...")
    sem = asyncio.Semaphore(HEALTH_CONC)
    alive = []
    done = 0

    tasks = [health_check(sem, p) for p in master_pool]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        done += 1
        if result:
            alive.append(result)
        if done % 200 == 0:
            log(f"  Checked {done}/{len(master_pool)} — {len(alive)} alive")

    # Save alive
    save_alive(alive)
    alive_labels = {p.get("exit_ip") or p["addr"] for p in alive}

    elapsed = time.time() - cycle_start
    log(f"═══ Cycle {cycle_num} done ({elapsed:.0f}s) ═══")
    log(f"  Master pool: {len(master_pool)} total discovered")
    log(f"  Alive now: {len(alive)} entries, {len(alive_labels)} unique labels")
    log(f"  New this cycle: {len(new_hits)} hits")
    log("")

    return master_pool, len(alive), len(alive_labels)

# ── Entry point ───────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=30000, help="Stop after this many total discovered")
    parser.add_argument("--pause", type=int, default=60, help="Seconds between cycles")
    args = parser.parse_args()

    log(f"Continuous Pipeline — target {args.target} total discovered")
    log(f"Pause between cycles: {args.pause}s")

    master_pool = load_master()
    log(f"Loaded {len(master_pool)} from master pool")

    cycle = 0
    while True:
        cycle += 1
        master_pool, alive_count, alive_labels = await run_cycle(cycle, master_pool)

        if len(master_pool) >= args.target:
            log(f"TARGET REACHED: {len(master_pool)} >= {args.target}")
            break

        log(f"Sleeping {args.pause}s before next cycle... (master={len(master_pool)}, alive={alive_count}/{alive_labels} labels)")
        await asyncio.sleep(args.pause)

if __name__ == "__main__":
    asyncio.run(main())
