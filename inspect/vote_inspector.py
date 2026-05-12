#!/usr/bin/env python3
"""
vote_inspector.py — Deep Ynet comment-voting investigator.

Investigates: does a vote actually move the counter? What does blocking look like?
How long is the counter latency? Does Ynet dedup by IP?

Modes:
  baseline     Fetch + print all comment counts for an article, then exit.
  probe        Vote once through one proxy, then poll counter until it moves or times out.
  dedup        Vote twice with the same proxy on the same talkback — detect dedup.
  multi        Vote N times via different proxies, compare success rates.
  headers      Try different header combinations and log which get through vs blocked.
  full         Run baseline → probe → dedup in order.

Usage:
  python3 vote_inspector.py --article skcbqht011g --mode baseline
  python3 vote_inspector.py --article skcbqht011g --talkback 99347590 --mode probe
  python3 vote_inspector.py --article skcbqht011g --talkback 99347590 --mode dedup
  python3 vote_inspector.py --article skcbqht011g --talkback 99347590 --mode multi --count 20
  python3 vote_inspector.py --article skcbqht011g --talkback 99347590 --mode full
  python3 vote_inspector.py --find-old         # scan known articles for stable counters
"""

import argparse
import json
import os
import sys
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from requests.exceptions import RequestException

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YNET_BASE   = "https://www.ynet.co.il"
VOTE_URL    = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
LIST_URL    = f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2"

REPO_ROOT   = Path(__file__).resolve().parent.parent
PROXIES_FILE = REPO_ROOT / "proxies" / "alive.json"
OUT_DIR     = Path(__file__).resolve().parent / "results"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg, prefix=""):
    print(f"[{ts()}] {prefix}{msg}", flush=True)

def section(title):
    print(f"\n{'='*60}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'='*60}", flush=True)

def fmt_resp(r):
    """Summarize a requests.Response for printing."""
    try:
        body = r.json()
    except Exception:
        body = r.text[:300]
    hdrs = dict(r.headers)
    # Only the interesting response headers
    interesting = {k: v for k, v in hdrs.items() if k.lower() in (
        "content-type", "server", "x-cache", "x-request-id",
        "set-cookie", "cf-ray", "x-akamai-transformed",
        "x-check-cacheable", "via", "age", "cache-control",
    )}
    return {"status": r.status_code, "headers": interesting, "body": body}

# ---------------------------------------------------------------------------
# Proxy loading
# ---------------------------------------------------------------------------

def load_proxies(path=None):
    p = path or PROXIES_FILE
    if not os.path.exists(p):
        log(f"No proxies file at {p}. Will run direct (no proxy).", prefix="WARN ")
        return []
    with open(p) as f:
        data = json.load(f)
    result = []
    for item in data:
        scheme = item.get("scheme", "http")
        addr   = item.get("addr")
        if not addr:
            continue
        url = f"{scheme}://{addr}"
        result.append({
            "label":   item.get("exit_ip") or addr,
            "proxies": {"http": url, "https": url},
        })
    log(f"Loaded {len(result)} proxies from {p}")
    return result

# ---------------------------------------------------------------------------
# Ynet API calls
# ---------------------------------------------------------------------------

def fetch_comments(article_id, timeout=15):
    """Fetch all comment pages for article_id. Returns list of comment dicts."""
    all_items = []
    seen = set()
    for page in range(1, 51):
        url = f"{LIST_URL}/{article_id}/0/{page}"
        try:
            r = requests.get(url, headers=BASE_HEADERS, timeout=timeout)
            raw = r.json()
        except Exception as exc:
            log(f"fetch_comments page {page} error: {exc}", prefix="ERR  ")
            break
        ch       = raw.get("rss", {}).get("channel", {}) or {}
        items    = ch.get("item", []) or []
        has_more = ch.get("hasMore")
        if not items:
            break
        for c in items:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            all_items.append({
                "id":      c["id"],
                "author":  c.get("author", ""),
                "likes":   int(c.get("likes",   0) or 0),
                "unlikes": int(c.get("unlikes", 0) or 0),
                "text":    (c.get("text") or "")[:80],
                "pubDate": c.get("pubDate", ""),
            })
        if not has_more:
            break
    return all_items


def cast_vote(article_id, talkback_id, like=True, proxy_entry=None, timeout=20,
              extra_headers=None, log_file=None):
    """
    Cast one vote. Returns a full audit dict.
    Logs request+response to log_file (JSONL) if provided.
    """
    payload = {
        "article_id":      article_id,
        "talkback_id":     talkback_id,
        "talkback_like":   like,
        "talkback_unlike": not like,
        "vote_type":       "2state",
    }
    hdrs = {
        **BASE_HEADERS,
        "Content-Type": "application/json",
        "Origin":        YNET_BASE,
        "Referer":       f"{YNET_BASE}/news/article/{article_id}",
        **(extra_headers or {}),
    }
    proxies = proxy_entry["proxies"] if proxy_entry else None
    label   = proxy_entry["label"]   if proxy_entry else "DIRECT"

    t0 = time.time()
    audit = {
        "ts":           datetime.now().isoformat(timespec="milliseconds"),
        "proxy":        label,
        "article_id":   article_id,
        "talkback_id":  talkback_id,
        "like":         like,
        "req_headers":  hdrs,
        "req_payload":  payload,
    }
    try:
        r = requests.post(VOTE_URL, json=payload, headers=hdrs,
                          proxies=proxies, timeout=timeout,
                          allow_redirects=False)
        elapsed = round(time.time() - t0, 3)
        resp    = fmt_resp(r)
        audit.update({
            "elapsed_s":   elapsed,
            "status":      r.status_code,
            "ok":          r.status_code == 200,
            "resp_headers": resp["headers"],
            "resp_body":   resp["body"],
        })
        # Classify the outcome
        if r.status_code == 200:
            if isinstance(resp["body"], dict) and resp["body"].get("success"):
                audit["outcome"] = "VOTED"
            else:
                audit["outcome"] = "200_NOT_SUCCESS"
        elif r.status_code == 403:
            audit["outcome"] = "BLOCKED_403"
        elif r.status_code == 429:
            audit["outcome"] = "RATE_LIMITED"
        elif r.status_code >= 500:
            audit["outcome"] = f"SERVER_ERR_{r.status_code}"
        else:
            audit["outcome"] = f"HTTP_{r.status_code}"
    except Exception as exc:
        elapsed = round(time.time() - t0, 3)
        audit.update({
            "elapsed_s": elapsed,
            "status":    f"ERR:{type(exc).__name__}",
            "ok":        False,
            "error":     str(exc)[:300],
            "outcome":   f"ERR_{type(exc).__name__}",
        })

    if log_file:
        with open(log_file, "a") as f:
            f.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return audit


def poll_counter(article_id, talkback_id, baseline_likes, baseline_unlikes,
                 interval_s=5, max_polls=24, log_file=None):
    """
    Poll comment list until counter changes or max_polls reached.
    Returns (changed: bool, final_likes, final_unlikes, polls_taken, first_change_at_poll)
    """
    log(f"Polling /{article_id} talkback {talkback_id} every {interval_s}s "
        f"(baseline likes={baseline_likes} unlikes={baseline_unlikes})")
    for i in range(1, max_polls + 1):
        time.sleep(interval_s)
        try:
            comments = fetch_comments(article_id, timeout=15)
        except Exception as exc:
            log(f"  poll {i}: fetch error {exc}", prefix="ERR  ")
            continue
        hit = next((c for c in comments if c["id"] == talkback_id), None)
        if not hit:
            log(f"  poll {i}: talkback {talkback_id} not found in article")
            continue
        likes   = hit["likes"]
        unlikes = hit["unlikes"]
        delta_l = likes   - baseline_likes
        delta_u = unlikes - baseline_unlikes
        changed = (delta_l != 0 or delta_u != 0)
        symbol  = "✓ CHANGED" if changed else "  no change"
        log(f"  poll {i:2d}/{max_polls}: likes={likes:+d}({delta_l:+d}) "
            f"unlikes={unlikes:+d}({delta_u:+d})  {symbol}")
        if log_file:
            rec = {"poll": i, "ts": datetime.now().isoformat(timespec="milliseconds"),
                   "likes": likes, "unlikes": unlikes,
                   "delta_likes": delta_l, "delta_unlikes": delta_u, "changed": changed}
            with open(log_file, "a") as f:
                f.write(json.dumps(rec) + "\n")
        if changed:
            return True, likes, unlikes, i, i
    return False, likes, unlikes, max_polls, None


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_baseline(article_id, args):
    section(f"BASELINE: {article_id}")
    comments = fetch_comments(article_id)
    if not comments:
        log("No comments found.", prefix="WARN ")
        return []
    log(f"Found {len(comments)} comments")
    print(f"\n{'ID':>12}  {'likes':>6}  {'unlikes':>7}  {'date':>12}  author / text")
    print("-" * 90)
    for c in sorted(comments, key=lambda x: -(x["likes"] + x["unlikes"]))[:50]:
        text_preview = c["text"][:50].replace("\n", " ")
        date = c["pubDate"][:10] if c["pubDate"] else "?"
        print(f"{c['id']:>12}  {c['likes']:>6}  {c['unlikes']:>7}  {date:>12}  "
              f"{c['author'][:15]}: {text_preview}")
    return comments


def mode_probe(article_id, talkback_id, like, proxy_entry, args, log_file):
    section(f"PROBE: article={article_id} talkback={talkback_id} "
            f"like={like} proxy={proxy_entry['label'] if proxy_entry else 'DIRECT'}")

    # Baseline
    log("Fetching baseline counts...")
    comments = fetch_comments(article_id)
    baseline = next((c for c in comments if c["id"] == talkback_id), None)
    if not baseline:
        log(f"Talkback {talkback_id} not found in article {article_id}", prefix="ERR  ")
        return
    log(f"Baseline → likes={baseline['likes']}  unlikes={baseline['unlikes']}")

    # Cast vote
    log("Casting vote...")
    audit = cast_vote(article_id, talkback_id, like=like,
                      proxy_entry=proxy_entry, timeout=args.timeout,
                      log_file=log_file)
    _print_audit(audit)

    if not audit["ok"] and audit.get("outcome", "").startswith("ERR"):
        log("Vote failed at network level — skipping counter poll.", prefix="WARN ")
        return

    # Poll counter
    section("COUNTER POLL")
    changed, final_l, final_u, polls, poll_idx = poll_counter(
        article_id, talkback_id,
        baseline["likes"], baseline["unlikes"],
        interval_s=args.poll_interval,
        max_polls=args.max_polls,
        log_file=str(Path(log_file).parent / "polls.jsonl") if log_file else None,
    )
    if changed:
        log(f"Counter moved after poll {poll_idx} "
            f"(~{poll_idx * args.poll_interval}s). "
            f"Final: likes={final_l} unlikes={final_u}", prefix="OK   ")
    else:
        log(f"Counter did NOT change after {polls} polls "
            f"({polls * args.poll_interval}s). "
            f"Vote outcome was: {audit.get('outcome')}", prefix="WARN ")


def mode_dedup(article_id, talkback_id, like, proxy_entry, args, log_file):
    section(f"DEDUP TEST: same proxy twice on talkback {talkback_id}")
    if not proxy_entry:
        log("Need a proxy for dedup test (need stable IP). Picking first available.", prefix="WARN ")

    log("Vote 1 →")
    a1 = cast_vote(article_id, talkback_id, like=like,
                   proxy_entry=proxy_entry, timeout=args.timeout, log_file=log_file)
    _print_audit(a1)

    delay = args.dedup_delay
    log(f"Waiting {delay}s before vote 2...")
    time.sleep(delay)

    log("Vote 2 (same proxy) →")
    a2 = cast_vote(article_id, talkback_id, like=like,
                   proxy_entry=proxy_entry, timeout=args.timeout, log_file=log_file)
    _print_audit(a2)

    print()
    if a1["ok"] and a2["ok"]:
        log("RESULT: Both votes accepted — Ynet may not dedup by IP (or cache TTL not expired)")
    elif a1["ok"] and not a2["ok"]:
        log(f"RESULT: Second vote rejected ({a2.get('outcome')}) — dedup IS enforced")
    elif not a1["ok"]:
        log(f"RESULT: First vote failed ({a1.get('outcome')}) — can't conclude dedup")
    else:
        log("RESULT: Both failed — proxy not working")


def mode_multi(article_id, talkback_id, like, proxies, count, args, log_file):
    section(f"MULTI-PROXY: {count} votes on talkback {talkback_id}")

    # Baseline
    log("Fetching baseline...")
    comments = fetch_comments(article_id)
    baseline = next((c for c in comments if c["id"] == talkback_id), None)
    if baseline:
        log(f"Baseline: likes={baseline['likes']} unlikes={baseline['unlikes']}")
    else:
        log("Could not fetch baseline.", prefix="WARN ")
        baseline = None

    picks = random.sample(proxies, min(count, len(proxies))) if proxies else [None] * count
    results = {"VOTED": 0, "BLOCKED_403": 0, "200_NOT_SUCCESS": 0, "other": 0}
    outcomes = []

    for i, px in enumerate(picks, 1):
        label = px["label"] if px else "DIRECT"
        audit = cast_vote(article_id, talkback_id, like=like,
                          proxy_entry=px, timeout=args.timeout, log_file=log_file)
        outcome = audit.get("outcome", "unknown")
        outcomes.append(outcome)
        bucket = outcome if outcome in results else "other"
        results[bucket] += 1

        sym = {"VOTED": "✓", "BLOCKED_403": "✗", "200_NOT_SUCCESS": "?"}.get(outcome, "!")
        log(f"  [{i:3d}/{len(picks)}] {sym} {outcome:25s} {label[:30]}  "
            f"({audit.get('elapsed_s', '?')}s)")

    section("MULTI SUMMARY")
    for k, v in results.items():
        print(f"  {k:<25}: {v}")
    print(f"  success rate: {results['VOTED']/len(picks)*100:.1f}%")

    if baseline:
        log("Fetching post-vote counts (30s wait)...")
        time.sleep(30)
        comments2 = fetch_comments(article_id)
        post = next((c for c in comments2 if c["id"] == talkback_id), None)
        if post:
            delta_l = post["likes"]   - baseline["likes"]
            delta_u = post["unlikes"] - baseline["unlikes"]
            log(f"Post-vote: likes={post['likes']} ({delta_l:+d})  "
                f"unlikes={post['unlikes']} ({delta_u:+d})")
            if delta_l == 0 and delta_u == 0:
                log("Counter did NOT change despite votes — check dedup/caching.", prefix="WARN ")
            elif delta_l == results["VOTED"] or delta_u == results["VOTED"]:
                log(f"Counter moved by exactly {results['VOTED']} — 1:1 match", prefix="OK   ")
            else:
                log(f"Counter moved by {delta_l}/{delta_u}, but {results['VOTED']} votes OK — "
                    "may be deduped or cached", prefix="INFO ")


def mode_headers(article_id, talkback_id, like, proxy_entry, args, log_file):
    """Try different header combinations to find what triggers blocking."""
    section(f"HEADER ANALYSIS: talkback {talkback_id}")

    variants = [
        ("full_headers",    {}),
        ("no_referer",      {"Referer": ""}),
        ("wrong_origin",    {"Origin": "https://google.com"}),
        ("no_origin",       {"Origin": ""}),
        ("mobile_ua",       {"User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/112.0.0.0 Mobile Safari/537.36"}),
        ("curl_ua",         {"User-Agent": "curl/7.88.1"}),
        ("python_ua",       {"User-Agent": "python-requests/2.31.0"}),
    ]

    for name, extra in variants:
        audit = cast_vote(article_id, talkback_id, like=like,
                          proxy_entry=proxy_entry, timeout=args.timeout,
                          extra_headers=extra, log_file=log_file)
        outcome = audit.get("outcome", "?")
        sym = "✓" if outcome == "VOTED" else "✗"
        log(f"  {sym} {name:<20} → {outcome}  ({audit.get('elapsed_s','?')}s)")
        if args.verbose:
            print(f"      resp: {json.dumps(audit.get('resp_body', ''), ensure_ascii=False)[:150]}")
        time.sleep(2)  # slight gap between header tests


def mode_find_old(proxies, args):
    """Scan known_articles.json + try to find stable (low-activity) articles."""
    section("FIND OLD ARTICLES")
    known_file = REPO_ROOT / "results" / "known_articles.json"
    try:
        known = json.loads(known_file.read_text())
    except Exception:
        known = ["yokra14737379", "yokra14764416"]

    log(f"Checking {len(known)} known articles for stable comment counters...")

    for aid in known:
        try:
            comments = fetch_comments(aid, timeout=10)
        except Exception as exc:
            log(f"  {aid}: error ({exc})", prefix="ERR  ")
            continue
        if not comments:
            log(f"  {aid}: 0 comments")
            continue
        total_likes   = sum(c["likes"]   for c in comments)
        total_unlikes = sum(c["unlikes"] for c in comments)
        # Score: articles with small total interactions are "quieter"
        activity = total_likes + total_unlikes
        oldest_date = min((c["pubDate"] for c in comments if c["pubDate"]), default="?")
        best_comment = max(comments, key=lambda c: c["likes"] + c["unlikes"])
        log(f"  {aid}: {len(comments)} comments, total_likes={total_likes}, "
            f"total_unlikes={total_unlikes}, oldest={oldest_date[:10]}, "
            f"best_id={best_comment['id']} (likes={best_comment['likes']})")

    log("\nTip: pick an article with few total votes so your changes are detectable.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_audit(audit):
    ok     = audit.get("ok", False)
    status = audit.get("status")
    outcome = audit.get("outcome", "?")
    elapsed = audit.get("elapsed_s", "?")
    sym    = "✓" if ok else "✗"
    log(f"  {sym} outcome={outcome}  status={status}  elapsed={elapsed}s  "
        f"proxy={audit.get('proxy', '?')[:40]}")
    log(f"    req_url : {VOTE_URL}")
    log(f"    payload : {json.dumps(audit.get('req_payload', {}), ensure_ascii=False)}")
    log(f"    resp    : {json.dumps(audit.get('resp_body', ''), ensure_ascii=False)[:200]}")
    resp_hdrs = audit.get("resp_headers", {})
    if resp_hdrs:
        log(f"    resp_hdrs: {json.dumps(resp_hdrs, ensure_ascii=False)}")
    if not ok and audit.get("error"):
        log(f"    error   : {audit['error'][:200]}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ynet vote inspector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--article",    default="skcbqht011g",  help="Ynet article ID")
    parser.add_argument("--talkback",   type=int, default=None, help="Talkback (comment) ID")
    parser.add_argument("--like",       action="store_true", default=True, help="Cast a like (default)")
    parser.add_argument("--dislike",    action="store_true", default=False, help="Cast a dislike")
    parser.add_argument("--mode",       default="baseline",
                        choices=["baseline","probe","dedup","multi","headers","full","find-old"])
    parser.add_argument("--count",      type=int, default=20, help="Votes for multi mode")
    parser.add_argument("--proxies",    default=None, help="Path to proxies JSON file")
    parser.add_argument("--no-proxy",   action="store_true", help="Skip proxies, go direct")
    parser.add_argument("--proxy-idx",  type=int, default=0, help="Index of proxy to use (0-based)")
    parser.add_argument("--timeout",    type=int, default=20, help="Request timeout seconds")
    parser.add_argument("--poll-interval", type=int, default=5, dest="poll_interval",
                        help="Seconds between counter polls")
    parser.add_argument("--max-polls",  type=int, default=24, dest="max_polls",
                        help="Max counter polls before giving up (default 24 = 2min at 5s)")
    parser.add_argument("--dedup-delay", type=int, default=5, dest="dedup_delay",
                        help="Seconds between the two votes in dedup mode")
    parser.add_argument("--verbose",    action="store_true", help="Extra output")

    args = parser.parse_args()

    like = not args.dislike  # --dislike overrides --like

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = str(OUT_DIR / f"inspect_{run_ts}.jsonl")
    log(f"Output log: {log_file}")

    # Load proxies
    proxies = [] if args.no_proxy else load_proxies(args.proxies)
    proxy_entry = None
    if proxies and not args.no_proxy:
        idx = min(args.proxy_idx, len(proxies) - 1)
        proxy_entry = proxies[idx]
        log(f"Using proxy [{idx}]: {proxy_entry['label']}")

    article_id  = args.article
    talkback_id = args.talkback

    mode = args.mode

    if mode == "find-old":
        mode_find_old(proxies, args)
        return

    if mode == "baseline":
        mode_baseline(article_id, args)
        return

    # Modes that need a talkback_id
    if not talkback_id:
        log("Auto-selecting talkback with most likes from baseline...")
        comments = fetch_comments(article_id)
        if not comments:
            log("No comments found, cannot proceed.", prefix="ERR  ")
            sys.exit(1)
        best = max(comments, key=lambda c: c["likes"] + c["unlikes"])
        talkback_id = best["id"]
        log(f"Selected talkback {talkback_id} (likes={best['likes']} unlikes={best['unlikes']}): "
            f"{best['text'][:60]}")

    if mode == "probe":
        mode_probe(article_id, talkback_id, like, proxy_entry, args, log_file)
    elif mode == "dedup":
        mode_dedup(article_id, talkback_id, like, proxy_entry, args, log_file)
    elif mode == "multi":
        mode_multi(article_id, talkback_id, like, proxies, args.count, args, log_file)
    elif mode == "headers":
        mode_headers(article_id, talkback_id, like, proxy_entry, args, log_file)
    elif mode == "full":
        mode_baseline(article_id, args)
        mode_probe(article_id, talkback_id, like, proxy_entry, args, log_file)
        mode_dedup(article_id, talkback_id, like, proxy_entry, args, log_file)


if __name__ == "__main__":
    main()
