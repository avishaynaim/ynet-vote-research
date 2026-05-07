#!/usr/bin/env python3
"""Merge hits_checkpoint.json + unique_working_proxies.json → unique_working_proxies_v2.json.
Dedupe by exit_ip (last-writer wins).
"""
import json, os, sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OLD  = os.path.join(_REPO_ROOT, "proxies", "unique.json")
NEW  = os.path.join(_REPO_ROOT, "proxies", "hits_checkpoint.json")
OUT  = os.path.join(_REPO_ROOT, "proxies", "unique_v2.json")

old  = json.load(open(OLD))
new  = json.load(open(NEW))
by_ip = {}
for rec in old + new:
    ip = rec.get("exit_ip")
    if ip: by_ip[ip] = rec
out = list(by_ip.values())
out.sort(key=lambda r: r.get("ynet_ms", 99999))
json.dump(out, open(OUT, "w"), indent=2)
print(f"old: {len(old)}  new: {len(new)}  merged unique exit_ips: {len(out)}")
