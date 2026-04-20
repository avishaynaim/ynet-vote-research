#!/usr/bin/env python3
"""Merge every raw proxy source under scripts/discovery/sources/ into
scripts/discovery/sources/candidates.txt — one 'scheme addr' per line.
Dedupes. Excludes addresses already confirmed in proxies/unique.json.

Drop new raw lists into scripts/discovery/sources/ (or any subdir) as
'*.txt'. Scheme is inferred from the filename (contains 's5' / 'socks5'
-> socks5, 's4' / 'socks4' -> socks4, else http).
"""
import json, os, re, glob, random
random.seed(1337)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SOURCES_DIR = os.path.join(_REPO_ROOT, "scripts", "discovery", "sources")
PROXIES_DIR = os.path.join(_REPO_ROOT, "proxies")
KNOWN_FILES = [os.path.join(PROXIES_DIR, f) for f in
               ("unique.json", "alive.json", "unique_v2.json", "hits_checkpoint.json")]
OUT_FILE    = os.path.join(SOURCES_DIR, "candidates.txt")

SOURCES = []
for p in sorted(glob.glob(os.path.join(SOURCES_DIR, "**", "*.txt"), recursive=True)):
    if os.path.abspath(p) == os.path.abspath(OUT_FILE):
        continue
    name = os.path.basename(p).lower()
    if "s5" in name or "socks5" in name: sch = "socks5"
    elif "s4" in name or "socks4" in name: sch = "socks4"
    else: sch = "http"
    SOURCES.append((p, sch))

ADDR_RE = re.compile(r"^(?:([a-z]+)://)?([0-9]{1,3}(?:\.[0-9]{1,3}){3}):(\d{2,5})")

known_addrs = set()
known_ips = set()
for kf in KNOWN_FILES:
    try:
        for rec in json.load(open(kf)):
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
with open(OUT_FILE, "w") as out:
    for scheme, addr in records:
        out.write(f"{scheme} {addr}\n")

print(f"unique candidates: {total}")
print(f"excluded (already working): {dropped_known}")
print(f"scheme breakdown:")
schemes = {}
for s, _ in seen: schemes[s] = schemes.get(s, 0) + 1
for s, c in sorted(schemes.items()): print(f"  {s}: {c}")
