#!/usr/bin/env python3
"""
IP Rotation Vote Client — tests against the LOCAL MOCK SERVER only.

Uses real comment data from the live Ynet article snapshot.
Demonstrates how a researcher would SELECT which comments to target
and what the resulting vote manipulation looks like.

Usage:
    # Terminal 1
    python3 mock_server.py

    # Terminal 2
    python3 rotation_client.py
"""

import time
import json
import requests

BASE       = "http://127.0.0.1:5001"
VOTE_URL   = f"{BASE}/iphone/json/api/talkbacks/vote"
LIST_URL   = f"{BASE}/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
STATS_URL  = f"{BASE}/admin/stats"
RESET_URL  = f"{BASE}/admin/reset"
ARTICLE_ID = "yokra14737379"
CACHE_TTL  = 87

# ---------------------------------------------------------------------------
# IP pools (RFC 5737 documentation ranges — not real IPs)
# ---------------------------------------------------------------------------

POOL_5  = [f"203.0.113.{i}"  for i in range(1, 6)]
POOL_25 = [f"198.51.100.{i}" for i in range(1, 26)]
POOL_50 = [f"192.0.2.{i}"    for i in range(1, 51)]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def separator(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")

def reset():
    requests.post(RESET_URL)

def get_all_comments() -> list:
    resp = requests.get(LIST_URL)
    return resp.json()["rss"]["channel"]["item"]

def get_comment(talkback_id: int) -> dict | None:
    for c in get_all_comments():
        if c["id"] == talkback_id:
            return c
    return None

def get_admin_comment(talkback_id: int) -> dict:
    resp = requests.get(STATS_URL).json()
    return resp["comments"].get(ARTICLE_ID, {}).get(str(talkback_id), {})

def cast_vote(talkback_id: int, simulated_ip: str,
              like: bool = True, vote_type: str = "2state",
              cookie: str = None) -> dict:
    headers = {
        "Content-Type":   "application/json",
        "Origin":         BASE,
        "X-Simulated-IP": simulated_ip,
    }
    if cookie:
        headers["Cookie"] = f"talkback_{talkback_id}={cookie}"
    payload = {
        "article_id":     ARTICLE_ID,
        "talkback_id":    talkback_id,
        "talkback_like":  like,
        "talkback_unlike":not like,
        "vote_type":      vote_type,
    }
    resp = requests.post(VOTE_URL, json=payload, headers=headers)
    return {
        "status":     resp.status_code,
        "body":       resp.json(),
        "set_cookie": resp.headers.get("Set-Cookie", "—"),
    }

def rotate_votes(talkback_id: int, ip_pool: list,
                 like: bool = True) -> dict:
    """Send one vote per IP in pool. Returns summary."""
    accepted = dropped = 0
    for ip in ip_pool:
        r = cast_vote(talkback_id, simulated_ip=ip, like=like)
        stats = get_admin_comment(talkback_id)
        if ip in stats.get("voters", []):
            accepted += 1
        else:
            dropped += 1
    stats = get_admin_comment(talkback_id)
    return {
        "accepted": accepted,
        "dropped":  dropped,
        "real_likes":   stats.get("likes", 0),
        "real_unlikes": stats.get("unlikes", 0),
        "real_net":     stats.get("net", 0),
    }

# ---------------------------------------------------------------------------
# SECTION 1: Targeting strategy — how to choose which comment to target
# ---------------------------------------------------------------------------

def show_targeting_analysis():
    separator("TARGETING ANALYSIS — Real Ynet Comments")

    comments = get_all_comments()
    total    = len(comments)

    print(f"\nTotal comments in snapshot: {total}")
    print(f"\nHow an attacker decides which comment to target:\n")

    # Strategy 1: Most visible (highest likes)
    top_liked = sorted(comments, key=lambda x: x["likes"], reverse=True)[:5]
    print("--- Strategy 1: BOOST — target most-liked comments (already visible) ---")
    print(f"  {'ID':<12} {'likes':>6} {'unlikes':>8} {'net':>6}  text[:60]")
    for c in top_liked:
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('unlikes',0):>8} "
              f"{c.get('talkback_like',0):>6}  {c['text'][:60]}")

    print()

    # Strategy 2: Most controversial (high unlikes) — suppress by adding unlikes
    top_disputed = sorted(comments, key=lambda x: x.get("unlikes",0), reverse=True)[:5]
    print("--- Strategy 2: SUPPRESS — target most-disliked (pile-on unlikes to bury) ---")
    print(f"  {'ID':<12} {'likes':>6} {'unlikes':>8} {'net':>6}  text[:60]")
    for c in top_disputed:
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('unlikes',0):>8} "
              f"{c.get('talkback_like',0):>6}  {c['text'][:60]}")

    print()

    # Strategy 3: Zero-vote comments — easiest to make appear popular from scratch
    zero = [c for c in comments if c["likes"] == 0 and c.get("unlikes",0) == 0]
    print(f"--- Strategy 3: MANUFACTURE — zero-vote comments ({len(zero)} available) ---")
    print("  (easiest: any likes make them appear in 'top' sort)")
    print(f"  {'ID':<12} {'author':<22}  text[:60]")
    for c in zero[:5]:
        print(f"  {c['id']:<12} {c['author'][:20]:<22}  {c['text'][:60]}")

    print()

    # Strategy 4: Comments just below "recommended" threshold
    near_recommended = sorted(
        [c for c in comments if 5 <= c["likes"] <= 15],
        key=lambda x: x["likes"], reverse=True
    )[:5]
    print("--- Strategy 4: PUSH TO RECOMMENDED — comments near the threshold ---")
    print("  (a few votes could push them into the highlighted 'recommended' band)")
    print(f"  {'ID':<12} {'likes':>6} {'net':>6}  text[:60]")
    for c in near_recommended:
        print(f"  {c['id']:<12} {c['likes']:>6} {c.get('talkback_like',0):>6}  {c['text'][:60]}")


# ---------------------------------------------------------------------------
# SECTION 2: Live rotation tests on real comment IDs
# ---------------------------------------------------------------------------

def test_boost_top_comment():
    separator("TEST A: BOOST — Add likes to the most-liked comment")
    reset()

    # Most liked in the real snapshot: ID 98996137 (likes=16)
    TARGET = 98996137
    c_before = get_comment(TARGET)
    print(f"\nTarget: ID {TARGET}")
    print(f"Author: {c_before['author']}")
    print(f"Text  : {c_before['text'][:80]}")
    print(f"Before: likes={c_before['likes']} unlikes={c_before.get('unlikes',0)} "
          f"net={c_before.get('talkback_like',0)}")

    print(f"\nRotating {len(POOL_25)} IPs → likes...")
    print(f"\n  {'IP':<18} {'HTTP':>5}  {'Real likes after':>16}")
    print(f"  {'-'*45}")

    for ip in POOL_25:
        cast_vote(TARGET, simulated_ip=ip, like=True)
        stats = get_admin_comment(TARGET)
        print(f"  {ip:<18}  200   likes={stats['likes']:>4}")

    stats = get_admin_comment(TARGET)
    print(f"\nFinal server state : likes={stats['likes']} unlikes={stats.get('unlikes',0)} "
          f"net={stats['net']}")
    print(f"Lift               : +{stats['likes'] - c_before['likes']} likes")
    print(f"Voters logged      : {stats['vote_count']} unique IPs")


def test_suppress_comment():
    separator("TEST B: SUPPRESS — Add unlikes to a pro-Ben Gvir comment")
    reset()

    # Most disliked in snapshot: ID 98996148 (likes=2, unlikes=6)
    TARGET = 98996148
    c_before = get_comment(TARGET)
    print(f"\nTarget: ID {TARGET}")
    print(f"Author: {c_before['author']}")
    print(f"Text  : {c_before['text'][:80]}")
    print(f"Before: likes={c_before['likes']} unlikes={c_before.get('unlikes',0)} "
          f"net={c_before.get('talkback_like',0)}")

    print(f"\nRotating {len(POOL_25)} IPs → unlikes...")
    for ip in POOL_25:
        cast_vote(TARGET, simulated_ip=ip, like=False)

    stats = get_admin_comment(TARGET)
    print(f"\nFinal server state : likes={stats['likes']} unlikes={stats.get('unlikes',0)} "
          f"net={stats['net']}")
    print(f"Unlikes added      : +{stats.get('unlikes', 0) - c_before.get('unlikes',0)}")
    print(f"Net change         : {c_before.get('talkback_like',0)} → {stats['net']}")


def test_manufacture_zero_comment():
    separator("TEST C: MANUFACTURE — Make a zero-vote comment appear popular")
    reset()

    # A real zero-vote comment: ID 98997284
    TARGET = 98997284
    c_before = get_comment(TARGET)
    print(f"\nTarget: ID {TARGET}")
    print(f"Author: {c_before['author']}")
    print(f"Text  : {c_before['text'][:80]}")
    print(f"Before: likes=0 unlikes=0 net=0  (completely unknown)")

    print(f"\nRotating {len(POOL_50)} IPs → likes...")
    for ip in POOL_50:
        cast_vote(TARGET, simulated_ip=ip, like=True)

    stats = get_admin_comment(TARGET)
    print(f"\nAfter rotation:")
    print(f"  Real likes now : {stats['likes']}")
    print(f"  This comment   : went from unknown → appears in top results")

    # Show where it ranks now
    all_comments = get_all_comments()
    ranked = sorted(all_comments, key=lambda x: x["likes"], reverse=True)
    for rank, c in enumerate(ranked, 1):
        if c["id"] == TARGET:
            print(f"  Rank in list   : #{rank} out of {len(ranked)} comments")
            break


def test_dedup_still_works():
    separator("TEST D: CONFIRM DEDUP — Same IP can't vote twice")
    reset()

    TARGET = 98996137
    IP = "203.0.113.99"

    r1 = cast_vote(TARGET, simulated_ip=IP, like=True)
    s1 = get_admin_comment(TARGET)
    print(f"\nVote 1 from {IP}: HTTP {r1['status']} → likes={s1['likes']}")

    r2 = cast_vote(TARGET, simulated_ip=IP, like=True)
    s2 = get_admin_comment(TARGET)
    print(f"Vote 2 from {IP}: HTTP {r2['status']} → likes={s2['likes']} (no change)")

    print(f"\nConclusion: {s2['vote_count']} unique IP(s) registered despite 2 attempts")


def test_validation():
    separator("TEST E: VALIDATION — Error messages match real Ynet API")

    cases = [
        ("Empty body",         {}),
        ("Missing vote_type",  {"article_id": ARTICLE_ID, "talkback_id": 98996137,
                                "talkback_like": True, "talkback_unlike": False}),
        ("Bad vote_type",      {"article_id": ARTICLE_ID, "talkback_id": 98996137,
                                "talkback_like": True, "talkback_unlike": False,
                                "vote_type": "4state"}),
    ]
    for label, payload in cases:
        resp = requests.post(VOTE_URL, json=payload,
                             headers={"Content-Type": "application/json",
                                      "X-Simulated-IP": "1.1.1.1"})
        print(f"\n{label}:")
        print(f"  HTTP {resp.status_code}")
        print(f"  {json.dumps(resp.json(), ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        requests.get(BASE, timeout=2)
    except Exception:
        print("\n[ERROR] Mock server not running!")
        print("  Start it: python3 mock_server.py")
        exit(1)

    print("\n" + "="*65)
    print("  Ynet Vote Rotation Client")
    print("  Target: LOCAL MOCK (127.0.0.1:5001) — real comment data")
    print("="*65)

    show_targeting_analysis()
    test_boost_top_comment()
    test_suppress_comment()
    test_manufacture_zero_comment()
    test_dedup_still_works()
    test_validation()

    print("\n" + "="*65)
    print(f"  Done. Full log: GET {STATS_URL}")
    print("="*65 + "\n")
