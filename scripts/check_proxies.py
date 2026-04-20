#!/usr/bin/env python3
"""Health-check all proxies in unique_working_proxies.json against ynet.

Runs in parallel, writes alive proxies (sorted by latency) to proxies_alive.json.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC       = os.path.join(_REPO_ROOT, "proxies", "unique.json")
DST       = os.path.join(_REPO_ROOT, "proxies", "alive.json")
TEST_URL  = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
TIMEOUT   = 10
WORKERS   = 30
UA        = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def probe(entry: dict) -> dict:
    scheme = entry.get("scheme", "http")
    addr   = entry["addr"]
    url    = f"{scheme}://{addr}"
    proxies = {"http": url, "https": url}
    t0 = time.monotonic()
    try:
        r = requests.get(TEST_URL, proxies=proxies, timeout=TIMEOUT,
                         headers={"User-Agent": UA})
        ms = int((time.monotonic() - t0) * 1000)
        ok = r.status_code == 200 and "item" in r.text[:500] or "rss" in r.text[:500]
        return {**entry, "check_status": r.status_code, "check_ms": ms, "alive": bool(ok)}
    except Exception as exc:
        return {**entry, "check_status": f"ERR:{type(exc).__name__}",
                "check_ms": int((time.monotonic() - t0) * 1000), "alive": False}


def main():
    with open(SRC, encoding="utf-8") as f:
        proxies = json.load(f)
    print(f"Checking {len(proxies)} proxies against ynet (timeout={TIMEOUT}s, workers={WORKERS})...")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(probe, p): p for p in proxies}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            flag = "OK " if r["alive"] else "-- "
            print(f"  [{i:3}/{len(proxies)}] {flag} {r['addr']:<22} "
                  f"status={r['check_status']!s:<20} {r['check_ms']:>5}ms")

    alive = sorted([r for r in results if r["alive"]], key=lambda x: x["check_ms"])
    with open(DST, "w", encoding="utf-8") as f:
        json.dump(alive, f, indent=2, ensure_ascii=False)

    print(f"\nTotal checked : {len(results)}")
    print(f"Alive         : {len(alive)}")
    print(f"Dead          : {len(results) - len(alive)}")
    print(f"Written to    : {DST}")


if __name__ == "__main__":
    main()
