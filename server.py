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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
    proxy_workers = int(cfg.get("proxy_workers", 20))
    jitter_min = float(cfg.get("vote_jitter_min_s", 0.0))
    jitter_max = float(cfg.get("vote_jitter_max_s", 10.0))
    log_lock = threading.Lock()
    pool_lock = threading.Lock()
    failure_counts = {}   # proxy_url -> consecutive failure count
    EVICT_AFTER = 3       # remove proxy after this many consecutive failures
    MIN_POOL    = 5       # never evict below this size
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
    log_path = os.path.join(
        results_dir, f"web_client_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    log_lock = threading.Lock()
    print(f"  Client events → {log_path}")

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
                r = req.get(url, headers=PROXY_HEADERS, timeout=10)
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
            return cors(make_response(jsonify({
                "comments":      comments,
                "total":         len(comments),
                "sum_talkbacks": sum_talkbacks,
            })))
        except Exception as e:
            return cors(make_response(jsonify({"error": str(e)}), 502))

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

        if proxy_pool:
            # Pick `count` proxies randomly (with replacement if count > pool).
            # ynet doesn't strictly dedup by IP, so reuse is fine.
            to_try = [random.choice(proxy_pool) for _ in range(count)]
        else:
            to_try = [None] * count
        total_to_try = len(to_try)
        burst_workers = min(proxy_workers, total_to_try)

        # Persistent vote log — every attempt is recorded to JSONL so we can
        # analyze dedup behavior, cooldown windows, and per-proxy patterns.
        vote_log_path = os.path.join(results_dir, "vote_log.jsonl")

        def _update_proxy_health(addr, ok):
            if ok:
                failure_counts.pop(addr, None)
                return
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
                rec = {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "proxy": px_label, "addr": px_addr,
                    "talkback_id": talkback_id, "article_id": article_id,
                    "like": like, "status": r.status_code,
                    "ok": r.status_code == 200, "elapsed_s": elapsed,
                    "response": resp_body,
                }
                with log_lock:
                    with open(vote_log_path, "a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ok_vote = r.status_code == 200
                if entry:
                    _update_proxy_health(px_addr, ok_vote)
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
                if entry:
                    _update_proxy_health(px_addr, False)
                return {"proxy": px_label,
                        "status": f"ERR:{type(exc).__name__}", "ok": False}

        def generate():
            sent = ok = errors = 0
            target = count
            # Keep retrying until we hit the target number of successes.
            # Cap total attempts at 10x target to avoid infinite loops if
            # all proxies are dead.
            max_attempts = target * 10

            while ok < target and sent < max_attempts:
                # Submit a batch: enough to fill the remaining target, plus
                # a buffer based on observed failure rate.
                remaining = target - ok
                # Estimate how many to send based on success rate so far
                if sent > 0 and ok > 0:
                    rate = ok / sent
                    batch = int(remaining / rate * 1.3) + 10
                else:
                    batch = remaining + 20  # first round: add small buffer
                batch = max(batch, 10)
                batch = min(batch, max_attempts - sent)

                with pool_lock:
                    pool_snap = list(proxy_pool)
                picks = [random.choice(pool_snap) for _ in range(batch)] if pool_snap else []
                if not picks:
                    break
                workers = min(proxy_workers, batch)
                ex = ThreadPoolExecutor(max_workers=workers)
                try:
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
                    ex.shutdown(wait=False, cancel_futures=True)

            yield f"data: {json.dumps({'t':'done','s':sent,'o':ok,'e':errors,'n':target,'pool_size':pool_size})}\n\n"

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
    app.run(host=host, port=port, debug=False)
