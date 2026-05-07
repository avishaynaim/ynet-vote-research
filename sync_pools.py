#!/usr/bin/env python3
"""
Pool sync — merges all proxies/pool_*.json files into master_pool.json,
then pushes this machine's own pool file to GitHub.

Each machine writes only its own pool file, so there are never git conflicts.
master_pool.json is derived locally and NOT pushed (it's .gitignored).

Usage:
  On this machine (main server):
    python3 sync_pools.py --mine pool_main

  On remote harvest machine:
    python3 sync_pools.py --mine pool_remote

  --mine   base name (without .json) of this machine's pool file
  --push   also git-commit and push (default: True)
  --no-push  merge only, don't push

The script:
  1. git pull (get latest pool files from other machines)
  2. Merge all proxies/pool_*.json → proxies/master_pool.json (dedup by addr)
  3. Reload the local server if running
  4. git add + commit + push only this machine's pool file
"""

import argparse
import glob
import json
import os
import socket
import subprocess
import time
import urllib.request
from datetime import datetime

REPO          = os.path.dirname(os.path.abspath(__file__))
PROXIES_DIR   = os.path.join(REPO, "proxies")
MASTER        = os.path.join(PROXIES_DIR, "master_pool.json")
SERVER_RELOAD = "http://127.0.0.1:5001/admin/reload"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, **kw):
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kw)
    if result.returncode != 0:
        log(f"  CMD: {' '.join(cmd)}")
        log(f"  STDERR: {result.stderr.strip()}")
    return result


def git_pull():
    log("git pull...")
    r = run(["git", "pull", "--rebase", "--autostash"])
    log(f"  {r.stdout.strip() or r.stderr.strip()}")
    return r.returncode == 0


def merge_pools():
    pattern = os.path.join(PROXIES_DIR, "pool_*.json")
    files   = sorted(glob.glob(pattern))
    if not files:
        log("No pool_*.json files found — nothing to merge")
        return 0

    log(f"Merging {len(files)} pool files: {[os.path.basename(f) for f in files]}")

    seen    = {}   # addr -> entry (last-write wins for duplicates)
    totals  = {}
    for fpath in files:
        try:
            data = json.load(open(fpath))
            name = os.path.basename(fpath)
            totals[name] = len(data)
            for entry in data:
                addr = entry.get("addr")
                if addr:
                    seen[addr] = entry
        except Exception as e:
            log(f"  WARNING: could not read {fpath}: {e}")

    merged = sorted(seen.values(), key=lambda x: x.get("ynet_ms", 9999))

    log(f"  Per-file counts: {totals}")
    log(f"  Merged: {len(merged)} unique proxies")

    tmp = MASTER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MASTER)
    log(f"  Written → master_pool.json")
    return len(merged)


def reload_server():
    try:
        req = urllib.request.Request(SERVER_RELOAD, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            log(f"  Server reloaded → {resp.get('loaded')} proxies")
    except Exception:
        pass  # server might not be running on this machine


def git_push(mine_file):
    rel = os.path.join("proxies", mine_file + ".json")
    abs_path = os.path.join(REPO, rel)

    if not os.path.exists(abs_path):
        log(f"  {rel} does not exist — nothing to push")
        return False

    size = os.path.getsize(abs_path)
    count = len(json.load(open(abs_path)))
    log(f"Pushing {rel}  ({count} proxies, {size//1024}KB)...")

    run(["git", "add", rel])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"sync: {mine_file} — {count} proxies [{ts}]"
    r = run(["git", "commit", "-m", msg])
    if r.returncode != 0 and "nothing to commit" in r.stdout + r.stderr:
        log("  Nothing new to commit")
        return True
    if r.returncode != 0:
        log(f"  Commit failed: {r.stderr.strip()}")
        return False

    # Pull again in case of race, then push
    run(["git", "pull", "--rebase", "--autostash"])
    r = run(["git", "push"])
    if r.returncode == 0:
        log(f"  Pushed OK")
        return True
    else:
        log(f"  Push failed: {r.stderr.strip()}")
        return False


def main():
    hostname = socket.gethostname().replace(" ", "_").replace("/", "_")
    default_mine = f"pool_{hostname}"

    ap = argparse.ArgumentParser(description="Merge proxy pools and sync to GitHub")
    ap.add_argument("--mine", default=default_mine,
                    help=f"Base name of this machine's pool file (default: pool_<hostname> = {default_mine!r})")
    ap.add_argument("--no-push", action="store_true",
                    help="Merge only, do not git push")
    ap.add_argument("--pull-only", action="store_true",
                    help="Pull + merge + reload server, but do NOT push (server mode)")
    ap.add_argument("--loop", action="store_true",
                    help="Run repeatedly on an interval (use with --interval)")
    ap.add_argument("--interval", type=int, default=900,
                    help="Seconds between sync runs when --loop is set (default 900 = 15 min)")
    args = ap.parse_args()

    no_push = args.no_push or args.pull_only

    def run_once():
        log(f"=== sync_pools  mine={args.mine}.json  push={not no_push} ===")
        git_pull()
        total = merge_pools()
        reload_server()
        if not no_push:
            git_push(args.mine)
        else:
            log("pull-only mode: skipping git push")
        log(f"=== done: {total} proxies in master_pool.json ===\n")
        return total

    if args.loop:
        log(f"Loop mode: syncing every {args.interval}s")
        while True:
            run_once()
            log(f"Sleeping {args.interval}s...")
            time.sleep(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
