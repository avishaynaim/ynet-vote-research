#!/usr/bin/env python3
"""Merge every known proxy source into /root/sources/candidates.txt — one 'scheme addr' per line.
Dedupes. Excludes addresses already confirmed in unique_working_proxies.json.
"""
import json, os, re, glob, random
random.seed(1337)

SOURCES = []
# legacy files in /root/
for path, default in [
    ("/root/clarketm.txt",    "http"),
    ("/root/hookzof_s5.txt",  "socks5"),
    ("/root/mmpx12_http.txt", "http"),
    ("/root/mmpx12_s4.txt",   "socks4"),
    ("/root/mmpx12_s5.txt",   "socks5"),
    ("/root/monosans_s4.txt", "socks4"),
    ("/root/monosans_s5.txt", "socks5"),
    ("/root/monosans.txt",    "http"),
    ("/root/proxifly.txt",    "http"),
    ("/root/ps_s4.txt",       "socks4"),
    ("/root/ps_s5.txt",       "socks5"),
    ("/root/socks4.txt",      "socks4"),
    ("/root/socks5.txt",      "socks5"),
    ("/root/sunny9577.txt",   "http"),
]:
    SOURCES.append((path, default))

# new fresh sources (phase 2 + phase 2b)
for p in sorted(glob.glob("/root/sources/*.txt")) + sorted(glob.glob("/root/sources/b2/*.txt")):
    name = os.path.basename(p).lower()
    if "s5" in name or "socks5" in name: sch = "socks5"
    elif "s4" in name or "socks4" in name: sch = "socks4"
    else: sch = "http"
    SOURCES.append((p, sch))

ADDR_RE = re.compile(r"^(?:([a-z]+)://)?([0-9]{1,3}(?:\.[0-9]{1,3}){3}):(\d{2,5})")

known_addrs = set()
known_ips = set()
try:
    for rec in json.load(open("/root/unique_working_proxies.json")):
        if rec.get("addr"): known_addrs.add(rec["addr"])
        if rec.get("exit_ip"): known_ips.add(rec["exit_ip"])
except Exception: pass

seen = set()
records = []
dropped_known = 0
for path, default in SOURCES:
    if not os.path.exists(path): continue
    try:
        for line in open(path, errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#"): continue
            line = line.split()[0].split(",")[0]
            m = ADDR_RE.match(line.lower())
            if not m: continue
            scheme = m.group(1) or default
            if scheme not in ("http", "socks4", "socks5"): scheme = default
            addr = f"{m.group(2)}:{m.group(3)}"
            if addr in known_addrs:
                dropped_known += 1; continue
            key = (scheme, addr)
            if key in seen: continue
            seen.add(key)
            records.append((scheme, addr))
    except Exception as e:
        print(f"WARN {path}: {e}")

random.shuffle(records)
total = len(records)
with open("/root/sources/candidates.txt", "w") as out:
    for scheme, addr in records:
        out.write(f"{scheme} {addr}\n")

print(f"unique candidates: {total}")
print(f"excluded (already working): {dropped_known}")
print(f"scheme breakdown:")
schemes = {}
for s, _ in seen: schemes[s] = schemes.get(s, 0) + 1
for s, c in sorted(schemes.items()): print(f"  {s}: {c}")
