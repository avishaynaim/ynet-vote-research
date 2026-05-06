#!/usr/bin/env python3
"""
Proxy Monitor — watches vote success rate and keeper health, acts autonomously.

Every CHECK_INTERVAL seconds:
  1. Checks server is up (restarts if down)
  2. Reads recent vote_log → computes live OK rate
  3. Checks keeper is alive and not stuck
  4. If OK rate is bad AND keeper is idle → starts a new keeper cycle
  5. Prints a one-line health summary

Run in background:
    python3 proxy_monitor.py > /tmp/proxy_monitor.log 2>&1 &
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

REPO           = os.path.dirname(os.path.abspath(__file__))
VOTE_LOG       = os.path.join(REPO, "results", "vote_log.jsonl")
KEEPER_LOG     = "/tmp/proxy_keeper.log"
SERVER_LOG     = "/tmp/ynet_server.log"

CHECK_INTERVAL = 300          # seconds between health checks (5 min)
VOTE_WINDOW    = 300          # look at votes from last N entries
OK_RATE_WARN   = 0.20         # warn below 20%
OK_RATE_TRIGGER= 0.10         # start keeper below 10%
KEEPER_STUCK_S = 25 * 60      # keeper silent for 25 min during active cycle = stuck
SERVER_URL     = "http://127.0.0.1:5001/api/proxy_capacity"


# ── Logging ────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Server health ──────────────────────────────────────────────────────────

def server_alive():
    try:
        with urllib.request.urlopen(SERVER_URL, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def start_server():
    log("  ACTION: starting server...")
    subprocess.Popen(
        ["python3", "server.py"],
        cwd=REPO,
        stdout=open(SERVER_LOG, "a"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(3)
    if server_alive():
        log("  server started OK")
    else:
        log("  server start failed — check /tmp/ynet_server.log")


# ── Vote log analysis ──────────────────────────────────────────────────────

def vote_stats():
    """Return (ok, total, ok_rate) from last VOTE_WINDOW lines of vote_log."""
    if not os.path.exists(VOTE_LOG):
        return 0, 0, 0.0
    try:
        with open(VOTE_LOG) as f:
            lines = f.readlines()
        recent = lines[-VOTE_WINDOW:]
        total = ok = 0
        for line in recent:
            try:
                e = json.loads(line)
                total += 1
                if e.get("ok"):
                    ok += 1
            except Exception:
                pass
        rate = ok / total if total else 0.0
        return ok, total, rate
    except Exception:
        return 0, 0, 0.0


# ── Keeper health ──────────────────────────────────────────────────────────

def keeper_pid():
    """Return PID of running proxy_keeper.py, or None."""
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "proxy_keeper.py" in line and "grep" not in line:
                return int(line.split()[0]) if line.split()[0].isdigit() else int(line.split()[1])
    except Exception:
        pass
    return None


def keeper_status():
    """
    Returns one of: 'running', 'sleeping', 'stuck', 'dead'
    Also returns seconds since last log line.
    """
    pid = keeper_pid()
    if not os.path.exists(KEEPER_LOG):
        return ("dead", None) if not pid else ("running", 0)

    try:
        with open(KEEPER_LOG) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return "dead" if not pid else "running", 0

        last = lines[-1]
        # Parse timestamp from "[HH:MM:SS] ..."
        ts_str = last[1:9] if last.startswith("[") else None
        age_s = None
        if ts_str:
            try:
                now = datetime.now()
                t = datetime.strptime(ts_str, "%H:%M:%S").replace(
                    year=now.year, month=now.month, day=now.day
                )
                age_s = (now - t).total_seconds()
                if age_s < 0:
                    age_s += 86400  # crossed midnight
            except Exception:
                pass

        if not pid:
            return "dead", age_s

        if "sleeping" in last:
            return "sleeping", age_s

        # Active cycle — stuck if no progress for 25+ min
        if age_s and age_s > KEEPER_STUCK_S:
            return "stuck", age_s

        return "running", age_s
    except Exception:
        return ("dead", None) if not pid else ("running", None)


def start_keeper():
    log("  ACTION: starting proxy_keeper.py...")
    # Truncate keeper log so next status check sees fresh output
    open(KEEPER_LOG, "w").close()
    subprocess.Popen(
        ["python3", "proxy_keeper.py"],
        cwd=REPO,
        stdout=open(KEEPER_LOG, "w"),
        stderr=subprocess.STDOUT,
    )
    log("  keeper started")


def kill_keeper():
    pid = keeper_pid()
    if pid:
        try:
            os.kill(pid, 15)  # SIGTERM
            log(f"  ACTION: killed stuck keeper (pid {pid})")
        except Exception as e:
            log(f"  could not kill keeper: {e}")


# ── Server pool info ───────────────────────────────────────────────────────

def pool_info():
    try:
        with urllib.request.urlopen(SERVER_URL, timeout=5) as r:
            d = json.loads(r.read())
            return d.get("total_addresses", "?"), d.get("source", "?")
    except Exception:
        return "?", "?"


# ── Main loop ──────────────────────────────────────────────────────────────

def check():
    # 1. Server health
    if not server_alive():
        log("ALERT: server is DOWN")
        start_server()
        return

    # 2. Vote stats
    ok, total, rate = vote_stats()
    pool_sz, pool_src = pool_info()

    # 3. Keeper status
    k_status, k_age_s = keeper_status()
    k_age_str = f"{int(k_age_s)}s ago" if k_age_s is not None else "?"

    # 4. Summary line
    rate_pct = f"{rate*100:.0f}%"
    status_flag = "OK" if rate >= OK_RATE_WARN else ("WARN" if rate >= OK_RATE_TRIGGER else "BAD ")
    log(f"[{status_flag}] votes={ok}/{total} ok={rate_pct}  "
        f"pool={pool_sz}({pool_src})  keeper={k_status}(last={k_age_str})")

    # 5. Act
    if k_status == "stuck":
        log("  keeper appears stuck — restarting")
        kill_keeper()
        time.sleep(2)
        start_keeper()
        return

    if rate < OK_RATE_TRIGGER and k_status in ("dead", "sleeping"):
        log(f"  ok_rate {rate_pct} below {OK_RATE_TRIGGER*100:.0f}% — triggering keeper cycle")
        if k_status == "sleeping":
            kill_keeper()
            time.sleep(2)
        start_keeper()


def main():
    log(f"proxy_monitor starting  check_interval={CHECK_INTERVAL}s  "
        f"warn={OK_RATE_WARN*100:.0f}%  trigger={OK_RATE_TRIGGER*100:.0f}%")
    while True:
        try:
            check()
        except Exception as e:
            log(f"check error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
