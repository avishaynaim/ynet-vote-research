#!/usr/bin/env python3
"""
Headless vote client — runs vote campaigns for a fixed duration with no browser.

Usage:
    python3 headless_client.py --talkback-id 99256137 --count 500 --like --duration 48
    python3 headless_client.py --talkback-id 99027115 --count 200 --dislike --duration 24 --pause 30

Args:
    --talkback-id   Talkback comment ID to vote on (required)
    --count         Votes per round (default 500)
    --like          Vote like (default)
    --dislike       Vote dislike
    --duration      Total run hours (default 48)
    --pause         Seconds to wait between rounds (default 10)
    --server        Server base URL (default http://127.0.0.1:5001)
    --article-id    Article ID (default from config.json)
    --config        Path to config.json
"""

import argparse
import json
import os
import sys
import signal
import time
from datetime import datetime, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

STOP = False


def handle_signal(sig, frame):
    global STOP
    print("\n[headless] Caught signal — stopping after this round...")
    STOP = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def load_config(path):
    with open(path) as f:
        return json.load(f)


def log(logfile, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    logfile.write(line + "\n")
    logfile.flush()


def run_round(server, article_id, talkback_id, count, like, timeout=360):
    url = f"{server}/vote/batch"
    payload = {
        "article_id":   article_id,
        "talkback_id":  talkback_id,
        "count":        count,
        "like":         like,
    }
    t0 = time.time()
    sent = ok = err = 0

    try:
        resp = requests.post(url, json=payload, stream=True, timeout=timeout)
        resp.raise_for_status()

        buf = ""
        for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
            if STOP:
                break
            buf += chunk
            # Parse SSE events out of buffer
            parts = buf.split("\n\n")
            buf = parts.pop()
            for part in parts:
                for line in part.split("\n"):
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                        sent = ev.get("sent", sent)
                        ok   = ev.get("ok",   ok)
                        err  = ev.get("err",  err)
                    except Exception:
                        pass

        elapsed = round(time.time() - t0, 1)
        return {"sent": sent, "ok": ok, "err": err, "elapsed": elapsed, "error": None}

    except requests.exceptions.Timeout:
        elapsed = round(time.time() - t0, 1)
        return {"sent": sent, "ok": ok, "err": err, "elapsed": elapsed,
                "error": f"timeout after {elapsed}s"}
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        return {"sent": sent, "ok": ok, "err": err, "elapsed": elapsed, "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="Headless ynet vote client (48h capable)")
    parser.add_argument("--talkback-id", type=int, required=True)
    parser.add_argument("--count",       type=int, default=500,                    help="Votes per round")
    parser.add_argument("--duration",    type=float, default=48.0,                 help="Total run hours")
    parser.add_argument("--pause",       type=float, default=10.0,                 help="Seconds between rounds")
    parser.add_argument("--server",      default="http://127.0.0.1:5001")
    parser.add_argument("--article-id",  default=None, dest="article_id")
    parser.add_argument("--config",      default=os.path.join(BASE_DIR, "config.json"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--like",    action="store_true", default=True)
    action.add_argument("--dislike", action="store_true", default=False)
    args = parser.parse_args()

    like = not args.dislike

    cfg = load_config(args.config)
    article_id = args.article_id or cfg.get("article_id", "")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(RESULTS_DIR, f"headless_{ts}.jsonl")
    logfile = open(log_path, "a")

    deadline = datetime.now() + timedelta(hours=args.duration)
    action_str = "👍 like" if like else "👎 dislike"

    print("=" * 65)
    print(f"  Headless vote client")
    print(f"  Article    : {article_id}")
    print(f"  Talkback   : #{args.talkback_id}")
    print(f"  Action     : {action_str}  ×{args.count} per round")
    print(f"  Duration   : {args.duration}h  (until {deadline.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  Pause      : {args.pause}s between rounds")
    print(f"  Server     : {args.server}")
    print(f"  Log        : {log_path}")
    print("=" * 65)

    total_ok = total_err = round_num = 0

    while datetime.now() < deadline and not STOP:
        round_num += 1
        remaining = deadline - datetime.now()
        rem_h = int(remaining.total_seconds() // 3600)
        rem_m = int((remaining.total_seconds() % 3600) // 60)

        log(logfile, f"Round #{round_num}  remaining={rem_h}h{rem_m:02d}m  total_ok={total_ok}")

        result = run_round(
            server=args.server,
            article_id=article_id,
            talkback_id=args.talkback_id,
            count=args.count,
            like=like,
        )

        total_ok  += result["ok"]
        total_err += result["err"]

        rec = {
            "round": round_num,
            "ts":    datetime.now().isoformat(timespec="seconds"),
            **result,
            "total_ok": total_ok,
            "total_err": total_err,
        }
        logfile.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logfile.flush()

        if result["error"]:
            log(logfile, f"  ERROR: {result['error']}  (sent={result['sent']} ok={result['ok']})")
        else:
            log(logfile, f"  sent={result['sent']}  ok={result['ok']}  err={result['err']}  "
                         f"elapsed={result['elapsed']}s  cumulative_ok={total_ok}")

        if STOP or datetime.now() >= deadline:
            break

        # Sleep in 1s ticks so we can catch SIGINT quickly
        for _ in range(int(args.pause)):
            if STOP or datetime.now() >= deadline:
                break
            time.sleep(1)

    logfile.close()
    print("\n" + "=" * 65)
    print(f"  Done.  rounds={round_num}  total_ok={total_ok}  total_err={total_err}")
    print(f"  Log: {log_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
