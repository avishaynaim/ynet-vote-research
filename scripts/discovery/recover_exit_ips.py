#!/usr/bin/env python3
"""Recover exit IPs for confirmed-working proxies whose ipify lookup timed out
during the main probe. Reads /root/hits_checkpoint.json, retries IPIFY with
generous timeout via 4 fallback endpoints, writes back in place.
"""
import asyncio, json, os, time
import aiohttp
from aiohttp_socks import ProxyConnector

HITS = "/root/hits_checkpoint.json"
LOG  = "/root/recover.log"
ENDPOINTS = [
    ("https://api.ipify.org?format=json",   "ip"),
    ("https://api64.ipify.org?format=json", "ip"),
    ("https://ifconfig.co/json",            "ip"),
    ("https://ipinfo.io/json",              "ip"),
]
HEADERS = {"User-Agent": "Mozilla/5.0"}
CONC = 60
TIMEOUT = 25.0

def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

async def lookup(scheme, addr):
    try: conn = ProxyConnector.from_url(f"{scheme}://{addr}", rdns=True)
    except Exception: return None
    try:
        async with aiohttp.ClientSession(
            connector=conn, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            headers=HEADERS, trust_env=False) as s:
            for url, key in ENDPOINTS:
                try:
                    async with s.get(url) as r:
                        if r.status != 200: continue
                        data = json.loads(await r.text())
                        ip = data.get(key)
                        if ip and "." in ip: return ip
                except Exception:
                    continue
    except Exception:
        pass
    return None

async def main():
    hits = json.load(open(HITS))
    todo = [i for i, r in enumerate(hits) if not r.get("exit_ip")]
    log(f"recover starting | total={len(hits)} need_ip={len(todo)}")
    sem = asyncio.Semaphore(CONC)
    found = 0
    async def work(idx):
        nonlocal found
        async with sem:
            ip = await lookup(hits[idx]["scheme"], hits[idx]["addr"])
        if ip:
            hits[idx]["exit_ip"] = ip
            found += 1
            if found % 10 == 0:
                tmp = HITS + ".tmp"
                json.dump(hits, open(tmp, "w"), indent=2)
                os.replace(tmp, HITS)
                log(f"  recovered {found} so far")
    await asyncio.gather(*(work(i) for i in todo))
    json.dump(hits, open(HITS + ".tmp", "w"), indent=2)
    os.replace(HITS + ".tmp", HITS)
    ips = {r["exit_ip"] for r in hits if r.get("exit_ip")}
    log(f"DONE | recovered={found} | unique exit_ip total={len(ips)}")

if __name__ == "__main__":
    asyncio.run(main())
