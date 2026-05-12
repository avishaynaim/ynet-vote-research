#!/usr/bin/env python3
"""
Ynet CORS Proxy Server
======================
Serves web_ui.html and proxies all API requests to the real Ynet API,
bypassing browser CORS restrictions.

Run:
    python3 server.py
    python3 server.py --config /path/to/other_config.json
"""

import os
import sys
import argparse
import json
import random
import time
import threading
import subprocess
import signal
import atexit
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from flask import Flask, request, make_response, jsonify, send_from_directory, Response, stream_with_context
import requests as req

YNET_BASE = "https://www.ynet.co.il"
DEFAULT_PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "proxies", "alive.json")

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_vote_history(path, votes_ok):
    """Rebuild votes_ok_by_talkback from vote_log.jsonl so dedup survives restarts."""
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not rec.get("ok"):
                    continue
                tid  = rec.get("talkback_id")
                like = rec.get("like")
                addr = rec.get("addr")
                if tid and addr is not None and like is not None:
                    votes_ok[(int(tid), bool(like))].add(addr)
                    n += 1
            except Exception:
                pass
    return n


def load_proxies(path: str) -> list:
    """Return list of requests-compatible {'http','https'} proxy dicts."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for p in data:
        scheme = p.get("scheme", "http")
        addr   = p.get("addr")
        if not addr:
            continue
        url = f"{scheme}://{addr}"
        out.append({
            "label":   p.get("exit_ip") or addr,
            "proxies": {"http": url, "https": url},
        })
    return out

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(cfg: dict, base_dir: str) -> Flask:
    app = Flask(__name__)

    DEFAULT_ARTICLE = cfg["article_id"]

    # ── Proxy pool (for outbound votes to ynet) ──────────────────────────────
    proxies_file = cfg.get("proxies_file", DEFAULT_PROXIES_FILE)
    proxy_pool   = load_proxies(proxies_file)
    proxy_timeout = int(cfg.get("proxy_timeout", 20))
    # Cap at 60 workers per campaign — proot kills at 150 threads.
    # 2 concurrent campaigns × 60 workers + Flask overhead ≈ 135 threads, safe.
    # Lower than this kills throughput: 88% of proxies fail, so you need volume.
    proxy_workers = min(int(cfg.get("proxy_workers", 60)), 60)
    jitter_min = float(cfg.get("vote_jitter_min_s", 0.0))
    jitter_max = float(cfg.get("vote_jitter_max_s", 10.0))
    log_lock = threading.Lock()
    pool_lock = threading.Lock()
    # Limit concurrent vote-batch campaigns so threads don't pile up past proot's limit.
    _campaign_sem = threading.Semaphore(2)
    failure_counts = {}   # proxy_url -> consecutive failure count
    EVICT_AFTER = 3       # remove proxy after this many consecutive failures
    MIN_POOL    = 5       # never evict below this size
    from collections import defaultdict
    votes_ok_by_talkback   = defaultdict(set)  # (talkback_id, like) -> proxies that got 200
    votes_hard_fail        = defaultdict(set)  # (talkback_id, like) -> proxies ynet rejected (non-200, non-403)

    # ── Multi-round campaign infrastructure ──────────────────────────────────
    CAMPAIGN_ROUNDS  = 10
    CAMPAIGN_DELAY_S = 600   # 10-minute gap between rounds
    _campaigns: dict = {}    # camp_id → state dict
    _camp_lock = threading.Lock()

    def _cast_one(entry, payload, vote_url, talkback_id, like=True):
        """Single proxy vote — shared by both old batch endpoint and campaigns."""
        px_label = entry["label"] if entry else "direct"
        px_addr  = entry["proxies"]["http"] if entry else "direct"
        t0 = time.time()
        try:
            hdrs = {**PROXY_HEADERS, "Content-Type": "application/json"}
            if entry:
                r = req.post(vote_url, json=payload, headers=hdrs,
                             proxies=entry["proxies"], timeout=proxy_timeout)
            else:
                r = req.post(vote_url, json=payload, headers=hdrs, timeout=10)
            ok = r.status_code == 200
            if entry:
                failure_counts.pop(px_addr, None)
                if ok:
                    votes_ok_by_talkback[(talkback_id, like)].add(px_addr)
                elif r.status_code == 403:
                    # Akamai IP-reputation block — permanently dead for all talkbacks
                    with pool_lock:
                        if len(proxy_pool) > MIN_POOL:
                            proxy_pool[:] = [e for e in proxy_pool
                                             if e["proxies"]["http"] != px_addr]
                else:
                    votes_hard_fail[(talkback_id, like)].add(px_addr)
            return {"ok": ok, "proxy": px_label, "addr": px_addr,
                    "status": r.status_code, "elapsed": round(time.time() - t0, 2)}
        except Exception as exc:
            if entry:
                cnt = failure_counts.get(px_addr, 0) + 1
                failure_counts[px_addr] = cnt
                if cnt >= EVICT_AFTER:
                    with pool_lock:
                        if len(proxy_pool) > MIN_POOL:
                            proxy_pool[:] = [e for e in proxy_pool
                                             if e["proxies"]["http"] != px_addr]
                    failure_counts.pop(px_addr, None)
            return {"ok": False, "proxy": px_label, "addr": px_addr,
                    "status": f"ERR:{type(exc).__name__}",
                    "elapsed": round(time.time() - t0, 2)}

    def _campaign_runner(camp_id):
        """Background thread: runs 10 rounds with 10-min gaps, fully decoupled from HTTP."""
        camp      = _campaigns[camp_id]
        vote_url  = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
        vote_log  = os.path.join(results_dir, "vote_log.jsonl")

        if not _campaign_sem.acquire(blocking=True, timeout=10):
            with _camp_lock:
                camp["status"] = "error"
                camp["error"]  = "server busy — max 2 concurrent campaigns"
            return

        try:
            for rn in range(1, CAMPAIGN_ROUNDS + 1):
                with _camp_lock:
                    if camp["status"] == "cancelled":
                        return
                    camp["status"]        = "running"
                    camp["current_round"] = rn
                    rd = {"round": rn, "sent": 0, "ok": 0, "errors": 0,
                          "started_at": datetime.now().isoformat(), "finished_at": None}
                    camp["rounds"].append(rd)

                tid  = camp["talkback_id"]
                aid  = camp["article_id"]
                like = camp["like"]
                payload = {
                    "article_id":      aid,
                    "talkback_id":     tid,
                    "talkback_like":   like,
                    "talkback_unlike": not like,
                    "vote_type":       "2state",
                }

                with pool_lock:
                    snap = list(proxy_pool)
                ok_set   = votes_ok_by_talkback.get((tid, like), set())
                fail_set = votes_hard_fail.get((tid, like), set())
                fresh    = [p for p in snap
                            if p["proxies"]["http"] not in ok_set
                            and p["proxies"]["http"] not in fail_set]
                picks = fresh if fresh else snap
                if not picks:
                    rd["finished_at"] = datetime.now().isoformat()
                    continue

                workers = min(proxy_workers, len(picks))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_cast_one, e, payload, vote_url, tid, like)
                            for e in picks]
                    for fut in as_completed(futs):
                        res = fut.result()
                        with _camp_lock:
                            rd["sent"]   += 1
                            rd["ok"]     += 1 if res["ok"] else 0
                            rd["errors"] += 0 if res["ok"] else 1
                            camp["total_ok"]   = sum(r["ok"]   for r in camp["rounds"])
                            camp["total_sent"] = sum(r["sent"] for r in camp["rounds"])
                        rec = {
                            "ts": datetime.now().isoformat(timespec="milliseconds"),
                            "campaign": camp_id, "round": rn,
                            "proxy": res["proxy"], "addr": res["addr"],
                            "talkback_id": tid, "article_id": aid,
                            "like": like, "status": res["status"],
                            "ok": res["ok"], "elapsed_s": res["elapsed"],
                        }
                        with log_lock:
                            with open(vote_log, "a") as f:
                                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

                rd["finished_at"] = datetime.now().isoformat()

                if rn < CAMPAIGN_ROUNDS:
                    next_at = datetime.now() + timedelta(seconds=CAMPAIGN_DELAY_S)
                    with _camp_lock:
                        camp["status"]        = "waiting"
                        camp["next_round_at"] = next_at.isoformat()
                    # Sleep in 5-s ticks so cancellation is responsive
                    for _ in range(CAMPAIGN_DELAY_S // 5):
                        time.sleep(5)
                        with _camp_lock:
                            if camp["status"] == "cancelled":
                                return

            with _camp_lock:
                if camp["status"] != "cancelled":
                    camp["status"]      = "done"
                    camp["finished_at"] = datetime.now().isoformat()
        finally:
            _campaign_sem.release()

    # ── Comment cache — serve last good response if Ynet is temporarily down ──
    _comment_cache      = {}   # article_id -> {"data": ..., "ts": float}
    COMMENT_CACHE_TTL   = int(cfg.get("cache_ttl_seconds", 87))
    print(f"  Proxies : {len(proxy_pool)} loaded from {proxies_file}")
    print(f"  Workers : {proxy_workers} parallel  |  timeout {proxy_timeout}s")
    print(f"  Jitter  : {jitter_min:.1f}-{jitter_max:.1f}s random delay per vote")

    # ── Client-log sink ──────────────────────────────────────────────────────
    # Every event the browser emits (clicks, input changes, HTTP request/response,
    # init, auto-refresh, etc.) gets appended to a JSONL file in results/.
    # One file per server run; events from all browser tabs/sessions are interleaved
    # but each record carries a session_id so they can be split later.
    results_dir = os.path.join(base_dir, "results")
    os.makedirs(results_dir, exist_ok=True)
    _hist = _load_vote_history(os.path.join(results_dir, "vote_log.jsonl"), votes_ok_by_talkback)
    if _hist:
        print(f"  Vote history: {_hist:,} accepted votes loaded — used proxies won't be reused")
    log_path = os.path.join(
        results_dir, f"web_client_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    known_articles_file = os.path.join(results_dir, "known_articles.json")
    known_articles_lock = threading.Lock()
    log_lock = threading.Lock()
    print(f"  Client events → {log_path}")

    def _track_article_id(article_id):
        """Persist article_id to known_articles.json so proxy_keeper can use it."""
        if not article_id:
            return
        try:
            with known_articles_lock:
                try:
                    known = json.load(open(known_articles_file))
                except Exception:
                    known = []
                if article_id not in known:
                    known.append(article_id)
                    tmp = known_articles_file + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(known, f)
                    os.replace(tmp, known_articles_file)
        except Exception:
            pass

    PROXY_HEADERS = {
        "Origin":     YNET_BASE,
        "Referer":    f"{YNET_BASE}/news/article/{DEFAULT_ARTICLE}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    # ── CORS helpers ─────────────────────────────────────────────────────────

    def cors(resp):
        resp.headers["access-control-allow-origin"]  = "*"
        resp.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
        resp.headers["access-control-allow-headers"] = "Content-Type"
        resp.headers["cache-control"]                = "no-store, no-cache, must-revalidate"
        resp.headers["pragma"]                       = "no-cache"
        return resp

    def preflight():
        return cors(make_response("", 204))

    # ── Web UI ────────────────────────────────────────────────────────────────

    @app.route("/")
    def serve_ui():
        return send_from_directory(base_dir, "web_ui.html")

    # ── Proxy capacity (drives the UI's vote-count slider max) ───────────────
    # Computed from the live proxy_pool — distinct exit IPs we know about,
    # plus addresses with unknown exit IP (they may add new IPs). Excludes
    # known collisions so the cap reflects countable votes, not raw requests.

    @app.route("/api/proxy_capacity", methods=["GET", "OPTIONS"])
    def proxy_capacity():
        if request.method == "OPTIONS":
            return preflight()
        try:
            with open(proxies_file) as f:
                raw = json.load(f)
            unique_ips = {r["exit_ip"] for r in raw if r.get("exit_ip")}
            no_ip      = sum(1 for r in raw if not r.get("exit_ip"))
            return cors(make_response(jsonify({
                "total_addresses":    len(raw),
                "unique_exit_ips":    len(unique_ips),
                "unknown_exit_ip":    no_ip,
                "max_distinct_votes": len(unique_ips) + no_ip,
                "source":             os.path.basename(proxies_file),
            })))
        except Exception as e:
            return cors(make_response(jsonify({"error": str(e)}), 500))

    def _count_fresh(pool_snap, talkback_id, like=True):
        """Count proxies in pool that haven't voted on this talkback yet.
        Uses actual pool membership — evicted proxies are not subtracted."""
        ok_set   = votes_ok_by_talkback.get((talkback_id, like), set())
        fail_set = votes_hard_fail.get((talkback_id, like), set())
        fresh = sum(1 for p in pool_snap
                    if p["proxies"]["http"] not in ok_set
                    and p["proxies"]["http"] not in fail_set)
        ok_in_pool   = sum(1 for p in pool_snap if p["proxies"]["http"] in ok_set)
        fail_in_pool = sum(1 for p in pool_snap if p["proxies"]["http"] in fail_set)
        return fresh, ok_in_pool, fail_in_pool

    @app.route("/api/proxy_remaining/<int:talkback_id>", methods=["GET", "OPTIONS"])
    def proxy_remaining(talkback_id):
        if request.method == "OPTIONS":
            return preflight()
        like = request.args.get("like", "true").lower() != "false"
        with pool_lock:
            snap = list(proxy_pool)
        fresh, used_ok, hard_fail = _count_fresh(snap, talkback_id, like)
        return cors(make_response(jsonify({
            "talkback_id": talkback_id,
            "pool_total":  len(snap),
            "used_ok":     used_ok,
            "hard_fail":   hard_fail,
            "fresh":       fresh,
            "remaining":   fresh,
        })))

    @app.route("/api/proxies", methods=["GET", "OPTIONS"])
    def proxy_list():
        if request.method == "OPTIONS":
            return preflight()
        try:
            with open(proxies_file) as f:
                raw = json.load(f)
            offset = int(request.args.get("offset", 0))
            limit  = int(request.args.get("limit", 200))
            page   = raw[offset:offset + limit]
            return cors(make_response(jsonify({
                "total": len(raw),
                "offset": offset,
                "limit": limit,
                "proxies": page,
            })))
        except Exception as e:
            return cors(make_response(jsonify({"error": str(e)}), 500))

    # ── Per-talkback proxy usage stats ──────────────────────────────────────

    pool_size = len(proxy_pool)

    # ── Client event sink ────────────────────────────────────────────────────
    # Accepts a single event {...} or a batch {"events": [...]}.
    # Writes one JSON record per line. Fire-and-forget — always returns 204.

    @app.route("/client-log", methods=["POST", "OPTIONS"])
    def client_log():
        if request.method == "OPTIONS":
            return preflight()
        payload = request.get_json(silent=True) or {}
        events = payload.get("events")
        if events is None:
            events = [payload]
        server_ts = datetime.now().isoformat(timespec="milliseconds")
        remote = request.headers.get("X-Forwarded-For", request.remote_addr)
        ua = request.headers.get("User-Agent", "")
        with log_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                for e in events:
                    rec = {
                        "server_ts": server_ts,
                        "remote": remote,
                        "ua": ua,
                        **(e if isinstance(e, dict) else {"raw": e}),
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return cors(make_response("", 204))

    # ── /proxy/api/comments — normalises real Ynet talkbacks list ────────────

    @app.route("/proxy/api/comments", methods=["GET", "OPTIONS"])
    def proxy_comments():
        if request.method == "OPTIONS":
            return preflight()
        article_id = request.args.get("article_id", DEFAULT_ARTICLE)

        # Serve from cache if fresh enough
        cached = _comment_cache.get(article_id)
        if cached and (time.time() - cached["ts"]) < COMMENT_CACHE_TTL:
            return cors(make_response(jsonify(cached["data"])))

        # Ynet paginates the talkbacks list. Page 1 returns ~100 items and
        # `hasMore: 1` if more exist; keep walking until an empty page or
        # hasMore flips off. Cap at 50 pages as a safety rail.
        all_items = []
        seen_ids  = set()
        sum_talkbacks = None
        try:
            for page in range(1, 51):
                url = (f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2"
                       f"/{article_id}/0/{page}")
                hdrs = {**PROXY_HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"}
                r = req.get(url, headers=hdrs, timeout=10)
                r.raise_for_status()
                ch        = r.json().get("rss", {}).get("channel", {}) or {}
                items     = ch.get("item", []) or []
                has_more  = ch.get("hasMore")
                if sum_talkbacks is None:
                    sum_talkbacks = ch.get("sum_talkbacks")
                if not items:
                    break
                for c in items:
                    cid = c.get("id")
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    all_items.append(c)
                if not has_more:
                    break
            comments = [{
                "id":          c["id"],
                "author":      c.get("author", ""),
                "text":        c.get("text", ""),
                "pubDate":     c.get("pubDate", ""),
                "likes":       c.get("likes", 0),
                "unlikes":     c.get("unlikes", 0),
                "net":         c.get("talkback_like", 0),
                "recommended": c.get("recommended", False),
                "vote_count":  c.get("likes", 0),
            } for c in all_items]
            _track_article_id(article_id)
            payload = {"comments": comments, "total": len(comments),
                       "sum_talkbacks": sum_talkbacks}
            _comment_cache[article_id] = {"data": payload, "ts": time.time()}
            return cors(make_response(jsonify(payload)))
        except Exception as e:
            # Ynet is temporarily down — serve stale cache if we have it
            if cached:
                return cors(make_response(jsonify({**cached["data"], "stale": True})))
            return cors(make_response(jsonify({"error": str(e)}), 502))

    @app.route("/api/known_articles", methods=["GET", "OPTIONS"])
    def api_known_articles():
        if request.method == "OPTIONS":
            return preflight()
        try:
            known = json.load(open(known_articles_file))
        except Exception:
            known = [DEFAULT_ARTICLE]
        return cors(jsonify({"article_ids": known, "count": len(known)}))

    @app.route("/api/used_proxies", methods=["GET", "OPTIONS"])
    def api_used_proxies():
        if request.method == "OPTIONS":
            return preflight()
        all_used = set()
        for addrs in votes_ok_by_talkback.values():
            all_used.update(addrs)
        return cors(jsonify({"used_proxies": sorted(all_used), "count": len(all_used)}))

    # ── /proxy/vote/batch — sends N individual votes to real Ynet ────────────

    @app.route("/proxy/vote/batch", methods=["POST", "OPTIONS"])
    def proxy_vote_batch():
        if request.method == "OPTIONS":
            return preflight()
        body        = request.get_json(silent=True) or {}
        article_id  = body.get("article_id",  DEFAULT_ARTICLE)
        talkback_id = int(body.get("talkback_id", 0))
        count       = min(int(body.get("count", 1)), cfg["max_batch_votes"])
        like        = bool(body.get("like", True))

        if not talkback_id:
            return cors(make_response(jsonify({"error": "talkback_id required"}), 400))

        vote_url = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
        payload = {
            "article_id":      article_id,
            "talkback_id":     talkback_id,
            "talkback_like":   like,
            "talkback_unlike": not like,
            "vote_type":       "2state",
        }
        headers = {**PROXY_HEADERS, "Content-Type": "application/json"}

        # Persistent vote log — every attempt is recorded to JSONL so we can
        # analyze dedup behavior, cooldown windows, and per-proxy patterns.
        vote_log_path = os.path.join(results_dir, "vote_log.jsonl")

        def _mark_proxy_dead(addr):
            """Only called on network-level failure — proxy never reached ynet."""
            cnt = failure_counts.get(addr, 0) + 1
            failure_counts[addr] = cnt
            if cnt >= EVICT_AFTER:
                with pool_lock:
                    if len(proxy_pool) > MIN_POOL:
                        proxy_pool[:] = [e for e in proxy_pool
                                         if e["proxies"]["http"] != addr]
                failure_counts.pop(addr, None)

        def _one(entry):
            px_label = entry["label"] if entry else "direct"
            px_addr  = entry["proxies"]["http"] if entry else "direct"
            t0 = time.time()
            try:
                if entry:
                    r = req.post(vote_url, json=payload, headers=headers,
                                 proxies=entry["proxies"], timeout=proxy_timeout)
                else:
                    r = req.post(vote_url, json=payload, headers=headers, timeout=10)
                elapsed = round(time.time() - t0, 2)
                # Capture response body for analysis
                try:
                    resp_body = r.json()
                except:
                    resp_body = r.text[:500]
                ok_vote = r.status_code == 200
                rec = {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "proxy": px_label, "addr": px_addr,
                    "talkback_id": talkback_id, "article_id": article_id,
                    "like": like, "status": r.status_code,
                    "ok": ok_vote, "elapsed_s": elapsed,
                    "response": resp_body,
                }
                with log_lock:
                    with open(vote_log_path, "a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # Proxy reached ynet → it's alive regardless of HTTP status.
                if entry:
                    failure_counts.pop(px_addr, None)
                    if ok_vote:
                        votes_ok_by_talkback[(talkback_id, like)].add(px_addr)
                    elif r.status_code == 403:
                        # Akamai IP-reputation block — permanently dead for all talkbacks
                        with pool_lock:
                            if len(proxy_pool) > MIN_POOL:
                                proxy_pool[:] = [e for e in proxy_pool
                                                 if e["proxies"]["http"] != px_addr]
                    else:
                        votes_hard_fail[(talkback_id, like)].add(px_addr)
                return {"proxy": px_label, "status": r.status_code, "ok": ok_vote}
            except Exception as exc:
                elapsed = round(time.time() - t0, 2)
                rec = {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "proxy": px_label, "addr": px_addr,
                    "talkback_id": talkback_id, "article_id": article_id,
                    "like": like, "status": f"ERR:{type(exc).__name__}",
                    "ok": False, "elapsed_s": elapsed, "error": str(exc)[:200],
                }
                with log_lock:
                    with open(vote_log_path, "a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # Network-level failure: proxy never reached ynet → penalize it.
                if entry:
                    _mark_proxy_dead(px_addr)
                return {"proxy": px_label,
                        "status": f"ERR:{type(exc).__name__}", "ok": False}

        def generate():
            # Block if 2 campaigns are already running — avoids thread pile-up.
            if not _campaign_sem.acquire(blocking=True, timeout=5):
                yield f"data: {json.dumps({'t':'done','s':0,'o':0,'e':0,'n':count,'pool_size':pool_size,'remaining':0,'used':0,'hard_fail':0,'error':'server busy'})}\n\n"
                return
            try:
                sent = ok = errors = 0
                target = count
                # Cap total attempts at 10x target to avoid infinite loops.
                max_attempts = target * 10

                # Single executor for the whole campaign — never recreated per-loop.
                # Reusing it prevents thread accumulation across retries.
                workers = min(proxy_workers, target)
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    while ok < target and sent < max_attempts:
                        remaining = target - ok
                        if sent > 0 and ok > 0:
                            rate = ok / sent
                            batch = int(remaining / rate * 1.3) + 10
                        else:
                            batch = remaining + 20
                        batch = max(batch, 10)
                        batch = min(batch, max_attempts - sent)

                        with pool_lock:
                            pool_snap = list(proxy_pool)
                        ok_set   = votes_ok_by_talkback.get((talkback_id, like), set())
                        fail_set = votes_hard_fail.get((talkback_id, like), set())
                        fresh    = [p for p in pool_snap if p["proxies"]["http"] not in ok_set and p["proxies"]["http"] not in fail_set]
                        pick_from = fresh if fresh else pool_snap
                        if not pick_from:
                            break
                        picks = [random.choice(pick_from) for _ in range(batch)]
                        futures = [ex.submit(_one, e) for e in picks]
                        for fut in as_completed(futures):
                            res = fut.result()
                            sent += 1
                            if res["ok"]:
                                ok += 1
                            else:
                                errors += 1
                            yield f"data: {json.dumps({'t':'p','s':sent,'o':ok,'e':errors,'n':target})}\n\n"
                            if ok >= target:
                                break
            finally:
                _campaign_sem.release()

            with pool_lock:
                end_snap = list(proxy_pool)
            fresh, used_count, fail_count = _count_fresh(end_snap, talkback_id, like)
            yield f"data: {json.dumps({'t':'done','s':sent,'o':ok,'e':errors,'n':target,'pool_size':pool_size,'remaining':fresh,'used':used_count,'hard_fail':fail_count})}\n\n"

        resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
        resp.headers["access-control-allow-origin"]  = "*"
        resp.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
        resp.headers["access-control-allow-headers"] = "Content-Type"
        resp.headers["cache-control"]                = "no-store, no-cache"
        resp.headers["x-accel-buffering"]            = "no"
        return resp

    # ── Vote log analysis ──────────────────────────────────────────────────
    @app.route("/api/vote_log/analysis", methods=["GET", "OPTIONS"])
    def vote_log_analysis():
        """Analyze vote log to find dedup patterns."""
        if request.method == "OPTIONS":
            return preflight()
        vote_log_path = os.path.join(results_dir, "vote_log.jsonl")
        if not os.path.exists(vote_log_path):
            return cors(make_response(jsonify({"error": "No vote log yet"}), 404))

        tid_filter = request.args.get("talkback_id")
        records = []
        with open(vote_log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if tid_filter and str(rec.get("talkback_id")) != tid_filter:
                        continue
                    records.append(rec)
                except:
                    pass

        # Per-proxy stats
        proxy_stats = {}
        for r in records:
            p = r["proxy"]
            if p not in proxy_stats:
                proxy_stats[p] = {"ok": 0, "fail": 0, "errors": [], "timestamps": [], "responses": []}
            if r["ok"]:
                proxy_stats[p]["ok"] += 1
                proxy_stats[p]["timestamps"].append(r["ts"])
                proxy_stats[p]["responses"].append(r.get("response"))
            else:
                proxy_stats[p]["fail"] += 1
                proxy_stats[p]["errors"].append(r.get("status"))

        # Summary
        total_ok = sum(s["ok"] for s in proxy_stats.values())
        total_fail = sum(s["fail"] for s in proxy_stats.values())
        multi_success = {p: s for p, s in proxy_stats.items() if s["ok"] > 1}
        unique_responses = set()
        for r in records:
            if r["ok"]:
                resp = r.get("response")
                if isinstance(resp, dict):
                    unique_responses.add(json.dumps(resp, sort_keys=True))
                else:
                    unique_responses.add(str(resp))

        return cors(make_response(jsonify({
            "total_records": len(records),
            "total_ok": total_ok,
            "total_fail": total_fail,
            "unique_proxies_tried": len(proxy_stats),
            "unique_proxies_succeeded": sum(1 for s in proxy_stats.values() if s["ok"] > 0),
            "proxies_succeeded_multiple_times": len(multi_success),
            "multi_success_details": {p: {"ok": s["ok"], "timestamps": s["timestamps"]}
                                       for p, s in sorted(multi_success.items(), key=lambda x: -x[1]["ok"])[:20]},
            "unique_response_bodies": len(unique_responses),
            "sample_responses": list(unique_responses)[:5],
            "error_breakdown": {},
        })))

    @app.route("/api/vote_log/raw", methods=["GET", "OPTIONS"])
    def vote_log_raw():
        """Return last N vote log entries."""
        if request.method == "OPTIONS":
            return preflight()
        vote_log_path = os.path.join(results_dir, "vote_log.jsonl")
        if not os.path.exists(vote_log_path):
            return cors(make_response(jsonify([]), 200))
        n = int(request.args.get("n", 100))
        records = []
        with open(vote_log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except:
                        pass
        return cors(make_response(jsonify(records[-n:])))

    # ── Admin: hot-reload proxy pool ─────────────────────────────────────────

    @app.route("/admin/reload", methods=["POST", "OPTIONS"])
    def admin_reload():
        if request.method == "OPTIONS":
            return cors(make_response("", 204))
        nonlocal proxy_pool
        try:
            proxy_pool = load_proxies(proxies_file)
            return cors(jsonify({"loaded": len(proxy_pool), "source": proxies_file}))
        except Exception as e:
            return cors(make_response(jsonify({"error": str(e)}), 500))

    # ── Aliases — same handlers without /proxy prefix (backward compat + client)

    @app.route("/api/comments", methods=["GET", "OPTIONS"])
    def comments_alias():
        return proxy_comments()

    @app.route("/vote/batch", methods=["POST", "OPTIONS"])
    def vote_batch_alias():
        return proxy_vote_batch()

    # ── Multi-round campaign endpoints ───────────────────────────────────────

    @app.route("/vote/campaign", methods=["POST", "OPTIONS"])
    def create_campaign():
        if request.method == "OPTIONS":
            return preflight()
        body        = request.get_json(silent=True) or {}
        talkback_id = int(body.get("talkback_id", 0))
        article_id  = body.get("article_id", DEFAULT_ARTICLE)
        like        = bool(body.get("like", True))
        if not talkback_id:
            return cors(make_response(jsonify({"error": "talkback_id required"}), 400))
        camp_id = f"camp_{uuid.uuid4().hex[:8]}"
        camp = {
            "id":            camp_id,
            "talkback_id":   talkback_id,
            "article_id":    article_id,
            "like":          like,
            "total_rounds":  CAMPAIGN_ROUNDS,
            "current_round": 0,
            "status":        "starting",
            "next_round_at": None,
            "rounds":        [],
            "total_ok":      0,
            "total_sent":    0,
            "created_at":    datetime.now().isoformat(),
            "finished_at":   None,
        }
        with _camp_lock:
            _campaigns[camp_id] = camp
        threading.Thread(target=_campaign_runner, args=(camp_id,), daemon=True).start()
        return cors(jsonify({"campaign_id": camp_id}))

    @app.route("/vote/campaign/<camp_id>", methods=["GET", "OPTIONS"])
    def get_campaign(camp_id):
        if request.method == "OPTIONS":
            return preflight()
        with _camp_lock:
            raw = _campaigns.get(camp_id)
        if not raw:
            return cors(make_response(jsonify({"error": "not found"}), 404))
        camp = {
            **raw,
            "rounds": [dict(r) for r in raw["rounds"]],
        }
        if camp.get("status") == "waiting" and camp.get("next_round_at"):
            try:
                nra  = datetime.fromisoformat(camp["next_round_at"])
                camp["next_round_in_s"] = max(0, int((nra - datetime.now()).total_seconds()))
            except Exception:
                camp["next_round_in_s"] = None
        return cors(jsonify(camp))

    @app.route("/vote/campaigns", methods=["GET", "OPTIONS"])
    def list_campaigns():
        if request.method == "OPTIONS":
            return preflight()
        with _camp_lock:
            result = [{"id": c["id"], "talkback_id": c["talkback_id"],
                       "like": c["like"], "status": c["status"],
                       "current_round": c["current_round"],
                       "total_rounds": c["total_rounds"],
                       "total_ok": c["total_ok"], "total_sent": c["total_sent"],
                       "created_at": c["created_at"]}
                      for c in _campaigns.values()]
        return cors(jsonify(result))

    @app.route("/vote/campaign/<camp_id>", methods=["DELETE", "OPTIONS"])
    def cancel_campaign(camp_id):
        if request.method == "OPTIONS":
            return preflight()
        with _camp_lock:
            camp = _campaigns.get(camp_id)
            if camp and camp["status"] not in ("done", "cancelled"):
                camp["status"] = "cancelled"
                cancelled = True
            else:
                cancelled = bool(camp)
        return cors(jsonify({"ok": True, "cancelled": cancelled}))

    # ── Catch-all — forward any unmatched path to real Ynet ─────────────────
    # Handles rotation_client paths: /iphone/json/api/talkbacks/...
    # Also handles /proxy/<path> for transparency

    def _forward(path):
        target = f"{YNET_BASE}/{path}"
        try:
            if request.method == "POST":
                r = req.post(
                    target,
                    json=request.get_json(silent=True),
                    headers={**PROXY_HEADERS, "Content-Type": "application/json"},
                    timeout=10,
                )
            else:
                r = req.get(
                    target,
                    params=dict(request.args),
                    headers=PROXY_HEADERS,
                    timeout=10,
                )
            resp = make_response(r.content, r.status_code)
            resp.headers["Content-Type"] = r.headers.get(
                "Content-Type", "application/json"
            )
            return cors(resp)
        except Exception as e:
            return cors(make_response(jsonify({"error": str(e)}), 502))

    @app.route("/proxy/<path:path>", methods=["GET", "POST", "OPTIONS"])
    def proxy_generic(path):
        if request.method == "OPTIONS":
            return preflight()
        return _forward(path)

    @app.route("/<path:path>", methods=["GET", "POST", "OPTIONS"])
    def catchall(path):
        if request.method == "OPTIONS":
            return preflight()
        print(f"  [PROXY] {request.method} /{path} → {YNET_BASE}/{path}")
        return _forward(path)

    return app, cfg["server"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ynet CORS Proxy Server")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    config_path = os.path.abspath(args.config)
    base_dir    = os.path.dirname(config_path)
    cfg         = load_config(config_path)

    server = cfg["server"]
    host   = server["host"]
    port   = server["port"]

    print("=" * 55)
    print("  Ynet CORS Proxy Server")
    print("=" * 55)
    print(f"  UI      : http://{host}:{port}/")
    print(f"  Proxy   : http://{host}:{port}/proxy/...")
    print(f"  Target  : {YNET_BASE}")
    print("=" * 55)

    app, _ = create_app(cfg, base_dir)

    # ── Auto-launch background daemons ───────────────────────────────────────
    # Both processes are killed automatically when the server exits.
    _daemons = []

    def _spawn(label, cmd):
        # Kill any leftover copies from a previous server run before spawning fresh.
        script_name = os.path.basename(cmd[-1]) if cmd else ""
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", script_name], stderr=subprocess.DEVNULL).decode()
            for pid_str in out.split():
                pid = int(pid_str.strip())
                if pid != os.getpid():
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        except (subprocess.CalledProcessError, ValueError):
            pass
        log_path = f"/tmp/ynet_{label}.log"
        f = open(log_path, "a")
        p = subprocess.Popen(cmd, stdout=f, stderr=f, cwd=base_dir)
        _daemons.append(p)
        print(f"  {label:<14}: PID {p.pid}  → {log_path}")
        return p

    def _kill_daemons():
        for p in _daemons:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    atexit.register(_kill_daemons)

    print("=" * 55)
    print("  Starting background daemons")
    print("=" * 55)
    _spawn("proxy_keeper", [sys.executable, os.path.join(base_dir, "proxy_keeper.py")])
    _spawn("mega_harvest", [sys.executable,
                            os.path.join(base_dir, "scripts", "mega_harvest.py"),
                            "--loops", "0", "--concurrency", "120",
                            "--target", "0"])
    print("=" * 55)

    app.run(host=host, port=port, debug=False)
