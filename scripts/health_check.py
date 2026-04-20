#!/usr/bin/env python3
"""Health-check master_pool.json — keep only proxies that still reach ynet."""
import asyncio, aiohttp, json, time, sys
from aiohttp_socks import ProxyConnector

INPUT  = "proxies/master_pool.json"
OUTPUT = "proxies/alive.json"
YNET   = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
CONC   = 120
TIMEOUT = 8

async def check(sem, proxy, session_kwargs):
    scheme = proxy.get("scheme", "http")
    addr   = proxy["addr"]
    url    = f"{scheme}://{addr}"
    async with sem:
        try:
            conn = ProxyConnector.from_url(url)
            async with aiohttp.ClientSession(connector=conn, **session_kwargs) as s:
                async with s.get(YNET, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                    if r.status == 200:
                        return proxy
        except:
            pass
    return None

async def main():
    with open(INPUT) as f:
        pool = json.load(f)
    print(f"Testing {len(pool)} proxies against ynet (concurrency={CONC})...")

    sem = asyncio.Semaphore(CONC)
    kwargs = {"headers": {"User-Agent": "Mozilla/5.0"}}

    alive = []
    done = 0
    t0 = time.time()

    tasks = [check(sem, p, kwargs) for p in pool]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        done += 1
        if result:
            alive.append(result)
        if done % 50 == 0 or done == len(pool):
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  {done}/{len(pool)} checked — {len(alive)} alive ({rate:.0f}/s)", flush=True)

    with open(OUTPUT, "w") as f:
        json.dump(alive, f, indent=1)

    labels = {p.get("exit_ip") or p["addr"] for p in alive}
    print(f"\nDone: {len(alive)} alive ({len(labels)} unique labels) saved to {OUTPUT}")
    print(f"Time: {time.time()-t0:.0f}s")

if __name__ == "__main__":
    asyncio.run(main())
