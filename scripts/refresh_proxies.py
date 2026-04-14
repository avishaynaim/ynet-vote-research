#!/usr/bin/env python3
"""
Refresh the ynet proxy pool.

Flow:
  1. Fetch ~20 public proxy aggregator lists (raw .txt on GitHub).
  2. Parse + dedupe candidates.
  3. Exclude everything already in unique_working_proxies.json / proxies_alive.json.
  4. Probe each survivor in parallel with the same two-stage check the original
     discover scripts used:
       a. GET https://api.ipify.org → proves forwarding + reveals exit IP
       b. GET ynet talkbacks list    → proves the proxy can actually reach ynet
  5. Dedupe new survivors by exit IP.
  6. Append to /root/unique_working_proxies.json.
  7. Re-run the lightweight ynet health check on the full pool and rewrite
     /root/proxies_alive.json sorted by latency.

Usage:
  python3 scripts/refresh_proxies.py                 # target 50 new, ~20k candidates max
  python3 scripts/refresh_proxies.py --target 25
  python3 scripts/refresh_proxies.py --max-candidates 5000
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import threading
import time
import urllib.request

REPO_ROOT         = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROXIES_DIR       = os.path.join(REPO_ROOT, "proxies")
# Default to repo-relative paths so the script works in both local and
# remote (checked-out repo) environments. Overridable via --master-file /
# --alive-file. Legacy /root/*.json paths are merged on first run.
MASTER_FILE       = os.path.join(PROXIES_DIR, "unique.json")
ALIVE_FILE        = os.path.join(PROXIES_DIR, "alive.json")
LEGACY_MASTER     = "/root/unique_working_proxies.json"
LEGACY_ALIVE      = "/root/proxies_alive.json"

IP_CHECK_URL      = "https://api.ipify.org?format=json"
YNET_LIST_URL     = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
YNET_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Origin":  "https://www.ynet.co.il",
    "Referer": "https://www.ynet.co.il/news/article/yokra14737379",
}

# (scheme, url) — scheme is the default if lines don't carry a scheme://
SOURCES = [
    # TheSpeedX
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    # monosans
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    # mmpx12
    ("http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    # roosterkid
    ("http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    # jetkai
    ("http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    # ShiftyTR (archived but often still reachable)
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    # clarketm
    ("http",   "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    # sunny9577
    ("http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    # hookzof (socks5)
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    # proxifly (mixed — lines carry their own scheme)
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
]


def fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def parse_list(scheme: str, body: str):
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            s, _, addr = line.partition("://")
            s = s.lower()
            if s in ("http", "https"):
                s = "http"
            elif s not in ("socks4", "socks5"):
                s = scheme
            out.append((s, addr))
        else:
            out.append((scheme, line))
    return out


def gather_candidates(verbose=True):
    candidates = []
    errors = 0
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch, url): (scheme, url) for scheme, url in SOURCES}
        for fut in cf.as_completed(futs):
            scheme, url = futs[fut]
            try:
                body = fut.result()
                parsed = parse_list(scheme, body)
                candidates.extend(parsed)
                if verbose:
                    print(f"  [OK]  {url.split('/')[-1]:<28} {len(parsed):>6} entries")
            except Exception as exc:
                errors += 1
                if verbose:
                    print(f"  [ERR] {url.split('/')[-1]:<28} {exc}")
    before = len(candidates)
    candidates = list(dict.fromkeys(candidates))
    if verbose:
        print(f"\n  Raw: {before}  Dedup'd: {len(candidates)}  (source errors: {errors})")
    return candidates


def load_known(master_file=None, alive_file=None):
    master_file = master_file or MASTER_FILE
    alive_file  = alive_file  or ALIVE_FILE
    known_addrs, known_ips, existing = set(), set(), []
    # Include legacy /root/ paths so a first-run remote agent doesn't
    # re-probe proxies that were already validated locally.
    for path in (master_file, alive_file, LEGACY_MASTER, LEGACY_ALIVE):
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
                if isinstance(data, dict):
                    data = data.get("proxies", [])
                for p in data:
                    if p.get("addr"):
                        known_addrs.add(p["addr"])
                    if p.get("exit_ip"):
                        known_ips.add(p["exit_ip"])
                    if path == master_file:
                        existing.append(p)
            except Exception:
                pass
    return existing, known_addrs, known_ips


def probe_worker(entry, timeout, seen_ips, lock, stop_event, target_n, found):
    import requests
    if stop_event.is_set():
        return
    scheme, addr = entry
    px = {"http": f"{scheme}://{addr}", "https": f"{scheme}://{addr}"}
    try:
        r = requests.get(IP_CHECK_URL, proxies=px, timeout=timeout)
        ip = r.json().get("ip")
    except Exception:
        return
    if not ip:
        return
    with lock:
        if ip in seen_ips:
            return
        seen_ips.add(ip)
    try:
        t0 = time.time()
        r = requests.get(YNET_LIST_URL, headers=YNET_HEADERS, proxies=px, timeout=timeout * 2)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200 and "rss" in r.text[:200]:
            rec = {"scheme": scheme, "addr": addr, "exit_ip": ip, "ynet_ms": ms}
            with lock:
                found.append(rec)
                n = len(found)
                print(f"  [+] #{n:3d}  {scheme:6s} {addr:24s}  exit={ip:18s}  ynet={ms}ms",
                      flush=True)
                if n >= target_n:
                    stop_event.set()
    except Exception:
        return


def probe_candidates(candidates, target_n, timeout=5, workers=300):
    try:
        import requests  # noqa: F401  — verify dependency up front
    except ImportError:
        print("ERROR: 'requests' is required (pip install requests)", file=sys.stderr)
        sys.exit(1)

    lock      = threading.Lock()
    stop      = threading.Event()
    # seed with already-known exit IPs so we don't re-record them
    _existing, _, known_ips = load_known()
    seen_ips  = set(known_ips)
    found = []

    print(f"Probing {len(candidates)} candidates | target {target_n} new "
          f"| timeout {timeout}s | workers {workers}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe_worker, e, timeout, seen_ips, lock, stop, target_n, found)
                for e in candidates]
        for fut in cf.as_completed(futs):
            if stop.is_set():
                for g in futs:
                    g.cancel()
                break
    print(f"\n  Elapsed: {int(time.time() - t0)}s  Validated: {len(found)}")
    return found


def merge_and_save(new_proxies, master_file):
    existing, known_addrs, known_ips = load_known(master_file=master_file)
    merged = list(existing)
    added = 0
    for p in new_proxies:
        if p["addr"] in known_addrs or p["exit_ip"] in known_ips:
            continue
        merged.append(p)
        known_addrs.add(p["addr"])
        known_ips.add(p["exit_ip"])
        added += 1
    os.makedirs(os.path.dirname(master_file), exist_ok=True)
    with open(master_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"\n  Master file: {master_file}  (+{added} new, {len(merged)} total)")
    return merged


def refresh_alive(master, alive_file):
    """Re-probe the full master list with the lightweight ynet-only check."""
    import requests
    print(f"\n  Re-checking {len(master)} known proxies against ynet (timeout 10s)...")

    def check(p):
        scheme = p.get("scheme", "http")
        addr   = p.get("addr")
        url    = f"{scheme}://{addr}"
        t0 = time.time()
        try:
            r = requests.get(YNET_LIST_URL, headers=YNET_HEADERS,
                             proxies={"http": url, "https": url}, timeout=10)
            ms = int((time.time() - t0) * 1000)
            alive = r.status_code == 200 and ("rss" in r.text[:500] or "item" in r.text[:500])
            return {**p, "check_status": r.status_code, "check_ms": ms, "alive": bool(alive)}
        except Exception as exc:
            return {**p, "check_status": f"ERR:{type(exc).__name__}",
                    "check_ms": int((time.time() - t0) * 1000), "alive": False}

    with cf.ThreadPoolExecutor(max_workers=40) as ex:
        results = list(ex.map(check, master))
    alive = sorted([r for r in results if r["alive"]], key=lambda x: x["check_ms"])
    os.makedirs(os.path.dirname(alive_file), exist_ok=True)
    with open(alive_file, "w", encoding="utf-8") as f:
        json.dump(alive, f, indent=2, ensure_ascii=False)
    print(f"  Alive file : {alive_file}  ({len(alive)}/{len(master)} alive)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=50,
                    help="target number of NEW validated proxies to add (default 50)")
    ap.add_argument("--max-candidates", type=int, default=20000,
                    help="cap on candidates to probe (default 20000)")
    ap.add_argument("--timeout", type=int, default=5,
                    help="per-request timeout in seconds (default 5)")
    ap.add_argument("--workers", type=int, default=300,
                    help="parallel probe workers (default 300)")
    ap.add_argument("--skip-health-refresh", action="store_true",
                    help="don't re-check the whole pool at the end")
    ap.add_argument("--master-file", default=MASTER_FILE,
                    help=f"path to the master pool JSON (default {MASTER_FILE})")
    ap.add_argument("--alive-file", default=ALIVE_FILE,
                    help=f"path to the alive-pool JSON (default {ALIVE_FILE})")
    args = ap.parse_args()

    print("=" * 70)
    print(" Refreshing ynet proxy pool")
    print("=" * 70)
    print(f"  Master file: {args.master_file}")
    print(f"  Alive file : {args.alive_file}")
    print()

    print("Fetching source lists...")
    candidates = gather_candidates()

    existing, known_addrs, _known_ips = load_known(
        master_file=args.master_file, alive_file=args.alive_file)
    fresh = [c for c in candidates if c[1] not in known_addrs]
    import random
    random.shuffle(fresh)
    fresh = fresh[: args.max_candidates]
    print(f"  Candidates after excluding {len(known_addrs)} known addrs: {len(fresh)}\n")

    found = probe_candidates(fresh, args.target, timeout=args.timeout, workers=args.workers)
    merged = merge_and_save(found, args.master_file)
    if not args.skip_health_refresh:
        refresh_alive(merged, args.alive_file)
    print("\nDone.")


if __name__ == "__main__":
    main()
