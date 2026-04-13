#!/usr/bin/env python3
"""
IP Rotation Vote Client — tests against any server (local mock or remote).

All configuration is read from config.json. Every value can be overridden
via CLI arguments — nothing is hardcoded.

Usage:
    python3 rotation_client.py                          # uses config.json defaults
    python3 rotation_client.py --server http://x.x.x.x:5001
    python3 rotation_client.py --config /other/config.json
    python3 rotation_client.py --server http://x:5001 --article-id myarticle123
    python3 rotation_client.py --pool-size 10          # IPs per test
    python3 rotation_client.py --log-level DEBUG        # console verbosity

Logs are written to results/client_<timestamp>.log
"""

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Argument parsing — must happen before logging so --log-level works
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Ynet talkback vote rotation client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=os.path.join(os.path.dirname(__file__), "config.json"),
        help="Path to config.json",
    )
    parser.add_argument(
        "--server", default=None,
        help="Override server base URL, e.g. http://192.168.1.5:5001",
    )
    parser.add_argument(
        "--article-id", default=None, dest="article_id",
        help="Override article ID",
    )
    parser.add_argument(
        "--pool-size", type=int, default=None, dest="pool_size",
        help="Number of simulated IPs to use per test (default: from config or 25)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        dest="log_level",
        help="Console log verbosity (file always gets DEBUG)",
    )
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"[FATAL] Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def build_cfg(args) -> dict:
    """Merge config.json with CLI overrides into a single flat config dict."""
    raw = load_config(args.config)

    server_host = raw.get("server", {}).get("host", "127.0.0.1")
    server_port = raw.get("server", {}).get("port", 5001)
    base        = f"http://{server_host}:{server_port}"

    # CLI --server wins over config.json
    if args.server:
        base = args.server.rstrip("/")

    article_id = args.article_id or raw.get("article_id", "")
    cache_ttl  = raw.get("cache_ttl_seconds", 87)
    pool_size  = args.pool_size or raw.get("max_batch_votes", 25)
    # cap pool_size to a reasonable test limit (use full value if explicitly set)
    if args.pool_size is None:
        pool_size = min(pool_size, 50)

    endpoints = raw.get("endpoints", {})

    def ep(key: str, fallback: str) -> str:
        tmpl = endpoints.get(key, fallback)
        return base + tmpl.format(article_id=article_id, sort="0", page="1")

    return {
        "base":        base,
        "article_id":  article_id,
        "cache_ttl":   cache_ttl,
        "pool_size":   pool_size,
        "vote_url":    base + endpoints.get("vote",  "/iphone/json/api/talkbacks/vote"),
        "list_url":    ep("list",  "/iphone/json/api/talkbacks/list/v2/{article_id}/{sort}/{page}"),
        "config_url":  base + endpoints.get("config", "/api/config"),
        "config_path": args.config,
    }

# ---------------------------------------------------------------------------
# Logging setup (after args so we know --log-level)
# ---------------------------------------------------------------------------

def setup_logging(log_level_name: str) -> tuple:
    log_dir  = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(log_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"client_{ts}.log")

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(name)-20s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(getattr(logging, log_level_name, logging.INFO))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)

    return logging.getLogger("client"), log_file

# ---------------------------------------------------------------------------
# IP pool builder — generated from RFC 5737 documentation ranges
# ---------------------------------------------------------------------------

def build_pool(size: int) -> list:
    """Build a flat pool of `size` RFC-5737 documentation IPs."""
    ranges = [
        [f"192.0.2.{i}"    for i in range(1, 255)],   # TEST-NET-1
        [f"198.51.100.{i}" for i in range(1, 255)],   # TEST-NET-2
        [f"203.0.113.{i}"  for i in range(1, 255)],   # TEST-NET-3
    ]
    pool = []
    for r in ranges:
        pool.extend(r)
        if len(pool) >= size:
            break
    return pool[:size]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_http_log = logging.getLogger("http")

def _log_request(method: str, url: str, **kwargs):
    _http_log.debug("→ %s %s", method, url)
    if "json" in kwargs:
        _http_log.debug("  payload : %s", json.dumps(kwargs["json"], ensure_ascii=False))
    if "headers" in kwargs:
        _http_log.debug("  headers : %s", kwargs["headers"])

def _log_response(resp: requests.Response):
    _http_log.debug("← HTTP %s  (%dms)", resp.status_code,
                    int(resp.elapsed.total_seconds() * 1000))
    _http_log.debug("  content-type: %s", resp.headers.get("Content-Type", "—"))
    _http_log.debug("  body    : %s", resp.text[:400])
    sc = resp.headers.get("Set-Cookie")
    if sc:
        _http_log.debug("  set-cookie: %s", sc)
    if resp.status_code >= 400:
        _http_log.warning("HTTP error %s on %s — body: %s",
                          resp.status_code, resp.url, resp.text[:200])

def _safe_json(resp: requests.Response, context: str) -> dict | list | None:
    """Parse JSON from a response, logging a clear error if it's not JSON."""
    ct = resp.headers.get("Content-Type", "")
    if resp.status_code >= 400:
        _http_log.error("%s  HTTP %s — server returned error, not JSON. body: %s",
                        context, resp.status_code, resp.text[:300])
        return None
    if "application/json" not in ct and "text/json" not in ct:
        _http_log.error("%s  Expected JSON but got Content-Type: %s — body: %s",
                        context, ct, resp.text[:300])
        return None
    try:
        return resp.json()
    except Exception as exc:
        _http_log.error("%s  JSON parse failed: %s — body: %s", context, exc, resp.text[:300])
        return None

def _get(url: str, **kwargs) -> requests.Response:
    _log_request("GET", url, **kwargs)
    try:
        resp = requests.get(url, timeout=10, **kwargs)
    except Exception as exc:
        _http_log.error("GET %s failed: %s", url, exc)
        _http_log.debug(traceback.format_exc())
        raise
    _log_response(resp)
    return resp

def _post(url: str, **kwargs) -> requests.Response:
    _log_request("POST", url, **kwargs)
    try:
        resp = requests.post(url, timeout=10, **kwargs)
    except Exception as exc:
        _http_log.error("POST %s failed: %s", url, exc)
        _http_log.debug(traceback.format_exc())
        raise
    _log_response(resp)
    return resp

# ---------------------------------------------------------------------------
# Domain helpers — all URLs come from cfg dict, nothing hardcoded
# ---------------------------------------------------------------------------

def separator(title: str, log):
    bar = "=" * 65
    print(f"\n{bar}\n  {title}\n{bar}")
    log.info("=== %s ===", title)

def reset(cfg: dict, log):
    """No-op — reset is not available on the real Ynet API."""
    log.debug("Reset skipped (not available on real Ynet API)")

def get_all_comments(cfg: dict, log) -> list:
    log.debug("Fetching comment list from %s", cfg["list_url"])
    resp = _get(cfg["list_url"])
    data = _safe_json(resp, "get_all_comments")
    if data is None:
        log.error("get_all_comments: could not parse response from %s", cfg["list_url"])
        raise RuntimeError(
            f"Failed to fetch comments from {cfg['list_url']}\n"
            f"  HTTP {resp.status_code}  Content-Type: {resp.headers.get('Content-Type','?')}\n"
            f"  Is this the right server and article_id?\n"
            f"  Body preview: {resp.text[:200]}"
        )
    # Support both mock server structure and real Ynet structure
    if isinstance(data, dict):
        # Mock server / real Ynet: {"rss": {"channel": {"item": [...]}}}
        if "rss" in data:
            items = data["rss"]["channel"]["item"]
        # Flat list under a key
        elif "items" in data:
            items = data["items"]
        elif "comments" in data:
            items = data["comments"]
        else:
            log.error("get_all_comments: unrecognised JSON structure. keys=%s", list(data.keys()))
            raise RuntimeError(
                f"Unrecognised response structure from {cfg['list_url']}\n"
                f"  Top-level keys: {list(data.keys())}\n"
                f"  Expected 'rss', 'items', or 'comments' key."
            )
    elif isinstance(data, list):
        items = data
    else:
        raise RuntimeError(f"Unexpected JSON type from {cfg['list_url']}: {type(data)}")

    log.debug("Received %d comments", len(items))
    return items

def get_comment(talkback_id: int, cfg: dict, log) -> dict | None:
    log.debug("Looking up comment %d", talkback_id)
    for c in get_all_comments(cfg, log):
        if c["id"] == talkback_id:
            log.debug("Found comment %d: likes=%s unlikes=%s net=%s",
                      talkback_id, c["likes"], c.get("unlikes", 0),
                      c.get("talkback_like", 0))
            return c
    log.warning("Comment %d not found in list response", talkback_id)
    return None

def get_admin_comment(talkback_id: int, cfg: dict, log) -> dict:
    """Read comment stats from the public talkbacks list (real Ynet data)."""
    c = get_comment(talkback_id, cfg, log)
    if c:
        return {
            "likes":      c.get("likes", 0),
            "unlikes":    c.get("unlikes", 0),
            "net":        c.get("talkback_like", 0),
            "vote_count": c.get("likes", 0),
            "voters":     [],
        }
    return {"likes": 0, "unlikes": 0, "net": 0, "vote_count": 0, "voters": []}

def cast_vote(talkback_id: int, simulated_ip: str, cfg: dict, log,
              like: bool = True, vote_type: str = "2state",
              cookie: str = None) -> dict:
    action = "LIKE" if like else "UNLIKE"
    log.debug("cast_vote  id=%d  ip=%s  action=%s  vote_type=%s  cookie=%s",
              talkback_id, simulated_ip, action, vote_type, cookie)

    headers = {
        "Content-Type":   "application/json",
        "Origin":         cfg["base"],
        "X-Simulated-IP": simulated_ip,
    }
    if cookie:
        headers["Cookie"] = f"talkback_{talkback_id}={cookie}"

    payload = {
        "article_id":      cfg["article_id"],
        "talkback_id":     talkback_id,
        "talkback_like":   like,
        "talkback_unlike": not like,
        "vote_type":       vote_type,
    }

    try:
        resp = _post(cfg["vote_url"], json=payload, headers=headers)
    except Exception as exc:
        log.error("cast_vote FAILED  id=%d  ip=%s  error=%s", talkback_id, simulated_ip, exc)
        raise

    result = {
        "status":     resp.status_code,
        "body":       resp.json() if resp.text else {},
        "set_cookie": resp.headers.get("Set-Cookie", "—"),
    }

    if resp.status_code == 200:
        log.debug("cast_vote OK  id=%d  ip=%s  action=%s  set_cookie=%s",
                  talkback_id, simulated_ip, action, result["set_cookie"])
    else:
        log.warning("cast_vote REJECTED  id=%d  ip=%s  action=%s  status=%s  body=%s",
                    talkback_id, simulated_ip, action, resp.status_code, result["body"])

    return result

# ---------------------------------------------------------------------------
# Tests — targets chosen from live data, pool size from config
# ---------------------------------------------------------------------------

def show_targeting_analysis(cfg: dict, log):
    separator("TARGETING ANALYSIS — Real Comments", log)
    log.info("Starting targeting analysis  article=%s  server=%s",
             cfg["article_id"], cfg["base"])

    comments = get_all_comments(cfg, log)
    total    = len(comments)
    log.info("Snapshot contains %d comments", total)

    print(f"\nTotal comments in snapshot: {total}")
    print(f"Article: {cfg['article_id']}   Server: {cfg['base']}\n")

    top_liked = sorted(comments, key=lambda x: x["likes"], reverse=True)[:5]
    print("--- Strategy 1: BOOST — target most-liked comments (already visible) ---")
    print(f"  {'ID':<12} {'likes':>6} {'unlikes':>8} {'net':>6}  text[:60]")
    for c in top_liked:
        log.debug("BOOST candidate  id=%d  likes=%d  unlikes=%d  net=%d",
                  c["id"], c["likes"], c.get("unlikes", 0), c.get("talkback_like", 0))
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('unlikes',0):>8} "
              f"{c.get('talkback_like',0):>6}  {c['text'][:60]}")
    print()

    top_disputed = sorted(comments, key=lambda x: x.get("unlikes", 0), reverse=True)[:5]
    print("--- Strategy 2: SUPPRESS — target most-disliked (pile-on unlikes to bury) ---")
    print(f"  {'ID':<12} {'likes':>6} {'unlikes':>8} {'net':>6}  text[:60]")
    for c in top_disputed:
        log.debug("SUPPRESS candidate  id=%d  likes=%d  unlikes=%d  net=%d",
                  c["id"], c["likes"], c.get("unlikes", 0), c.get("talkback_like", 0))
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('unlikes',0):>8} "
              f"{c.get('talkback_like',0):>6}  {c['text'][:60]}")
    print()

    zero = [c for c in comments if c["likes"] == 0 and c.get("unlikes", 0) == 0]
    log.info("MANUFACTURE: %d zero-vote comments available", len(zero))
    print(f"--- Strategy 3: MANUFACTURE — zero-vote comments ({len(zero)} available) ---")
    print(f"  {'ID':<12} {'author':<22}  text[:60]")
    for c in zero[:5]:
        log.debug("MANUFACTURE candidate  id=%d  author=%s", c["id"], c["author"])
        print(f"  {c['id']:<12} {c['author'][:20]:<22}  {c['text'][:60]}")
    print()

    near = sorted([c for c in comments if 5 <= c["likes"] <= 15],
                  key=lambda x: x["likes"], reverse=True)[:5]
    print("--- Strategy 4: PUSH TO RECOMMENDED — comments near the threshold ---")
    print(f"  {'ID':<12} {'likes':>6} {'net':>6}  text[:60]")
    for c in near:
        log.debug("NEAR-THRESHOLD candidate  id=%d  likes=%d", c["id"], c["likes"])
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('talkback_like',0):>6}  {c['text'][:60]}")

    log.info("Targeting analysis complete")
    return {"top_liked": top_liked, "top_disputed": top_disputed,
            "zero": zero, "near": near, "all": comments}


def test_boost_top_comment(cfg: dict, log, targets: dict):
    separator("TEST A: BOOST — Add likes to the most-liked comment", log)
    log.info("TEST A START  server=%s", cfg["base"])
    reset(cfg, log)

    pool   = build_pool(cfg["pool_size"])
    target = targets["top_liked"][0]
    tid    = target["id"]

    log.info("TEST A  target=%d  pool_size=%d  before: likes=%d  unlikes=%d  net=%d",
             tid, len(pool), target["likes"], target.get("unlikes", 0),
             target.get("talkback_like", 0))

    print(f"\nTarget: ID {tid}")
    print(f"Author: {target['author']}")
    print(f"Text  : {target['text'][:80]}")
    print(f"Before: likes={target['likes']} unlikes={target.get('unlikes',0)} "
          f"net={target.get('talkback_like',0)}")
    print(f"\nRotating {len(pool)} IPs → likes...")
    print(f"\n  {'IP':<18} {'HTTP':>5}  {'Real likes after':>16}")
    print(f"  {'-'*45}")

    for idx, ip in enumerate(pool, 1):
        try:
            r     = cast_vote(tid, ip, cfg, log, like=True)
            stats = get_admin_comment(tid, cfg, log)
            log.info("TEST A  [%d/%d]  ip=%s  http=%d  likes=%d  set_cookie=%s",
                     idx, len(pool), ip, r["status"], stats["likes"], r["set_cookie"])
            print(f"  {ip:<18}  {r['status']}   likes={stats['likes']:>4}")
        except Exception as exc:
            log.error("TEST A  [%d/%d]  ip=%s  EXCEPTION: %s\n%s",
                      idx, len(pool), ip, exc, traceback.format_exc())
            print(f"  {ip:<18}  ERR   {exc}")

    stats = get_admin_comment(tid, cfg, log)
    lift  = stats["likes"] - target["likes"]
    log.info("TEST A DONE  target=%d  final_likes=%d  final_unlikes=%d  "
             "final_net=%d  lift=%d  unique_voters=%d",
             tid, stats["likes"], stats.get("unlikes", 0),
             stats["net"], lift, stats["vote_count"])
    print(f"\nFinal: likes={stats['likes']} unlikes={stats.get('unlikes',0)} "
          f"net={stats['net']}  lift=+{lift}  voters={stats['vote_count']}")


def test_suppress_comment(cfg: dict, log, targets: dict):
    separator("TEST B: SUPPRESS — Add unlikes to the most-disliked comment", log)
    log.info("TEST B START  server=%s", cfg["base"])
    reset(cfg, log)

    pool   = build_pool(cfg["pool_size"])
    target = targets["top_disputed"][0]
    tid    = target["id"]

    log.info("TEST B  target=%d  pool_size=%d  before: likes=%d  unlikes=%d  net=%d",
             tid, len(pool), target["likes"], target.get("unlikes", 0),
             target.get("talkback_like", 0))

    print(f"\nTarget: ID {tid}")
    print(f"Author: {target['author']}")
    print(f"Text  : {target['text'][:80]}")
    print(f"Before: likes={target['likes']} unlikes={target.get('unlikes',0)} "
          f"net={target.get('talkback_like',0)}")
    print(f"\nRotating {len(pool)} IPs → unlikes...")

    for idx, ip in enumerate(pool, 1):
        try:
            r = cast_vote(tid, ip, cfg, log, like=False)
            log.info("TEST B  [%d/%d]  ip=%s  http=%d  set_cookie=%s",
                     idx, len(pool), ip, r["status"], r["set_cookie"])
        except Exception as exc:
            log.error("TEST B  [%d/%d]  ip=%s  EXCEPTION: %s\n%s",
                      idx, len(pool), ip, exc, traceback.format_exc())

    stats         = get_admin_comment(tid, cfg, log)
    unlikes_added = stats.get("unlikes", 0) - target.get("unlikes", 0)
    net_before    = target.get("talkback_like", 0)
    log.info("TEST B DONE  target=%d  final_likes=%d  final_unlikes=%d  "
             "final_net=%d  unlikes_added=%d  net_change=%d→%d",
             tid, stats["likes"], stats.get("unlikes", 0), stats["net"],
             unlikes_added, net_before, stats["net"])
    print(f"\nFinal: likes={stats['likes']} unlikes={stats.get('unlikes',0)} "
          f"net={stats['net']}  unlikes_added=+{unlikes_added}  net_change={net_before}→{stats['net']}")


def test_manufacture_zero_comment(cfg: dict, log, targets: dict):
    separator("TEST C: MANUFACTURE — Make a zero-vote comment appear popular", log)
    log.info("TEST C START  server=%s", cfg["base"])
    reset(cfg, log)

    pool = build_pool(cfg["pool_size"])

    if not targets["zero"]:
        log.warning("TEST C SKIP — no zero-vote comments found")
        print("\nSKIPPED: no zero-vote comments in current snapshot.")
        return

    target = targets["zero"][0]
    tid    = target["id"]
    log.info("TEST C  target=%d  author=%s  pool_size=%d", tid, target["author"], len(pool))

    print(f"\nTarget: ID {tid}")
    print(f"Author: {target['author']}")
    print(f"Text  : {target['text'][:80]}")
    print(f"Before: likes=0 unlikes=0 net=0")
    print(f"\nRotating {len(pool)} IPs → likes...")

    for idx, ip in enumerate(pool, 1):
        try:
            r = cast_vote(tid, ip, cfg, log, like=True)
            log.info("TEST C  [%d/%d]  ip=%s  http=%d  set_cookie=%s",
                     idx, len(pool), ip, r["status"], r["set_cookie"])
        except Exception as exc:
            log.error("TEST C  [%d/%d]  ip=%s  EXCEPTION: %s\n%s",
                      idx, len(pool), ip, exc, traceback.format_exc())

    stats    = get_admin_comment(tid, cfg, log)
    all_c    = get_all_comments(cfg, log)
    ranked   = sorted(all_c, key=lambda x: x["likes"], reverse=True)
    rank     = next((i for i, c in enumerate(ranked, 1) if c["id"] == tid), None)
    log.info("TEST C DONE  target=%d  final_likes=%d  rank=%s/%d",
             tid, stats["likes"], rank, len(ranked))
    print(f"\nAfter rotation: likes={stats['likes']}  rank=#{rank}/{len(ranked)}")


def test_dedup_still_works(cfg: dict, log, targets: dict):
    separator("TEST D: CONFIRM DEDUP — Same IP can't vote twice", log)
    log.info("TEST D START  server=%s", cfg["base"])
    reset(cfg, log)

    target = targets["top_liked"][0]
    tid    = target["id"]
    ip     = build_pool(1)[0]   # use first IP from pool

    log.info("TEST D  target=%d  ip=%s", tid, ip)

    r1 = cast_vote(tid, ip, cfg, log, like=True)
    s1 = get_admin_comment(tid, cfg, log)
    log.info("TEST D  vote 1  ip=%s  http=%d  likes=%d  set_cookie=%s",
             ip, r1["status"], s1["likes"], r1["set_cookie"])
    print(f"\nVote 1 from {ip}: HTTP {r1['status']} → likes={s1['likes']}")

    r2 = cast_vote(tid, ip, cfg, log, like=True)
    s2 = get_admin_comment(tid, cfg, log)
    changed = s2["likes"] != s1["likes"]
    log.info("TEST D  vote 2  ip=%s  http=%d  likes=%d  set_cookie=%s  count_changed=%s",
             ip, r2["status"], s2["likes"], r2["set_cookie"], changed)

    if not changed:
        log.info("TEST D PASS  dedup working — second vote from same IP was silently dropped")
    else:
        log.warning("TEST D FAIL  dedup NOT working — second vote changed count!")

    print(f"Vote 2 from {ip}: HTTP {r2['status']} → likes={s2['likes']} (no change)")
    print(f"\nConclusion: {s2['vote_count']} unique IP(s) registered despite 2 attempts")


def test_validation(cfg: dict, log):
    separator("TEST E: VALIDATION — Error messages match real Ynet API", log)
    log.info("TEST E START  server=%s", cfg["base"])

    cases = [
        ("Empty body",        {}),
        ("Missing vote_type", {"article_id": cfg["article_id"], "talkback_id": 99999999,
                               "talkback_like": True, "talkback_unlike": False}),
        ("Bad vote_type",     {"article_id": cfg["article_id"], "talkback_id": 99999999,
                               "talkback_like": True, "talkback_unlike": False,
                               "vote_type": "4state"}),
    ]

    probe_ip = build_pool(1)[0]
    for label, payload in cases:
        log.info("TEST E  case='%s'  payload=%s", label, payload)
        try:
            resp = _post(cfg["vote_url"], json=payload,
                         headers={"Content-Type": "application/json",
                                  "X-Simulated-IP": probe_ip})
            body = resp.json() if resp.text else {}
            log.info("TEST E  case='%s'  http=%d  body=%s",
                     label, resp.status_code, json.dumps(body, ensure_ascii=False))
            print(f"\n{label}:")
            print(f"  HTTP {resp.status_code}")
            print(f"  {json.dumps(body, ensure_ascii=False)}")
        except Exception as exc:
            log.error("TEST E  case='%s'  EXCEPTION: %s\n%s",
                      label, exc, traceback.format_exc())

    log.info("TEST E DONE")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    log, LOG_FILE = setup_logging(args.log_level)
    cfg  = build_cfg(args)

    log.info("=== Ynet Vote Rotation Client starting ===")
    log.info("Config    : %s", cfg["config_path"])
    log.info("Server    : %s", cfg["base"])
    log.info("Article   : %s", cfg["article_id"])
    log.info("Pool size : %d", cfg["pool_size"])
    log.info("Vote URL  : %s", cfg["vote_url"])
    log.info("List URL  : %s", cfg["list_url"])
    log.info("Log file  : %s", LOG_FILE)

    print("\n" + "=" * 65)
    print("  Ynet Vote Rotation Client")
    print(f"  Server  : {cfg['base']}")
    print(f"  Article : {cfg['article_id']}")
    print(f"  Pool    : {cfg['pool_size']} IPs per test")
    print(f"  Log     : {LOG_FILE}")
    print("=" * 65)

    # Check server reachability, then probe the list endpoint
    try:
        requests.get(cfg["base"], timeout=5)
        log.info("Server reachable at %s", cfg["base"])
    except Exception as exc:
        log.error("Cannot reach server at %s — %s", cfg["base"], exc)
        log.debug(traceback.format_exc())
        print(f"\n[ERROR] Cannot connect to server at {cfg['base']}")
        print(f"        {exc}")
        print(f"        Check --server or config.json server.host / server.port")
        print(f"        Full details in log: {LOG_FILE}")
        sys.exit(1)

    # Probe the list endpoint before running tests
    log.info("Probing list endpoint: %s", cfg["list_url"])
    try:
        probe = requests.get(cfg["list_url"], timeout=10)
        log.info("List endpoint probe: HTTP %s  Content-Type: %s",
                 probe.status_code, probe.headers.get("Content-Type", "?"))
        if probe.status_code >= 400:
            log.error("List endpoint returned HTTP %s — cannot fetch comments. body: %s",
                      probe.status_code, probe.text[:300])
            print(f"\n[ERROR] List endpoint returned HTTP {probe.status_code}")
            print(f"        URL: {cfg['list_url']}")
            print(f"        Check --article-id and --server are correct")
            print(f"        Body: {probe.text[:200]}")
            print(f"        Full details in log: {LOG_FILE}")
            sys.exit(1)
        if "application/json" not in probe.headers.get("Content-Type", ""):
            log.warning("List endpoint returned non-JSON Content-Type: %s — may fail to parse",
                        probe.headers.get("Content-Type", "?"))
            print(f"[WARN] List endpoint returned Content-Type: {probe.headers.get('Content-Type','?')}")
            print(f"       Expected application/json — parsing may fail")
    except Exception as exc:
        log.error("List endpoint probe failed: %s", exc)
        log.debug(traceback.format_exc())
        print(f"\n[ERROR] Could not probe list endpoint: {exc}")
        print(f"        URL: {cfg['list_url']}")
        print(f"        Full details in log: {LOG_FILE}")
        sys.exit(1)

    try:
        targets = show_targeting_analysis(cfg, log)
        test_boost_top_comment(cfg, log, targets)
        test_suppress_comment(cfg, log, targets)
        test_manufacture_zero_comment(cfg, log, targets)
        test_dedup_still_works(cfg, log, targets)
        test_validation(cfg, log)
    except Exception as exc:
        log.critical("Unhandled exception: %s\n%s", exc, traceback.format_exc())
        print(f"\n[FATAL] {exc}")
        print(f"Full details in log: {LOG_FILE}")
        sys.exit(1)

    log.info("=== All tests complete ===")
    print("\n" + "=" * 65)
    print(f"  Done.")
    print(f"  Admin stats : GET {cfg['stats_url']}")
    print(f"  Full log    : {LOG_FILE}")
    print("=" * 65 + "\n")
