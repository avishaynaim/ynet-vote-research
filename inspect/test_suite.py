#!/usr/bin/env python3
"""
test_suite.py — Comprehensive Ynet vote behavior test suite.

Runs all scenarios sequentially, verifying the ACTUAL counter change
after each one (not just HTTP status). Waits real time between votes.

Scenarios tested:
  S01  Single like — baseline verification
  S02  Double like immediately (0s gap) — same IP, no delay
  S03  Double like 6s gap — short delay between votes
  S04  Double like 60s gap — after counter refresh window
  S05  Double like 10min gap — longer cooldown
  S06  Triple like immediately — 3x same IP
  S07  Like then dislike (0s gap) — does it cancel, or add 2?
  S08  Dislike then like (0s gap) — reverse order
  S09  Like on two DIFFERENT talkbacks (same IP) — cross-talkback dedup
  S10  Two DIFFERENT proxies, same talkback — IP uniqueness value
  S11  Direct vote (no proxy) — does our server IP work?
  S12  Double like via SESSION (cookies persist) — real browser simulation
  S13  10 votes same proxy rapid-fire — flood detection

Usage:
  python3 test_suite.py                    # full suite on default article
  python3 test_suite.py --article yokra14737379
  python3 test_suite.py --scenarios S01,S02,S03
  python3 test_suite.py --from-scenario S05
  python3 test_suite.py --proxy-idx 1     # which proxy to use as "same IP"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
YNET       = "https://www.ynet.co.il"
VOTE_URL   = f"{YNET}/iphone/json/api/talkbacks/vote"
LIST_URL   = f"{YNET}/iphone/json/api/talkbacks/list/v2"
REPO_ROOT  = Path(__file__).resolve().parent.parent
OUT_DIR    = Path(__file__).resolve().parent / "results"

BASE_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept":          "application/json, */*",
    "Accept-Language": "he-IL,he;q=0.9",
    "Origin":          YNET,
    "Content-Type":    "application/json",
}

COUNTER_WAIT_S  = 90     # seconds to wait after voting before reading counter
COUNTER_POLL_S  = 10     # poll interval when watching counter
COUNTER_TIMEOUT = 180    # give up after this many seconds
# ---------------------------------------------------------------------------


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg, tag="INFO"):
    print(f"[{ts()}] [{tag}] {msg}", flush=True)


def section(title):
    w = 65
    print(f"\n{'='*w}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'='*w}", flush=True)


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

def load_proxies(path=None):
    p = path or (REPO_ROOT / "proxies" / "alive.json")
    with open(p) as f:
        data = json.load(f)
    result = []
    for item in data:
        scheme = item.get("scheme", "http")
        addr   = item.get("addr")
        if not addr:
            continue
        url = f"{scheme}://{addr}"
        result.append({"label": item.get("exit_ip") or addr,
                        "proxies": {"http": url, "https": url}})
    return result


def proxy_dict(entry):
    return entry["proxies"] if entry else None


# ---------------------------------------------------------------------------
# Counter reading — searches ALL pages for the target talkback
# ---------------------------------------------------------------------------

def read_counter(article_id, talkback_id, timeout=15):
    """Fetch all comment pages and return (likes, unlikes) for talkback_id."""
    for page in range(1, 51):
        try:
            r = requests.get(
                f"{LIST_URL}/{article_id}/0/{page}",
                headers={"User-Agent": BASE_HDRS["User-Agent"],
                         "Cache-Control": "no-cache", "Pragma": "no-cache"},
                timeout=timeout,
            )
            ch    = r.json().get("rss", {}).get("channel", {}) or {}
            items = ch.get("item", []) or []
            for c in items:
                if c.get("id") == talkback_id:
                    return int(c.get("likes", 0) or 0), int(c.get("unlikes", 0) or 0)
            if not ch.get("hasMore"):
                break
        except Exception as exc:
            log(f"read_counter page {page} error: {exc}", "ERR")
            break
    return None, None


def wait_for_counter_change(article_id, talkback_id, baseline_l, baseline_u,
                             wait_before=COUNTER_WAIT_S, poll=COUNTER_POLL_S,
                             max_wait=COUNTER_TIMEOUT, log_file=None):
    """
    Wait `wait_before` seconds, then poll until counter changes or max_wait exceeded.
    Returns dict with all observations.
    """
    log(f"Waiting {wait_before}s before first counter read...")
    time.sleep(wait_before)

    start = time.time()
    poll_num = 0
    while time.time() - start < (max_wait - wait_before):
        poll_num += 1
        l, u = read_counter(article_id, talkback_id)
        elapsed = round(time.time() - start + wait_before, 1)
        if l is None:
            log(f"  poll {poll_num}: read_counter returned None", "WARN")
        else:
            dl = l - baseline_l
            du = u - baseline_u
            changed = dl != 0 or du != 0
            sym = "✓ CHANGED" if changed else "  no change"
            log(f"  poll {poll_num} (+{elapsed:.0f}s): likes={l}({dl:+d}) "
                f"unlikes={u}({du:+d})  {sym}")
            if log_file:
                with open(log_file, "a") as f:
                    f.write(json.dumps({"poll": poll_num, "elapsed_s": elapsed,
                                        "likes": l, "unlikes": u,
                                        "delta_likes": dl, "delta_unlikes": du,
                                        "changed": changed}) + "\n")
            if changed:
                return {"changed": True, "likes": l, "unlikes": u,
                        "delta_likes": dl, "delta_unlikes": du,
                        "elapsed_s": elapsed}
        time.sleep(poll)

    l, u = read_counter(article_id, talkback_id)
    return {"changed": False, "likes": l, "unlikes": u,
            "delta_likes": (l - baseline_l) if l is not None else None,
            "delta_unlikes": (u - baseline_u) if u is not None else None,
            "elapsed_s": round(time.time() - start + wait_before, 1)}


# ---------------------------------------------------------------------------
# Single vote
# ---------------------------------------------------------------------------

def cast_vote(article_id, talkback_id, like=True, proxy_entry=None,
              timeout=25, extra_hdrs=None, session=None):
    payload = {
        "article_id":      article_id,
        "talkback_id":     talkback_id,
        "talkback_like":   like,
        "talkback_unlike": not like,
        "vote_type":       "2state",
    }
    hdrs = {**BASE_HDRS, "Referer": f"{YNET}/news/article/{article_id}",
            **(extra_hdrs or {})}
    px = proxy_dict(proxy_entry)
    label = proxy_entry["label"] if proxy_entry else "DIRECT"
    t0 = time.time()
    caller = session or requests
    try:
        r = caller.post(VOTE_URL, json=payload, headers=hdrs,
                        proxies=px, timeout=timeout, allow_redirects=False)
        elapsed = round(time.time() - t0, 3)
        try:
            body = r.json()
        except Exception:
            body = r.text[:200]
        resp_hdrs = {k: v for k, v in r.headers.items()
                     if k.lower() in ("set-cookie", "cache-control", "content-type", "via")}
        ok = r.status_code == 200
        outcome = ("VOTED" if ok and isinstance(body, dict) and body.get("success")
                   else f"HTTP_{r.status_code}")
        return {"ok": ok, "outcome": outcome, "status": r.status_code,
                "body": body, "resp_hdrs": resp_hdrs,
                "elapsed_s": elapsed, "proxy": label}
    except Exception as exc:
        return {"ok": False, "outcome": f"ERR_{type(exc).__name__}",
                "status": None, "body": str(exc)[:200],
                "elapsed_s": round(time.time() - t0, 3), "proxy": label}


def fmt_vote(res):
    sym = "✓" if res["ok"] else "✗"
    return (f"{sym} {res['outcome']:25s} proxy={res['proxy'][:35]}  "
            f"status={res['status']}  {res['elapsed_s']}s  "
            f"cookie={'set-cookie' in {k.lower(): v for k,v in res.get('resp_hdrs',{}).items()}}")


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------

class Results:
    def __init__(self, log_file):
        self.log_file = log_file
        self.rows = []

    def record(self, scenario_id, name, votes_sent, votes_ok, gap_s,
               baseline_l, baseline_u, counter, notes=""):
        dl = (counter["delta_likes"]  if counter else None)
        du = (counter["delta_unlikes"] if counter else None)
        changed = counter.get("changed", False) if counter else False
        row = {
            "scenario":     scenario_id,
            "name":         name,
            "votes_sent":   votes_sent,
            "votes_ok":     votes_ok,
            "gap_s":        gap_s,
            "baseline_l":   baseline_l,
            "baseline_u":   baseline_u,
            "delta_likes":  dl,
            "delta_unlikes": du,
            "changed":      changed,
            "notes":        notes,
            "ts":           datetime.now().isoformat(timespec="seconds"),
        }
        self.rows.append(row)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        verdict = "OK" if changed else "FAIL"
        log(f"  → delta_likes={dl}  delta_unlikes={du}  "
            f"votes_ok={votes_ok}  counter_changed={changed}  [{verdict}]",
            "RESULT")

    def print_summary(self):
        section("SUMMARY")
        print(f"{'ID':>4}  {'Name':<35}  {'sent':>4}  {'ok':>3}  "
              f"{'gap':>6}  {'Δlikes':>7}  {'Δunlike':>7}  {'changed':>7}  notes")
        print("-" * 110)
        for r in self.rows:
            print(f"{r['scenario']:>4}  {r['name']:<35}  {str(r['votes_sent']):>4}  "
                  f"{str(r['votes_ok']):>3}  {str(r['gap_s']):>6}s  "
                  f"{str(r['delta_likes']):>7}  {str(r['delta_unlikes']):>7}  "
                  f"{str(r['changed']):>7}  {r['notes']}")


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------

def pick_talkback(article_id, prefer_low=True):
    """Pick a talkback from the article. Returns (talkback_id, likes, unlikes)."""
    all_items = []
    for page in range(1, 51):
        try:
            r = requests.get(f"{LIST_URL}/{article_id}/0/{page}",
                             headers={"User-Agent": BASE_HDRS["User-Agent"]}, timeout=10)
            ch    = r.json().get("rss", {}).get("channel", {}) or {}
            items = ch.get("item", []) or []
            for c in items:
                all_items.append((c["id"],
                                  int(c.get("likes",0) or 0),
                                  int(c.get("unlikes",0) or 0)))
            if not ch.get("hasMore"):
                break
        except Exception:
            break
    if not all_items:
        return None, 0, 0
    if prefer_low:
        # Lowest total activity — easiest to detect our change
        return min(all_items, key=lambda x: x[1] + x[2])
    # Highest activity — most visible
    return max(all_items, key=lambda x: x[1] + x[2])


def run_scenario(sid, name, article_id, talkback_id, px_main, px_alt,
                 results, log_file, votes_fn, gap_desc=""):
    """
    Generic scenario runner.
    votes_fn(article_id, talkback_id, px_main, px_alt) → list of vote results
    """
    section(f"[{sid}] {name}")
    bl, bu = read_counter(article_id, talkback_id)
    log(f"Baseline: likes={bl}  unlikes={bu}")
    if bl is None:
        log("Cannot read baseline — skipping", "WARN")
        return

    vote_results = votes_fn(article_id, talkback_id, px_main, px_alt)
    votes_sent = len(vote_results)
    votes_ok   = sum(1 for v in vote_results if v["ok"])
    for v in vote_results:
        log(fmt_vote(v))

    counter = wait_for_counter_change(article_id, talkback_id, bl, bu,
                                      log_file=log_file)
    results.record(sid, name, votes_sent, votes_ok, gap_desc,
                   bl, bu, counter)


def fetch_all_talkbacks(article_id):
    """Return all talkbacks sorted by total activity ascending (lowest first)."""
    all_items = []
    for page in range(1, 51):
        try:
            r = requests.get(f"{LIST_URL}/{article_id}/0/{page}",
                             headers={"User-Agent": BASE_HDRS["User-Agent"]}, timeout=10)
            ch    = r.json().get("rss", {}).get("channel", {}) or {}
            items = ch.get("item", []) or []
            for c in items:
                all_items.append({
                    "id":      c["id"],
                    "likes":   int(c.get("likes",   0) or 0),
                    "unlikes": int(c.get("unlikes", 0) or 0),
                })
            if not ch.get("hasMore"):
                break
        except Exception:
            break
    return sorted(all_items, key=lambda x: x["likes"] + x["unlikes"])


def run_suite(article_id, px_main, px_alt, results, log_file,
              run_only=None, from_sid=None):

    all_scenarios = [
        "S01","S02","S03","S04","S05","S06",
        "S07","S08","S09","S10","S11","S12","S13",
    ]
    if run_only:
        todo = [s for s in all_scenarios if s in run_only]
    elif from_sid:
        idx = all_scenarios.index(from_sid) if from_sid in all_scenarios else 0
        todo = all_scenarios[idx:]
    else:
        todo = all_scenarios

    # Pre-load ALL talkbacks sorted by activity so each scenario gets a FRESH one.
    # Using the same talkback across scenarios contaminates results because the
    # server tracks votes per (IP, talkback) — a proxy that already voted in S01
    # would appear to be "deduped" in S02 even if it's the first vote for that scenario.
    log(f"Loading all talkbacks from {article_id}...")
    talkbacks = fetch_all_talkbacks(article_id)
    log(f"  {len(talkbacks)} talkbacks loaded, lowest activity: "
        f"id={talkbacks[0]['id']} likes={talkbacks[0]['likes']} unlikes={talkbacks[0]['unlikes']}")

    tb_pool = iter(talkbacks)  # pop from front (lowest activity first)

    def next_tb():
        try:
            t = next(tb_pool)
            log(f"  Using talkback {t['id']} (likes={t['likes']} unlikes={t['unlikes']})")
            return t["id"]
        except StopIteration:
            log("Ran out of fresh talkbacks!", "WARN")
            return talkbacks[-1]["id"]

    def V(article_id, talkback_id, px, px_alt, like=True, n=1, gap=0):
        """Cast n votes, optional gap between them. Returns list of results."""
        res = []
        for i in range(n):
            if i > 0 and gap > 0:
                log(f"  waiting {gap}s between vote {i} and {i+1}...")
                time.sleep(gap)
            res.append(cast_vote(article_id, talkback_id, like=like,
                                 proxy_entry=px, timeout=25))
        return res

    # S01 — single like
    if "S01" in todo:
        run_scenario("S01", "Single like (baseline verify)", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=1),
                     gap_desc="0")

    # S02 — double like, 0s gap, FRESH talkback so the 2nd vote is the proxy's
    #        true 2nd attempt on that specific talkback
    if "S02" in todo:
        run_scenario("S02", "Double like, 0s gap, same proxy", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=2, gap=0),
                     gap_desc="0")

    # S03 — double like, 6s gap
    if "S03" in todo:
        run_scenario("S03", "Double like, 6s gap, same proxy", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=2, gap=6),
                     gap_desc="6")

    # S04 — double like, 60s gap (after counter refresh)
    if "S04" in todo:
        log("S04 has a 60s gap between votes — this scenario takes ~4min total")
        run_scenario("S04", "Double like, 60s gap, same proxy", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=2, gap=60),
                     gap_desc="60")

    # S05 — double like, 10min gap
    if "S05" in todo:
        log("S05 has a 10min gap between votes — this scenario takes ~13min total")
        run_scenario("S05", "Double like, 10min gap, same proxy", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=2, gap=600),
                     gap_desc="600")

    # S06 — triple like, 0s gap
    if "S06" in todo:
        run_scenario("S06", "Triple like, 0s gap, same proxy", article_id, next_tb(),
                     px_main, px_alt, results, log_file,
                     lambda a, t, p, pa: V(a, t, p, pa, like=True, n=3, gap=0),
                     gap_desc="0")

    # S07 — like then dislike, 0s gap
    if "S07" in todo:
        tb = next_tb()
        def s07(a, t, p, pa):
            r = [cast_vote(a, t, like=True, proxy_entry=p, timeout=25)]
            r.append(cast_vote(a, t, like=False, proxy_entry=p, timeout=25))
            return r
        run_scenario("S07", "Like then dislike, 0s gap", article_id, tb,
                     px_main, px_alt, results, log_file, s07, gap_desc="0")

    # S08 — dislike then like, 0s gap
    if "S08" in todo:
        tb = next_tb()
        def s08(a, t, p, pa):
            r = [cast_vote(a, t, like=False, proxy_entry=p, timeout=25)]
            r.append(cast_vote(a, t, like=True, proxy_entry=p, timeout=25))
            return r
        run_scenario("S08", "Dislike then like, 0s gap", article_id, tb,
                     px_main, px_alt, results, log_file, s08, gap_desc="0")

    # S09 — like on two DIFFERENT talkbacks, same proxy (fresh talkbacks for both)
    if "S09" in todo:
        tb1 = next_tb()
        tb2 = next_tb()
        section("[S09] Like on 2 different talkbacks, same proxy")
        bl1, bu1 = read_counter(article_id, tb1)
        bl2, bu2 = read_counter(article_id, tb2)
        log(f"Talkback 1 ({tb1}) baseline: likes={bl1} unlikes={bu1}")
        log(f"Talkback 2 ({tb2}) baseline: likes={bl2} unlikes={bu2}")
        v1 = cast_vote(article_id, tb1, like=True, proxy_entry=px_main)
        v2 = cast_vote(article_id, tb2, like=True, proxy_entry=px_main)
        log(fmt_vote(v1))
        log(fmt_vote(v2))
        c1 = wait_for_counter_change(article_id, tb1, bl1, bu1)
        c2 = wait_for_counter_change(article_id, tb2, bl2, bu2, wait_before=0)
        notes = (f"T1_delta={c1.get('delta_likes')},{c1.get('delta_unlikes')} "
                 f"T2_delta={c2.get('delta_likes')},{c2.get('delta_unlikes')}")
        results.record("S09", "2 different talkbacks same proxy", 2,
                       int(v1["ok"]) + int(v2["ok"]), "0", bl1, bu1, c1, notes=notes)

    # S10 — two different proxies, same talkback
    if "S10" in todo:
        tb = next_tb()
        section("[S10] Two DIFFERENT proxies, same talkback")
        bl, bu = read_counter(article_id, tb)
        log(f"Baseline: likes={bl} unlikes={bu}")
        log(f"Vote 1 via {px_main['label'] if px_main else 'DIRECT'}...")
        v1 = cast_vote(article_id, tb, like=True, proxy_entry=px_main)
        log(fmt_vote(v1))
        log(f"Vote 2 via {px_alt['label'] if px_alt else 'DIRECT'}...")
        v2 = cast_vote(article_id, tb, like=True, proxy_entry=px_alt)
        log(fmt_vote(v2))
        counter = wait_for_counter_change(article_id, tb, bl, bu)
        notes = f"px1={px_main['label'][:20] if px_main else 'direct'} px2={px_alt['label'][:20] if px_alt else 'direct'}"
        results.record("S10", "2 different proxies same talkback", 2,
                       int(v1["ok"]) + int(v2["ok"]), "0", bl, bu, counter, notes=notes)

    # S11 — direct (no proxy)
    if "S11" in todo:
        run_scenario("S11", "Direct (no proxy, our server IP)", article_id, next_tb(),
                     None, px_alt, results, log_file,
                     lambda a, t, p, pa: [cast_vote(a, t, like=True, proxy_entry=None)],
                     gap_desc="0")

    # S12 — double like via Session (cookies persist between calls)
    if "S12" in todo:
        tb = next_tb()
        section("[S12] Double like via Session (cookies persist)")
        bl, bu = read_counter(article_id, tb)
        log(f"Baseline: likes={bl} unlikes={bu}")
        s = requests.Session()
        log("Vote 1 (Session, no prior cookie)...")
        v1 = cast_vote(article_id, tb, like=True, proxy_entry=px_main,
                       timeout=25, session=s)
        log(fmt_vote(v1))
        log(f"  Cookie jar after vote 1: {dict(s.cookies)}")
        log("Vote 2 (Session, cookie from vote 1 is sent automatically)...")
        v2 = cast_vote(article_id, tb, like=True, proxy_entry=px_main,
                       timeout=25, session=s)
        log(fmt_vote(v2))
        log(f"  Cookie jar after vote 2: {dict(s.cookies)}")
        counter = wait_for_counter_change(article_id, tb, bl, bu)
        notes = f"cookie_after_v1={dict(s.cookies)}"
        results.record("S12", "Double like w/ session cookies", 2,
                       int(v1["ok"]) + int(v2["ok"]), "0", bl, bu, counter, notes=notes)

    # S13 — 10-vote rapid flood, same proxy
    if "S13" in todo:
        tb = next_tb()
        section("[S13] 10-vote rapid flood, same proxy")
        bl, bu = read_counter(article_id, tb)
        log(f"Baseline: likes={bl} unlikes={bu}")
        vote_results = []
        for i in range(1, 11):
            v = cast_vote(article_id, tb, like=True, proxy_entry=px_main)
            vote_results.append(v)
            log(f"  [{i:2d}/10] {fmt_vote(v)}")
        votes_ok = sum(1 for v in vote_results if v["ok"])
        counter = wait_for_counter_change(article_id, tb, bl, bu)
        notes = f"votes_ok={votes_ok}/10"
        results.record("S13", "10-vote flood, same proxy", 10, votes_ok, "0",
                       bl, bu, counter, notes=notes)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ynet vote test suite")
    parser.add_argument("--article",    default="yokra14737379")
    parser.add_argument("--proxy-idx",  type=int, default=1, dest="proxy_idx")
    parser.add_argument("--alt-idx",    type=int, default=2, dest="alt_idx")
    parser.add_argument("--proxies",    default=None)
    parser.add_argument("--scenarios",  default=None,
                        help="Comma-separated list e.g. S01,S02,S03")
    parser.add_argument("--from-scenario", default=None, dest="from_scenario",
                        help="Run from this scenario onwards e.g. S05")
    parser.add_argument("--skip-slow",  action="store_true", dest="skip_slow",
                        help="Skip S04 (60s gap) and S05 (10min gap)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = str(OUT_DIR / f"suite_{run_ts}.jsonl")
    log(f"Article   : {args.article}")
    log(f"Log file  : {log_file}")

    proxies = load_proxies(args.proxies)
    log(f"Proxies loaded: {len(proxies)}")

    # Find working proxies in parallel — fast scan, 4s timeout per proxy
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _probe(i):
        px = proxies[i]
        try:
            r = requests.get(
                f"{YNET}/iphone/json/api/talkbacks/list/v2/{args.article}/0/1",
                headers={"User-Agent": BASE_HDRS["User-Agent"]},
                proxies=proxy_dict(px), timeout=4,
            )
            if r.status_code == 200:
                return i, px
        except Exception:
            pass
        return i, None

    def find_working_fast(start_idx, skip_label=None, n_try=60):
        candidates = list(range(start_idx, min(start_idx + n_try, len(proxies))))
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(_probe, i): i for i in candidates}
            found = []
            for fut in as_completed(futs):
                idx, px = fut.result()
                if px and (skip_label is None or px["label"] != skip_label):
                    found.append((idx, px))
            found.sort(key=lambda x: x[0])
            if found:
                idx, px = found[0]
                log(f"Working proxy [{idx}]: {px['label']}")
                return px
        return None

    log("Finding working proxies (parallel scan)...")
    px_main = find_working_fast(args.proxy_idx)
    px_alt  = find_working_fast(
        args.alt_idx,
        skip_label=px_main["label"] if px_main else None,
    )
    if px_main is None:
        log("Could not find working proxy for px_main", "ERR")
        sys.exit(1)
    if px_alt is None:
        log("Could not find px_alt — using None (direct)", "WARN")

    run_only = [s.strip().upper() for s in args.scenarios.split(",")] if args.scenarios else None
    if args.skip_slow and run_only is None:
        run_only = [s for s in ["S01","S02","S03","S06","S07","S08",
                                "S09","S10","S11","S12","S13"] if True]

    res = Results(log_file)
    run_suite(args.article, px_main, px_alt, res, log_file,
              run_only=run_only, from_sid=args.from_scenario)
    res.print_summary()
    log(f"Full results in {log_file}")


if __name__ == "__main__":
    main()
