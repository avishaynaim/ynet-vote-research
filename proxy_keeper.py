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

import json
import os
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests as req_lib

# ── Paths ──────────────────────────────────────────────────────────────────
REPO   = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(REPO, "proxies", "master_pool.json")
ALIVE  = os.path.join(REPO, "proxies", "alive.json")

# ── Tuning ─────────────────────────────────────────────────────────────────
CYCLE_MINUTES   = 5     # sleep between cycles
WORKERS         = 120   # thread-pool workers — stays under proot 150-thread limit
PROBE_TIMEOUT   = 7.0   # per-proxy timeout seconds (5s was too short, killed good proxies)
RESAMPLE_SIZE   = 400   # existing master entries to re-validate per cycle — KEEP LOW
                        # high values destroy the pool during network outages
MIN_SURVIVORS   = 30    # refuse to overwrite alive.json below this
FLUSH_EVERY     = 25    # write alive.json + reload server after this many new hits
SERVER_RELOAD   = "http://127.0.0.1:5001/admin/reload"

# ── Probe target — GET talkback list (does NOT burn vote capacity) ──────────
# Using GET instead of POST means we don't spend any real votes during probing.
# A proxy that can GET the talkback list API can almost certainly POST votes too.
# Yield: ~2-5% (vs ~0.3% for POST vote) because Ynet doesn't dedup GETs.
YNET_BASE    = "https://www.ynet.co.il"
HEADERS  = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":       "application/json, text/plain, */*",
    "Origin":       YNET_BASE,
    "Referer":      f"{YNET_BASE}/",
}

KNOWN_ARTICLES_FILE   = os.path.join(REPO, "results", "known_articles.json")
SERVER_KNOWN_ARTICLES = "http://127.0.0.1:5001/api/known_articles"
SERVER_USED_PROXIES   = "http://127.0.0.1:5001/api/used_proxies"
DEFAULT_ARTICLE       = "yokra14737379"

SOURCES = [
    # ── TheSpeedX (huge, updated frequently) ──────────────────────────────
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    # ── monosans ──────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks5.txt"),
    # ── mmpx12 ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    # ── ErcinDedeoglu ─────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    # ── proxifly ──────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    # ── hookzof ───────────────────────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    # ── prxchk ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    # ── MuRongPIG ─────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    # ── yemixzy ───────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt"),
    # ── zloi-user ─────────────────────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks4.txt"),
    # ── Additional sources (new) ──────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    ("socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("socks5", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt"),
    ("socks4", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt"),
    ("http",   "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt"),
    ("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
    ("socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt"),
    ("socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"),
    ("http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
    ("http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt"),
    ("socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt"),
    ("http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5"),
    ("socks4", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks4"),
    ("http",   "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http"),
    ("socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt"),
    ("http",   "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt"),
    ("socks4", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt"),
    ("http",   "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt"),
]

# ── Additional live API sources (fetched directly, not from GitHub) ─────────
import datetime as _dt

def _fetch_checkerproxy_keeper():
    """checkerproxy.net daily archive — pre-verified proxies from last 24-48 h."""
    TYPE_MAP = {1: "http", 2: "http", 3: "socks4", 4: "socks5"}
    out = []
    for delta in range(3):
        try:
            d = (_dt.date.today() - _dt.timedelta(days=delta)).strftime("%Y-%m-%d")
            req = urllib.request.Request(
                f"https://checkerproxy.net/api/archive/{d}",
                headers={"User-Agent": "proxy-keeper/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                items = json.loads(r.read())
            for item in items:
                addr   = (item.get("addr") or "").strip()
                scheme = TYPE_MAP.get(item.get("type", 1), "http")
                if addr and ":" in addr:
                    out.append((scheme, addr))
            if out:
                break
        except Exception:
            pass
    return out


# ══════════════════════════════════════════════════════════════════════════
def load_known_articles():
    """Return list of article IDs. Try server API → local file → config fallback."""
    try:
        req = urllib.request.urlopen(SERVER_KNOWN_ARTICLES, timeout=5)
        data = json.loads(req.read())
        ids = data.get("article_ids", [])
        if ids:
            return ids
    except Exception:
        pass
    try:
        data = json.load(open(KNOWN_ARTICLES_FILE))
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    try:
        cfg = json.load(open(os.path.join(REPO, "config.json")))
        return [cfg.get("article_id", "yokra14737379")]
    except Exception:
        return ["yokra14737379"]


def fetch_article_targets(article_ids):
    """
    Fetch page-1 talkback IDs for each article directly from Ynet (no proxy).
    Returns {article_id: [talkback_id, ...]}. Articles that fail are skipped.
    """
    targets = {}
    for article_id in article_ids:
        url = f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{article_id}/0/1"
        try:
            r = req_lib.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                continue
            ch = r.json().get("rss", {}).get("channel", {}) or {}
            items = ch.get("item", []) or []
            ids = [c["id"] for c in items if c.get("id")]
            if ids:
                targets[article_id] = ids
                log(f"  article {article_id}: {len(ids)} comments loaded")
            else:
                log(f"  article {article_id}: 0 comments (skipped)")
        except Exception as e:
            log(f"  article {article_id}: fetch failed ({e})")
    return targets


def load_used_proxy_addrs():
    """
    Return the set of proxy addresses that already cast a successful vote this
    server session (via /api/used_proxies). Falls back to alive.json addresses.
    """
    try:
        req = urllib.request.urlopen(SERVER_USED_PROXIES, timeout=5)
        data = json.loads(req.read())
        used = set(data.get("used_proxies", []))
        if used:
            return used
    except Exception:
        pass
    try:
        alive = json.load(open(ALIVE))
        return {p["addr"] for p in alive}
    except Exception:
        return set()


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

    # checkerproxy.net — pre-verified daily archive (highest-quality free source)
    try:
        cp = _fetch_checkerproxy_keeper()
        candidates.extend(cp)
        log(f"  checkerproxy.net: {len(cp)} candidates")
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
# Probe using requests (same library as server) so validated proxies actually
# work for votes — aiohttp and requests behave differently with SOCKS proxies.

def _get_probe_url(targets):
    """Pick a talkback list URL to GET through the proxy (doesn't burn any votes)."""
    if targets:
        article_id = random.choice(list(targets.keys()))
    else:
        try:
            article_id = json.load(open(os.path.join(REPO, "config.json"))).get(
                "article_id", DEFAULT_ARTICLE)
        except Exception:
            article_id = DEFAULT_ARTICLE
    return f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{article_id}/0/1"


def probe_one(scheme, addr, targets, used_addrs):
    """
    Test a proxy by GETting the Ynet talkback list endpoint.

    Using GET instead of POST (vote) preserves all vote capacity for actual use.
    A proxy that can GET the talkback API almost certainly can POST votes too.
    Yield improves from ~0.3% (vote) to ~2-5% (GET reachability).

    Returns a record dict if the proxy reached Ynet, None on connection failure.
    """
    proxy_url = f"{scheme}://{addr}"
    proxies   = {"http": proxy_url, "https": proxy_url}
    url       = _get_probe_url(targets)

    t0 = time.time()
    try:
        r = req_lib.get(url, headers=HEADERS, proxies=proxies, timeout=PROBE_TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        return {
            "scheme":       scheme,
            "addr":         addr,
            "exit_ip":      addr.split(":")[0],
            "ynet_ms":      ms,
            "already_used": addr in used_addrs,
        }
    except Exception:
        return None


def probe_all(candidates, targets, used_addrs, on_flush, prev_alive=None):
    """
    Probe all candidates with a thread pool.
    Calls on_flush(hits, tested_addrs, prev_alive) every FLUSH_EVERY hits.
    hits = proxies that reached Ynet (any HTTP response, regardless of vote_ok).
    tested_addrs = every address probed so far (hit or miss).
    """
    hits = []
    tested_addrs = set()
    done = 0
    total = len(candidates)
    last_flush = 0
    lock = __import__("threading").Lock()

    def _probe(item):
        nonlocal done, last_flush
        scheme, addr = item
        rec = probe_one(scheme, addr, targets, used_addrs)
        with lock:
            done_val = done + 1
            done = done_val
            tested_addrs.add(addr)
            if rec:
                hits.append(rec)
                if len(hits) - last_flush >= FLUSH_EVERY:
                    last_flush = len(hits)
                    on_flush(list(hits), set(tested_addrs), prev_alive)
            if done_val % 500 == 0 or done_val == total:
                vote_ok = sum(1 for h in hits if h.get("vote_ok"))
                log(f"  probed {done_val}/{total}  reachable: {len(hits)}  vote_ok: {vote_ok}")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_probe, candidates))

    return hits, tested_addrs


# ══════════════════════════════════════════════════════════════════════════
def flush_alive(hits, tested_addrs=None, prev_alive=None):
    """
    Write alive.json, merging with still-untested entries from the previous cycle.

    During a cycle we only know about proxies we've actually probed so far.
    Proxies from the previous alive.json that haven't been re-tested yet are kept
    as-is — they're still the best information we have about those addresses.
    As the cycle progresses, each tested address either becomes a new hit (updated)
    or a confirmed miss (dropped). By cycle end alive.json converges to only
    the addresses confirmed alive in the current cycle.
    """
    merged = list(hits)  # confirmed alive in current cycle so far

    if prev_alive and tested_addrs is not None:
        hit_addrs = {h["addr"] for h in hits}
        for p in prev_alive:
            if p["addr"] not in tested_addrs and p["addr"] not in hit_addrs:
                merged.append(p)

    if len(merged) < MIN_SURVIVORS:
        return
    sorted_hits = sorted(merged, key=lambda x: x["ynet_ms"])
    atomic_write(ALIVE, sorted_hits)
    log(f"  flushed {len(sorted_hits)} to alive.json"
        f"  (current-cycle: {len(hits)}, carried-over: {len(sorted_hits)-len(hits)})")
    reload_server()


# ══════════════════════════════════════════════════════════════════════════
def _ynet_reachable():
    """Quick DNS + TCP check — returns True if www.ynet.co.il is reachable."""
    import socket as _sock
    try:
        _sock.setdefaulttimeout(5)
        _sock.getaddrinfo("www.ynet.co.il", 443)
        return True
    except Exception:
        return False


def run_cycle(cycle_num):
    log(f"=== Cycle #{cycle_num} start ===")

    # Safety check: if Ynet DNS is down, ALL probes will fail and the pool
    # will be aggressively pruned for no reason. Skip the cycle entirely.
    if not _ynet_reachable():
        log("  ⚠ Ynet unreachable (DNS/network) — skipping cycle to protect pool")
        return

    master = load_master()
    known_addrs = {p["addr"] for p in master}
    log(f"  master_pool: {len(master)} entries")

    # Load real articles + their comments to use as vote targets
    log("Phase 0: loading vote targets...")
    article_ids = load_known_articles()
    log(f"  known articles: {article_ids}")
    targets = fetch_article_targets(article_ids)
    if not targets:
        log("  WARNING: no comments fetched — probes will use connectivity-only fallback")
    total_comments = sum(len(v) for v in targets.values())
    log(f"  {len(targets)} articles · {total_comments} comments available as targets")

    # Load proxy addresses that already voted successfully this server session
    used_addrs = load_used_proxy_addrs()
    log(f"  used_proxies (already voted): {len(used_addrs)}")

    # Snapshot existing alive.json FIRST — needed to build probe list and for merging.
    try:
        prev_alive = json.load(open(ALIVE))
        log(f"  prev alive.json: {len(prev_alive)} proxies (will carry over un-retested)")
    except Exception:
        prev_alive = []

    # Fetch new candidates
    log("Phase 1: fetching candidates...")
    t0 = time.time()
    new_candidates = fetch_candidates(known_addrs)
    log(f"  {len(new_candidates)} new candidates in {time.time()-t0:.0f}s")

    # Re-probe a sample of existing entries + all new candidates
    resample = random.sample(master, min(RESAMPLE_SIZE, len(master)))
    resampled_addrs = {p["addr"] for p in resample}
    resample_pairs = [(p["scheme"], p["addr"]) for p in resample]

    master_by_addr = {p["addr"]: p for p in master}
    already_queued = set(resampled_addrs)

    # Always re-test every proxy currently in alive.json — critical for accuracy.
    alive_to_probe = []
    for p in prev_alive:
        if p["addr"] not in already_queued:
            alive_to_probe.append((p["scheme"], p["addr"]))
            already_queued.add(p["addr"])
    if alive_to_probe:
        log(f"  +{len(alive_to_probe)} alive.json proxies added to probe")

    # Always probe every currently-used proxy too.
    used_to_probe = [
        (master_by_addr[a]["scheme"], a)
        for a in used_addrs
        if a in master_by_addr and a not in already_queued
    ]
    if used_to_probe:
        log(f"  +{len(used_to_probe)} currently-used proxies added to probe")

    all_candidates = resample_pairs + alive_to_probe + used_to_probe + new_candidates
    random.shuffle(all_candidates)

    log(f"Phase 2: probing {len(all_candidates)} total (workers={WORKERS})...")

    def _flush(hits, tested, prev):
        flush_alive(hits, tested, prev)

    t0 = time.time()
    hits, tested_addrs = probe_all(all_candidates, targets, used_addrs, _flush, prev_alive)
    elapsed = time.time() - t0
    vote_ok_count    = sum(1 for h in hits if h.get("vote_ok"))
    already_used_ct  = sum(1 for h in hits if h.get("already_used"))
    fresh_votes      = sum(1 for h in hits if h.get("vote_ok") and not h.get("already_used"))
    log(f"  done: {len(hits)} reachable  already_used: {already_used_ct}  in {elapsed:.0f}s")

    if len(hits) < MIN_SURVIVORS:
        log(f"  only {len(hits)} survivors — skipping master save")
        return

    # Final flush — merge with prev_alive so proxies not re-tested this cycle
    # survive into the next cycle. Only proxies confirmed dead (tested & missed)
    # are dropped. This lets the alive pool GROW across cycles rather than reset.
    flush_alive(hits, tested_addrs=tested_addrs, prev_alive=prev_alive)

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
    log(f"proxy_keeper starting  cycle={CYCLE_MINUTES}min  workers={WORKERS}  resample={RESAMPLE_SIZE}  timeout={PROBE_TIMEOUT}s  probe=GET")
    cycle = 1
    while True:
        if not _ynet_reachable():
            log(f"  ⚠ Ynet DNS down — waiting 60s before retry (pool protected)")
            time.sleep(60)
            continue
        try:
            run_cycle(cycle)
        except Exception as e:
            log(f"cycle #{cycle} crashed: {e}")
        cycle += 1
        log(f"sleeping {CYCLE_MINUTES} min...")
        time.sleep(CYCLE_MINUTES * 60)


if __name__ == "__main__":
    main()
