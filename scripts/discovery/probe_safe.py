#!/usr/bin/env python3
"""Memory-safe async proxy probe with checkpointing & resume.

Uses aiohttp + aiohttp-socks. ~150 concurrent tasks is ~1/3 the memory of
150 threads because coroutines share a single event loop stack.

Checkpoint files (relative to repo root):
  proxies/hits_checkpoint.json         — list of confirmed working proxies
  scripts/discovery/sources/probe_cursor.txt — lines skipped from candidates.txt
  scripts/discovery/sources/probe.log        — human log (appended)

Resume: just re-run — cursor + hits checkpoint are read on startup.

Stop conditions:
  - reached --target hits (default 500)
  - exhausted candidates
  - received SIGINT
"""
import argparse, asyncio, json, os, signal, sys, time
import aiohttp
from aiohttp_socks import ProxyConnector

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CAND   = os.path.join(_REPO_ROOT, "scripts", "discovery", "sources", "candidates.txt")
HITS   = os.path.join(_REPO_ROOT, "proxies", "hits_checkpoint.json")
CURSOR = os.path.join(_REPO_ROOT, "scripts", "discovery", "sources", "probe_cursor.txt")
LOG    = os.path.join(_REPO_ROOT, "scripts", "discovery", "sources", "probe.log")

YNET_URL = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
IPIFY    = "https://api.ipify.org?format=json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0",
    "Origin":     "https://www.ynet.co.il",
    "Referer":    "https://www.ynet.co.il/news/article/yokra14737379",
}

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def load_hits():
    if os.path.exists(HITS):
        try: return json.load(open(HITS))
        except Exception: return []
    return []

def save_hits(hits):
    tmp = HITS + ".tmp"
    json.dump(hits, open(tmp, "w"), indent=2)
    os.replace(tmp, HITS)

def load_cursor():
    if os.path.exists(CURSOR):
        try: return int(open(CURSOR).read().strip() or 0)
        except Exception: return 0
    return 0

def save_cursor(n):
    with open(CURSOR + ".tmp", "w") as f: f.write(str(n))
    os.replace(CURSOR + ".tmp", CURSOR)

STATE = {
    "hits": [],
    "seen_ips": set(),
    "seen_addrs": set(),
    "tried": 0,
    "t0": 0.0,
    "stop": False,
}

async def probe_one(scheme, addr, ynet_timeout, ip_timeout):
    url = f"{scheme}://{addr}"
    try:
        conn = ProxyConnector.from_url(url, rdns=True)
    except Exception:
        return None
    timeout = aiohttp.ClientTimeout(total=ynet_timeout)
    try:
        async with aiohttp.ClientSession(connector=conn, timeout=timeout,
                                         trust_env=False) as s:
            t0 = time.time()
            async with s.get(YNET_URL, headers=HEADERS) as r:
                if r.status != 200: return None
                body = await r.content.read(1200)
                if not any(t in body for t in (b"rss", b"talkback", b"success", b"<?xml")):
                    return None
                dt = int((time.time() - t0) * 1000)
            # best-effort exit-IP lookup; proxy still counts as working if ipify fails
            ip = None
            try:
                async with s.get(IPIFY,
                                 timeout=aiohttp.ClientTimeout(total=ip_timeout)) as r:
                    if r.status == 200:
                        ip = json.loads(await r.text()).get("ip")
            except Exception:
                pass
            return {"scheme": scheme, "addr": addr, "exit_ip": ip, "ynet_ms": dt}
    except Exception:
        return None

async def worker(name, queue, sem, args):
    while not STATE["stop"]:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if queue.empty(): return
            continue
        if item is None: return
        line_no, scheme, addr = item
        STATE["tried"] += 1
        async with sem:
            if STATE["stop"]: queue.task_done(); return
            rec = await probe_one(scheme, addr, args.timeout, args.ip_timeout)
        if rec:
            ip = rec["exit_ip"]
            # dedupe by addr always; by exit IP only when we have one
            if addr in STATE["seen_addrs"]: pass
            elif ip and ip in STATE["seen_ips"]: pass
            else:
                STATE["seen_addrs"].add(addr)
                if ip: STATE["seen_ips"].add(ip)
                STATE["hits"].append(rec)
                n = len(STATE["hits"])
                log(f"HIT #{n} {scheme:6s} {addr:22s} exit={ip or '?':16s} ynet={rec['ynet_ms']}ms")
                if n % 10 == 0:
                    save_hits(STATE["hits"])
                    save_cursor(line_no)
                if n >= args.target:
                    STATE["stop"] = True
                    log(f"target {args.target} reached — stopping")
        queue.task_done()

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target",      type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=120)
    ap.add_argument("--timeout",     type=float, default=8.0)
    ap.add_argument("--ip-timeout",  type=float, default=5.0)
    ap.add_argument("--limit",       type=int, default=0, help="stop after N candidates (0 = all)")
    ap.add_argument("--reset",       action="store_true", help="ignore existing checkpoint")
    args = ap.parse_args()

    if args.reset:
        if os.path.exists(HITS):   os.remove(HITS)
        if os.path.exists(CURSOR): os.remove(CURSOR)

    STATE["hits"] = load_hits()
    STATE["seen_ips"] = {h["exit_ip"] for h in STATE["hits"] if h.get("exit_ip")}
    STATE["seen_addrs"] = {h["addr"] for h in STATE["hits"] if h.get("addr")}
    skip = load_cursor()
    STATE["t0"] = time.time()

    log(f"starting | target={args.target} concurrency={args.concurrency} "
        f"skip={skip} prior_hits={len(STATE['hits'])}")

    def sigint(*_):
        log("SIGINT — saving checkpoint and stopping")
        STATE["stop"] = True
    signal.signal(signal.SIGINT, sigint)
    signal.signal(signal.SIGTERM, sigint)

    queue = asyncio.Queue(maxsize=args.concurrency * 4)
    sem = asyncio.Semaphore(args.concurrency)

    workers = [asyncio.create_task(worker(f"w{i}", queue, sem, args))
               for i in range(args.concurrency)]

    enqueued = 0
    last_checkpoint_at = len(STATE["hits"])
    with open(CAND) as f:
        for i, line in enumerate(f):
            if i < skip: continue
            if STATE["stop"]: break
            if args.limit and enqueued >= args.limit: break
            line = line.strip()
            if not line: continue
            try: scheme, addr = line.split()
            except ValueError: continue
            if addr in STATE["seen_addrs"]: continue
            await queue.put((i + 1, scheme, addr))
            enqueued += 1
            if enqueued % 500 == 0:
                # periodic cursor & hits flush (even without new hits)
                save_cursor(i + 1)
                if len(STATE["hits"]) != last_checkpoint_at:
                    save_hits(STATE["hits"])
                    last_checkpoint_at = len(STATE["hits"])
                dt = time.time() - STATE["t0"]
                rate = STATE["tried"] / max(dt, 0.1)
                log(f"progress: enqueued={enqueued} tried={STATE['tried']} "
                    f"hits={len(STATE['hits'])} rate={rate:.1f}/s")

    # drain
    await queue.join()
    STATE["stop"] = True
    for w in workers: w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    save_hits(STATE["hits"])
    save_cursor(skip + enqueued)
    dt = time.time() - STATE["t0"]
    log(f"DONE in {dt:.0f}s | tried={STATE['tried']} hits={len(STATE['hits'])}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
