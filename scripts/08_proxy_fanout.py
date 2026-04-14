#!/usr/bin/env python3
"""
Script 08: Fan out like-votes through a pool of public HTTP/SOCKS proxies.

Pipeline per proxy:
  1. Liveness probe (GET api.ipify.org) — confirms the proxy forwards and
     reveals the exit IP Ynet will see.
  2. If exit IP is new in this run, POST one vote to the Ynet vote endpoint
     through the same proxy.
  3. Record: exit IP, scheme, latency, HTTP status, set-cookie, body.

The run stops as soon as TARGET_N successful distinct-IP votes are collected.

Proxy lists are pulled from public GitHub mirrors and deduped.
"""
import argparse
import concurrent.futures as cf
import json
import os
import threading
import time
from datetime import datetime

import requests

DEFAULTS = {
    "article_id":  "yokra14737379",
    "talkback_id": 99004846,
    "like":        True,
    "target_n":    10,
    "workers":     80,
    "timeout":     6,
    "proxy_lists": [
        ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
        ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
        ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
        ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ],
}

YNET_BASE  = "https://www.ynet.co.il"
VOTE_URL   = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
IP_CHECK   = "https://api.ipify.org?format=json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0",
    "Origin":     YNET_BASE,
    "Content-Type": "application/json",
}


def fetch_proxy_pool(sources):
    pool = []
    for scheme, url in sources:
        try:
            r = requests.get(url, timeout=10)
            for line in r.text.splitlines():
                line = line.strip()
                if line:
                    pool.append((scheme, line))
        except Exception as e:
            print(f"  ! could not fetch {url}: {e}")
    # de-dup while preserving order
    return list(dict.fromkeys(pool))


def make_worker(cfg, state):
    payload = {
        "article_id":      cfg["article_id"],
        "talkback_id":     cfg["talkback_id"],
        "talkback_like":   bool(cfg["like"]),
        "talkback_unlike": not bool(cfg["like"]),
        "vote_type":       "2state",
    }
    headers = {**HEADERS, "Referer": f"{YNET_BASE}/news/article/{cfg['article_id']}"}
    timeout = cfg["timeout"]
    target_n = cfg["target_n"]

    def worker(entry):
        if state["stop"].is_set():
            return
        scheme, addr = entry
        px = {"http": f"{scheme}://{addr}", "https": f"{scheme}://{addr}"}
        try:
            ip = requests.get(IP_CHECK, proxies=px, timeout=timeout).json().get("ip")
        except Exception:
            return
        if not ip:
            return
        with state["lock"]:
            if ip in state["seen_ips"]:
                return
            state["seen_ips"].add(ip)
        try:
            r = requests.post(VOTE_URL, json=payload, headers=headers,
                              proxies=px, timeout=timeout * 2)
        except Exception as e:
            with state["lock"]:
                state["results"].append({"exit_ip": ip, "proxy": addr,
                                          "scheme": scheme, "error": str(e)[:120]})
            return
        rec = {
            "exit_ip":   ip,
            "proxy":     addr,
            "scheme":    scheme,
            "status":    r.status_code,
            "body":      r.text[:200],
            "set_cookie": r.headers.get("Set-Cookie", "")[:200],
        }
        with state["lock"]:
            state["results"].append(rec)
            if r.status_code == 200 and '"success"' in r.text:
                state["accepted"].append(rec)
                print(f"  ✓ #{len(state['accepted'])}  {scheme}://{addr:24s}"
                      f"  exit={ip:18s}  {r.text[:60]}")
                if len(state["accepted"]) >= target_n:
                    state["stop"].set()
    return worker


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--article-id", default=DEFAULTS["article_id"])
    p.add_argument("--talkback-id", type=int, default=DEFAULTS["talkback_id"])
    p.add_argument("--dislike", action="store_true",
                   help="Send unlikes instead of likes")
    p.add_argument("--target-n", type=int, default=DEFAULTS["target_n"],
                   help="Stop after this many successful distinct-IP votes")
    p.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    p.add_argument("--timeout", type=int, default=DEFAULTS["timeout"],
                   help="Per-request timeout (seconds)")
    p.add_argument("--out", default=None,
                   help="Write JSON results to this path (default: results/proxy_fanout_<ts>.json)")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = {
        "article_id":  args.article_id,
        "talkback_id": args.talkback_id,
        "like":        not args.dislike,
        "target_n":    args.target_n,
        "workers":     args.workers,
        "timeout":     args.timeout,
    }
    print("=" * 60)
    print("  Ynet proxy-rotation vote fan-out")
    print("=" * 60)
    print(f"  article_id  : {cfg['article_id']}")
    print(f"  talkback_id : {cfg['talkback_id']}")
    print(f"  direction   : {'like' if cfg['like'] else 'unlike'}")
    print(f"  target votes: {cfg['target_n']}")
    print(f"  workers     : {cfg['workers']}")
    print()

    pool = fetch_proxy_pool(DEFAULTS["proxy_lists"])
    print(f"Loaded {len(pool)} candidate proxies.\n")

    state = {
        "results":  [],
        "accepted": [],
        "seen_ips": set(),
        "stop":     threading.Event(),
        "lock":     threading.Lock(),
    }
    worker = make_worker(cfg, state)

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = [ex.submit(worker, e) for e in pool]
        for f in cf.as_completed(futs):
            if state["stop"].is_set():
                for g in futs:
                    g.cancel()
                break
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Candidates tested : {len(pool)}")
    print(f"  Proxies responded : {len(state['seen_ips'])}")
    print(f"  Votes accepted    : {len(state['accepted'])}")
    print("=" * 60)
    for s in state["accepted"]:
        print(f"  {s['exit_ip']:18s}  via {s['scheme']}://{s['proxy']}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.out or os.path.join(
        script_dir, "..", "results", f"proxy_fanout_{ts}.json"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "run_ts":      ts,
            "config":      cfg,
            "elapsed_sec": round(elapsed, 2),
            "candidates":  len(pool),
            "accepted":    state["accepted"],
            "all_results": state["results"],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to {os.path.normpath(out_path)}")


if __name__ == "__main__":
    main()
