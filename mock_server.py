#!/usr/bin/env python3
"""
Ynet Talkback Vote API — Mock Server
=====================================
All configuration is read from config.json — nothing is hardcoded here.

Run:
    python3 mock_server.py
    python3 mock_server.py --config /path/to/other_config.json
"""

import os
import sys
import json
import time
import threading
import argparse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response, send_from_directory

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def resolve_path(base_dir: str, relative: str) -> str:
    """Resolve a path relative to the config file's directory."""
    return os.path.join(base_dir, relative) if not os.path.isabs(relative) else relative

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(cfg: dict, base_dir: str) -> Flask:
    app = Flask(__name__)

    CACHE_TTL      = cfg["cache_ttl_seconds"]
    MAX_BATCH      = cfg["max_batch_votes"]
    DEFAULT_ARTICLE = cfg["article_id"]
    COMMENTS_FILE  = resolve_path(base_dir, cfg["comments_file"])
    ENDPOINTS      = cfg["endpoints"]

    # ── In-memory state ──────────────────────────────────────────────────────
    vote_db      : dict = {}   # {article_id: {talkback_id: comment_dict}}
    ip_log       : dict = {}   # {article_id: {talkback_id: set(ips)}}
    cache        : dict = {}   # {article_id: {data, expires_at}}
    request_log  : list = []
    lock = threading.Lock()

    # ── Seed data ─────────────────────────────────────────────────────────────

    def load_comments() -> list:
        try:
            with open(COMMENTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            items = data["rss"]["channel"]["item"]
            print(f"  Loaded {len(items)} comments from {COMMENTS_FILE}")
            return items
        except Exception as e:
            print(f"  [WARN] Could not load comments file ({e}). Using empty set.")
            return []

    def init_db(article_id: str = None):
        aid = article_id or DEFAULT_ARTICLE
        vote_db[aid] = {}
        ip_log[aid]  = {}
        cache.pop(aid, None)
        for c in load_comments():
            tid = c["id"]
            vote_db[aid][tid] = {
                "id":                 tid,
                "author":             c.get("author", ""),
                "text":               c.get("text", ""),
                "likes":              c.get("likes", 0),
                "unlikes":            c.get("unlikes", 0),
                "pubDate":            c.get("pubDate", ""),
                "level":              c.get("level", 1),
                "recommended":        c.get("recommended", False),
                "talkback_parent_id": c.get("talkback_parent_id"),
                "post_id":            c.get("post_id"),
                "article_id":         aid,
            }
            ip_log[aid][tid] = set()

    init_db()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def cors(resp):
        resp.headers["access-control-allow-origin"]  = "*"
        resp.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
        resp.headers["access-control-allow-headers"] = "Content-Type, X-Simulated-IP"
        return resp

    def preflight():
        return cors(make_response("", 204))

    def invalidate_cache(article_id: str):
        cache.pop(article_id, None)

    def build_list_response(article_id: str) -> dict:
        with lock:
            comments = list(vote_db.get(article_id, {}).values())
        comments.sort(key=lambda c: c["id"], reverse=True)
        items = []
        for c in comments:
            items.append({
                "id":                 c["id"],
                "number":             len(items) + 1,
                "author":             c["author"],
                "pubDate":            c["pubDate"],
                "text":               c["text"],
                "level":              c["level"],
                "recommended":        c["recommended"],
                "talkback_like":      c["likes"] - c["unlikes"],
                "authorLocation":     "",
                "talkback_parent_id": c["talkback_parent_id"],
                "post_id":            c["post_id"],
                "article_id":         article_id,
                "likes":              c["likes"],
                "unlikes":            c["unlikes"],
            })
        return {
            "rss": {
                "channel": {
                    "hasMore":         0,
                    "sum_talkbacks":   len(items),
                    "sum_discussions": len(items),
                    "item":            items,
                }
            }
        }

    def get_cached_list(article_id: str):
        entry = cache.get(article_id)
        now   = time.time()
        if entry and now < entry["expires_at"]:
            return entry["data"], True
        data = build_list_response(article_id)
        cache[article_id] = {"data": data, "expires_at": now + CACHE_TTL}
        return data, False

    def get_effective_ip() -> str:
        """
        Use X-Simulated-IP if present (mock-only feature for testing).
        Otherwise fall back to real remote address.
        Real Ynet ignores X-Forwarded-For — we document this by keeping
        the two paths explicit.
        """
        return (request.headers.get("X-Simulated-IP")
                or request.remote_addr
                or "127.0.0.1")

    def log_req(endpoint: str, ip: str, payload: dict, result: str):
        request_log.append({
            "time":      datetime.now(timezone.utc).isoformat(),
            "endpoint":  endpoint,
            "source_ip": ip,
            "payload":   payload,
            "result":    result,
        })

    def process_vote(article_id, talkback_id, talkback_like, talkback_unlike,
                     vote_type, inbound_cookie, effective_ip):
        """
        Core vote logic — extracted so both /vote and /vote/batch share it.
        Returns (result_label, cookie_action)
        cookie_action: None | ("set", value) | "delete"
        """
        with lock:
            vote_db.setdefault(article_id, {}).setdefault(talkback_id, {
                "id": talkback_id, "author": "", "text": "",
                "likes": 0, "unlikes": 0, "pubDate": "",
                "level": 1, "recommended": False,
                "talkback_parent_id": None, "post_id": None,
                "article_id": article_id,
            })
            ip_log.setdefault(article_id, {}).setdefault(talkback_id, set())

            already  = effective_ip in ip_log[article_id][talkback_id]
            comment  = vote_db[article_id][talkback_id]
            result   = "NOOP"
            cookie   = None

            if talkback_like:
                if inbound_cookie == "True":
                    if not already:
                        comment["likes"] = max(0, comment["likes"] - 1)
                    result = "TOGGLE_OFF"
                elif inbound_cookie == "False":
                    if not already:
                        comment["unlikes"] = max(0, comment["unlikes"] - 1)
                    result = "NEUTRALIZED"
                    cookie = "delete"
                else:
                    if not already:
                        comment["likes"] += 1
                        ip_log[article_id][talkback_id].add(effective_ip)
                        invalidate_cache(article_id)
                        result = "ACCEPTED"
                    else:
                        result = "DROPPED_IP_DEDUP"
                    cookie = ("set", "True")

            elif talkback_unlike:
                if inbound_cookie == "False":
                    if not already:
                        comment["unlikes"] = max(0, comment["unlikes"] - 1)
                    result = "TOGGLE_OFF"
                elif inbound_cookie == "True":
                    if not already:
                        comment["likes"]   = max(0, comment["likes"] - 1)
                        comment["unlikes"] += 1
                    result = "SWITCHED_DISLIKE"
                    cookie = "delete"
                else:
                    if not already:
                        comment["unlikes"] += 1
                        ip_log[article_id][talkback_id].add(effective_ip)
                        invalidate_cache(article_id)
                        result = "ACCEPTED"
                    else:
                        result = "DROPPED_IP_DEDUP"
                    cookie = ("set", "False")

        return result, cookie

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/")
    def serve_ui():
        return send_from_directory(base_dir, "web_ui.html")

    @app.route("/api/config", methods=["GET", "OPTIONS"])
    def api_config():
        if request.method == "OPTIONS":
            return preflight()
        resp = make_response(jsonify({
            "article_id":    DEFAULT_ARTICLE,
            "cache_ttl":     CACHE_TTL,
            "max_batch":     MAX_BATCH,
            "endpoints":     ENDPOINTS,
        }))
        return cors(resp)

    @app.route("/api/comments", methods=["GET", "OPTIONS"])
    def api_comments():
        if request.method == "OPTIONS":
            return preflight()
        article_id = request.args.get("article_id", DEFAULT_ARTICLE)
        if article_id not in vote_db:
            init_db(article_id)
        with lock:
            comments = list(vote_db.get(article_id, {}).values())
        comments.sort(key=lambda c: c["id"], reverse=True)
        out = []
        for c in comments:
            tid    = c["id"]
            voters = ip_log.get(article_id, {}).get(tid, set())
            out.append({
                "id":          tid,
                "author":      c["author"],
                "text":        c["text"],
                "pubDate":     c["pubDate"],
                "likes":       c["likes"],
                "unlikes":     c["unlikes"],
                "net":         c["likes"] - c["unlikes"],
                "recommended": c["recommended"],
                "vote_count":  len(voters),
            })
        return cors(make_response(jsonify({"comments": out, "total": len(out)})))

    list_path = ENDPOINTS["list"].replace("{article_id}", "<article_id>") \
                                  .replace("{sort}", "<sort>") \
                                  .replace("{page}", "<int:page>")

    @app.route(list_path, methods=["GET"])
    def talkbacks_list(article_id, sort, page):
        data, is_hit = get_cached_list(article_id)
        resp = make_response(jsonify(data))
        resp.headers["vx-cache"]      = "HIT" if is_hit else "MISS"
        resp.headers["cache-control"] = f"private, max-age={CACHE_TTL}"
        resp.headers["osv"]           = "c8-mock"
        return cors(resp)

    @app.route(ENDPOINTS["vote"], methods=["POST", "OPTIONS"])
    def talkbacks_vote():
        if request.method == "OPTIONS":
            return preflight()

        body    = request.get_json(silent=True) or {}
        ip      = get_effective_ip()
        errors  = {}
        required = ["article_id", "talkback_id", "talkback_like",
                    "talkback_unlike", "vote_type"]
        for f in required:
            if f not in body:
                errors[f] = "dataclass field is missing a value"
        if not errors and body.get("vote_type") not in ("2state", "3state"):
            errors["vote_type"] = (
                f"'{body['vote_type']}' is not a valid value for 'VoteType'. "
                "Expected one of ['2state', '3state']"
            )
        if errors:
            log_req("/vote", ip, body, "VALIDATION_ERROR")
            return cors(make_response(
                jsonify({"errors": errors, "message": "Validation Error"}), 400))

        article_id   = body["article_id"]
        talkback_id  = int(body["talkback_id"])
        inbound_cookie = request.cookies.get(f"talkback_{talkback_id}")

        result, cookie = process_vote(
            article_id, talkback_id,
            body["talkback_like"], body["talkback_unlike"],
            body["vote_type"], inbound_cookie, ip
        )
        log_req("/vote", ip, body, result)
        print(f"  [{result:<22}] ip={ip} article={article_id} tb={talkback_id}")

        resp = make_response(jsonify({"success": True}))
        resp.headers["cache-control"] = "private, max-age=100"
        resp.headers["osv"]           = "c8-mock"
        if cookie == "delete":
            resp.set_cookie(f"talkback_{talkback_id}", "",
                            expires=0, max_age=0, path="/")
        elif isinstance(cookie, tuple):
            resp.set_cookie(f"talkback_{talkback_id}", cookie[1], path="/")
        return cors(resp)

    @app.route(ENDPOINTS["vote_batch"], methods=["POST", "OPTIONS"])
    def vote_batch():
        if request.method == "OPTIONS":
            return preflight()

        body        = request.get_json(silent=True) or {}
        article_id  = body.get("article_id", DEFAULT_ARTICLE)
        talkback_id = int(body.get("talkback_id", 0))
        count       = min(int(body.get("count", 1)), MAX_BATCH)
        like        = bool(body.get("like", True))

        if not talkback_id:
            return cors(make_response(
                jsonify({"error": "talkback_id required"}), 400))

        with lock:
            c = vote_db.get(article_id, {}).get(talkback_id, {})
            before_likes   = c.get("likes", 0)
            before_unlikes = c.get("unlikes", 0)

        accepted = dropped = 0
        base = 10 if like else 20

        for i in range(count):
            fake_ip = f"{base}.{(i//65025)%256}.{(i//255)%256}.{(i%255)+1}"
            result, _ = process_vote(
                article_id, talkback_id, like, not like,
                "2state", None, fake_ip
            )
            log_req("/vote/batch", fake_ip, {"article_id": article_id,
                "talkback_id": talkback_id, "like": like}, result)
            if result == "ACCEPTED":
                accepted += 1
            else:
                dropped += 1

        with lock:
            c_after        = vote_db.get(article_id, {}).get(talkback_id, {})
            after_likes    = c_after.get("likes", 0)
            after_unlikes  = c_after.get("unlikes", 0)

        print(f"  [BATCH] tb={talkback_id} like={like} count={count} "
              f"accepted={accepted} likes:{before_likes}→{after_likes}")

        return cors(make_response(jsonify({
            "talkback_id": talkback_id,
            "article_id":  article_id,
            "like":        like,
            "count":       count,
            "accepted":    accepted,
            "dropped":     dropped,
            "before":      {"likes": before_likes,  "unlikes": before_unlikes},
            "after":       {"likes": after_likes,   "unlikes": after_unlikes},
            "net_change":  (after_likes - before_likes) if like
                           else (after_unlikes - before_unlikes),
        })))

    @app.route(ENDPOINTS["stats"], methods=["GET", "OPTIONS"])
    def admin_stats():
        if request.method == "OPTIONS":
            return preflight()
        with lock:
            stats = {}
            for aid, comments in vote_db.items():
                stats[aid] = {}
                for tid, c in comments.items():
                    voters = list(ip_log.get(aid, {}).get(tid, set()))
                    stats[aid][str(tid)] = {
                        "likes":      c["likes"],
                        "unlikes":    c["unlikes"],
                        "net":        c["likes"] - c["unlikes"],
                        "voters":     voters,
                        "vote_count": len(voters),
                    }
        return cors(make_response(jsonify({
            "comments":       stats,
            "request_log":    request_log[-100:],
            "total_requests": len(request_log),
        })))

    @app.route(ENDPOINTS["reset"], methods=["POST", "OPTIONS"])
    def admin_reset():
        if request.method == "OPTIONS":
            return preflight()
        article_id = (request.get_json(silent=True) or {}).get(
            "article_id", DEFAULT_ARTICLE)
        with lock:
            vote_db.pop(article_id, None)
            ip_log.pop(article_id, None)
            cache.pop(article_id, None)
            for entry in list(request_log):
                request_log.remove(entry)
        init_db(article_id)
        return cors(make_response(jsonify({"reset": True, "article_id": article_id})))

    return app, cfg["server"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ynet Vote Mock Server")
    parser.add_argument("--config", default="config.json",
                        help="Path to config.json (default: ./config.json)")
    args = parser.parse_args()

    config_path = os.path.abspath(args.config)
    base_dir    = os.path.dirname(config_path)
    cfg         = load_config(config_path)

    print("=" * 55)
    print("  Ynet Talkback Mock Server")
    print("=" * 55)
    print(f"  Config  : {config_path}")
    print(f"  Article : {cfg['article_id']}")
    print(f"  Cache   : {cfg['cache_ttl_seconds']}s")
    print(f"  Max batch: {cfg['max_batch_votes']} votes")

    app, server = create_app(cfg, base_dir)
    host = server["host"]
    port = server["port"]

    print(f"  UI      : http://{host}:{port}/")
    print(f"  API     : http://{host}:{port}/api/config")
    print("=" * 55)

    app.run(host=host, port=port, debug=False)
