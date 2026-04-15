#!/usr/bin/env python3
"""Merge hits_checkpoint.json + unique_working_proxies.json → unique_working_proxies_v2.json.
Dedupe by exit_ip (last-writer wins).
"""
import json, sys

old  = json.load(open("/root/unique_working_proxies.json"))
new  = json.load(open("/root/hits_checkpoint.json"))
by_ip = {}
for rec in old + new:
    ip = rec.get("exit_ip")
    if ip: by_ip[ip] = rec
out = list(by_ip.values())
out.sort(key=lambda r: r.get("ynet_ms", 99999))
json.dump(out, open("/root/unique_working_proxies_v2.json", "w"), indent=2)
print(f"old: {len(old)}  new: {len(new)}  merged unique exit_ips: {len(out)}")
