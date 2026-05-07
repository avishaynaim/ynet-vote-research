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
CYCLE_MINUTES   = 15
WORKERS         = 100   # thread-pool workers — stays well under proot 150-thread limit
PROBE_TIMEOUT   = 10.0
RESAMPLE_SIZE   = 600   # existing master entries to re-validate per cycle
MIN_SURVIVORS   = 30    # refuse to overwrite alive.json below this
FLUSH_EVERY     = 25    # write alive.json + reload server after this many new hits
SERVER_RELOAD   = "http://127.0.0.1:5001/admin/reload"

# ── Probe target — POST a real vote through the proxy ──────────────────────
# We pick a random article + comment from known_articles.json, then vote
# like/dislike randomly. HTTP 200 = ynet accepted the vote; any other response
# still means the proxy is reachable but the vote was rejected (dedup / blocked).
YNET_BASE    = "https://www.ynet.co.il"
VOTE_URL     = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
HEADERS  = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin":       YNET_BASE,
    "Referer":      f"{YNET_BASE}/",
    "Content-Type": "application/json",
}

KNOWN_ARTICLES_FILE  = os.path.join(REPO, "results", "known_articles.json")
SERVER_KNOWN_ARTICLES = "http://127.0.0.1:5001/api/known_articles"
SERVER_USED_PROXIES   = "http://127.0.0.1:5001/api/used_proxies"

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

def probe_one(scheme, addr, targets, used_addrs):
    """
    Test a proxy by casting a real vote on a randomly chosen article + comment.
    - targets:    {article_id: [talkback_id, ...]} — pre-fetched this cycle
    - used_addrs: set of addr strings already in the server's vote-ok pool

    Returns a record dict if the proxy reached Ynet (any HTTP response), None if
    the proxy failed to connect entirely.

    Record fields:
      scheme, addr, exit_ip, ynet_ms — same as before (alive.json compatible)
      vote_ok      — True if Ynet returned HTTP 200 (vote accepted)
      already_used — True if this addr was already in used_addrs before this probe
      article_id, talkback_id, like — what was voted on
    """
    proxy_url = f"{scheme}://{addr}"
    proxies = {"http": proxy_url, "https": proxy_url}

    if targets:
        article_id  = random.choice(list(targets.keys()))
        talkback_id = random.choice(targets[article_id])
        like        = random.choice([True, False])
        payload = {
            "article_id":      article_id,
            "talkback_id":     talkback_id,
            "talkback_like":   like,
            "talkback_unlike": not like,
            "vote_type":       "2state",
        }
    else:
        # No comments available yet — fall back to the default article with id=0
        # (still tests reachability; vote will be rejected by Ynet)
        cfg_article = json.load(open(os.path.join(REPO, "config.json"))).get(
            "article_id", "yokra14737379")
        article_id  = cfg_article
        talkback_id = 0
        like        = True
        payload = {"article_id": article_id, "talkback_id": 0,
                   "talkback_like": True, "talkback_unlike": False, "vote_type": "2state"}

    t0 = time.time()
    try:
        r = req_lib.post(VOTE_URL, json=payload, headers=HEADERS,
                         proxies=proxies, timeout=PROBE_TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        return {
            "scheme":       scheme,
            "addr":         addr,
            "exit_ip":      addr.split(":")[0],
            "ynet_ms":      ms,
            "vote_ok":      r.status_code == 200,
            "already_used": addr in used_addrs,
            "article_id":   article_id,
            "talkback_id":  talkback_id,
            "like":         like,
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
def run_cycle(cycle_num):
    log(f"=== Cycle #{cycle_num} start ===")

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
    log(f"  done: {len(hits)} reachable  vote_ok: {vote_ok_count}"
        f"  fresh_votes: {fresh_votes}  re-used: {already_used_ct}  in {elapsed:.0f}s")

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
    log(f"proxy_keeper starting  cycle={CYCLE_MINUTES}min  workers={WORKERS}  resample={RESAMPLE_SIZE}")
    cycle = 1
    while True:
        try:
            run_cycle(cycle)
        except Exception as e:
            log(f"cycle #{cycle} crashed: {e}")
        cycle += 1
        log(f"sleeping {CYCLE_MINUTES} min...")
        time.sleep(CYCLE_MINUTES * 60)


if __name__ == "__main__":
    main()
