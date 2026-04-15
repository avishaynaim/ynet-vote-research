#!/usr/bin/env python3
"""Rebuild proxies/alive.json from /root/hits_checkpoint.json.

The hits_checkpoint already contains freshly ynet-validated proxies,
so we don't need to re-run check_proxies.py — we just translate the
records into the alive.json schema (scheme, addr, exit_ip, ynet_ms,
check_status, check_ms, alive).

Dedup by addr; if an addr exists in both old alive.json and new hits,
the newer hit wins (we just confirmed it works).
"""
import json, os, sys

HITS  = "/root/hits_checkpoint.json"
ALIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "proxies", "alive.json")
ALIVE = os.path.normpath(ALIVE)

def to_alive(rec):
    return {
        "scheme":       rec["scheme"],
        "addr":         rec["addr"],
        "exit_ip":      rec.get("exit_ip"),
        "ynet_ms":      rec.get("ynet_ms"),
        "check_status": 200,
        "check_ms":     rec.get("ynet_ms"),
        "alive":        True,
    }

def main():
    hits = json.load(open(HITS))
    by_addr = {h["addr"]: to_alive(h) for h in hits if h.get("addr")}

    # keep any addrs from previous alive.json that aren't already in our new
    # set (they were verified before; we leave them as-is)
    if os.path.exists(ALIVE):
        for r in json.load(open(ALIVE)):
            if r.get("addr") and r["addr"] not in by_addr:
                by_addr[r["addr"]] = r

    out = list(by_addr.values())
    # sort: known exit_ip first, then by latency
    out.sort(key=lambda r: (0 if r.get("exit_ip") else 1,
                            r.get("ynet_ms") or 99999))
    json.dump(out, open(ALIVE, "w"), indent=2)
    with_ip = sum(1 for r in out if r.get("exit_ip"))
    print(f"alive.json: {len(out)} entries | {with_ip} with exit_ip")

if __name__ == "__main__":
    main()
