#!/usr/bin/env python3
"""
Fast async proxy screener — tests all master_pool entries in minutes.

Uses aiohttp (asyncio, NOT threads) so concurrency can be 300+ without
hitting proot's 150-thread SIGKILL limit.

Test: GET /iphone/json/api/talkbacks/list/v2/{article_id}/0/1 through proxy.
A response of any status code (<500 or even Ynet's own error page) means
the proxy can reach Ynet — without burning any vote capacity.

Usage:
    python3 fast_screener.py                  # screens master_pool once, updates alive.json
    python3 fast_screener.py --loop           # runs continuously every SLEEP_BETWEEN seconds
    python3 fast_screener.py --concurrency 400
"""

import argparse
import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER   = os.path.join(BASE_DIR, "proxies", "master_pool.json")
ALIVE    = os.path.join(BASE_DIR, "proxies", "alive.json")
YNET_BASE = "https://www.ynet.co.il"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     YNET_BASE,
    "Referer":    f"{YNET_BASE}/",
}
DEFAULT_ARTICLE = "yokra14737379"
SERVER_RELOAD   = "http://127.0.0.1:5001/admin/reload"
SERVER_ARTICLES = "http://127.0.0.1:5001/api/known_articles"
MIN_SURVIVORS   = 30
SLEEP_BETWEEN   = 300  # seconds between loops


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_master():
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


def get_test_url():
    try:
        req = urllib.request.urlopen(SERVER_ARTICLES, timeout=4)
        data = json.loads(req.read())
        ids = data.get("article_ids", [])
        if ids:
            return f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{ids[0]}/0/1"
    except Exception:
        pass
    return f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{DEFAULT_ARTICLE}/0/1"


async def probe_one(entry, test_url, timeout_s, semaphore, results, counter, total):
    scheme = entry["scheme"]
    addr   = entry["addr"]
    proxy_url = f"{scheme}://{addr}"

    async with semaphore:
        t0 = time.monotonic()
        try:
            if scheme in ("socks4", "socks5"):
                ptype = ProxyType.SOCKS5 if scheme == "socks5" else ProxyType.SOCKS4
                host, port = addr.rsplit(":", 1)
                connector = ProxyConnector(proxy_type=ptype, host=host, port=int(port),
                                           ssl=False, enable_cleanup_closed=True)
                async with aiohttp.ClientSession(
                    connector=connector, headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=timeout_s)
                ) as sess:
                    async with sess.get(test_url, ssl=False) as resp:
                        ms = int((time.monotonic() - t0) * 1000)
                        if resp.status < 600:
                            results.append({
                                "scheme":  scheme,
                                "addr":    addr,
                                "exit_ip": entry.get("exit_ip", addr.split(":")[0]),
                                "ynet_ms": ms,
                            })
            else:
                connector = aiohttp.TCPConnector(ssl=False, enable_cleanup_closed=True)
                async with aiohttp.ClientSession(
                    connector=connector, headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=timeout_s)
                ) as sess:
                    async with sess.get(test_url, proxy=proxy_url, ssl=False) as resp:
                        ms = int((time.monotonic() - t0) * 1000)
                        if resp.status < 600:
                            results.append({
                                "scheme":  scheme,
                                "addr":    addr,
                                "exit_ip": entry.get("exit_ip", addr.split(":")[0]),
                                "ynet_ms": ms,
                            })
        except Exception:
            pass
        finally:
            async with counter["lock"]:
                counter["done"] += 1
                done = counter["done"]
            if done % 500 == 0 or done == total:
                log(f"  {done}/{total}  alive: {len(results)}")


async def run_screen(master, concurrency, timeout_s):
    test_url = get_test_url()
    log(f"Test URL: {test_url}")
    log(f"Concurrency: {concurrency}  Timeout: {timeout_s}s")

    semaphore = asyncio.Semaphore(concurrency)
    results = []
    counter = {"done": 0, "lock": asyncio.Lock()}

    tasks = [
        probe_one(entry, test_url, timeout_s, semaphore, results, counter, len(master))
        for entry in master
    ]

    t0 = time.monotonic()
    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t0

    log(f"Sweep done in {elapsed:.0f}s  —  {len(results)} alive from {len(master)} tested")
    return results


def merge_and_save(new_hits):
    # Merge with existing alive.json — keep entries not re-tested (from other sources)
    hit_addrs = {h["addr"] for h in new_hits}
    try:
        existing = json.load(open(ALIVE))
        for p in existing:
            if p["addr"] not in hit_addrs:
                new_hits.append(p)
    except Exception:
        pass

    if len(new_hits) < MIN_SURVIVORS:
        log(f"  only {len(new_hits)} survivors — keeping previous alive.json")
        return len(new_hits)

    new_hits.sort(key=lambda x: x.get("ynet_ms", 9999))
    atomic_write(ALIVE, new_hits)
    log(f"  wrote {len(new_hits)} proxies to alive.json")
    reload_server()
    return len(new_hits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=300)
    ap.add_argument("--timeout",     type=float, default=5.0)
    ap.add_argument("--loop",        action="store_true", help="Run continuously")
    ap.add_argument("--sleep",       type=int, default=SLEEP_BETWEEN,
                    help="Seconds between loops (default 300)")
    args = ap.parse_args()

    while True:
        master = load_master()
        if not master:
            log("master_pool.json empty or missing — nothing to screen")
        else:
            log(f"=== fast_screener start: {len(master)} proxies ===")
            hits = asyncio.run(run_screen(master, args.concurrency, args.timeout))
            total = merge_and_save(hits)
            log(f"=== done: {total} in alive.json ===\n")

        if not args.loop:
            break
        log(f"Sleeping {args.sleep}s before next sweep...")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
