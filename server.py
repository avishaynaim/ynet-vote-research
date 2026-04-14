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
from flask import Flask, request, make_response, jsonify, send_from_directory
import requests as req

YNET_BASE = "https://www.ynet.co.il"
DEFAULT_PROXIES_FILE = "/root/proxies_alive.json"

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
    print(f"  Proxies : {len(proxy_pool)} loaded from {proxies_file}")
    print(f"  Workers : {proxy_workers} parallel  |  timeout {proxy_timeout}s")

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
        url = (
            f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2"
            f"/{article_id}/0/1"
        )
        try:
            r = req.get(url, headers=PROXY_HEADERS, timeout=10)
            r.raise_for_status()
            items = r.json().get("rss", {}).get("channel", {}).get("item", [])
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
            } for c in items]
            return cors(make_response(jsonify({
                "comments": comments,
                "total":    len(comments),
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

        url     = f"{YNET_BASE}/iphone/json/api/talkbacks/vote"
        payload = {
            "article_id":      article_id,
            "talkback_id":     talkback_id,
            "talkback_like":   like,
            "talkback_unlike": not like,
            "vote_type":       "2state",
        }
        headers = {**PROXY_HEADERS, "Content-Type": "application/json"}

        # Randomize proxy order each batch (no deterministic pattern)
        # and fan out in parallel. Stop as soon as `count` HTTP 200s land;
        # any still-pending proxies are cancelled.
        if proxy_pool:
            shuffled = random.sample(proxy_pool, len(proxy_pool))
            used_proxies = True
        else:
            shuffled = [None] * count
            used_proxies = False

        def _one(entry):
            px_label = entry["label"] if entry else "direct"
            try:
                if entry:
                    r = req.post(url, json=payload, headers=headers,
                                 proxies=entry["proxies"], timeout=proxy_timeout)
                else:
                    r = req.post(url, json=payload, headers=headers, timeout=10)
                return {"proxy": px_label, "status": r.status_code,
                        "ok": r.status_code == 200}
            except Exception as exc:
                return {"proxy": px_label,
                        "status": f"ERR:{type(exc).__name__}", "ok": False}

        sent = ok = errors = 0
        per_proxy = []
        ex = ThreadPoolExecutor(max_workers=proxy_workers)
        try:
            futures = [ex.submit(_one, e) for e in shuffled]
            for fut in as_completed(futures):
                res = fut.result()
                per_proxy.append({"proxy": res["proxy"], "status": res["status"]})
                sent += 1
                if res["ok"]:
                    ok += 1
                else:
                    errors += 1
                if ok >= count:
                    break
        finally:
            # Don't wait for in-flight requests still running.
            ex.shutdown(wait=False, cancel_futures=True)

        return cors(make_response(jsonify({
            "talkback_id":   talkback_id,
            "article_id":    article_id,
            "like":          like,
            "sent":          sent,
            "ok":            ok,
            "errors":        errors,
            "used_proxies":  used_proxies,
            "pool_size":     len(proxy_pool),
            "per_proxy":     per_proxy,
            "note":          "Ynet returns success even for deduped votes. "
                             "Actual count change visible after CDN cache expires (~87s).",
        })))

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
