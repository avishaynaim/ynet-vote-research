#!/usr/bin/env python3
"""
Mega Proxy Harvester — continuous fetch → dedupe → probe loop.

Pulls from 80+ public proxy sources (GitHub raw lists + live APIs),
dedupes against the master pool, probes new candidates via asyncio,
and appends working proxies to proxies/master_pool.json.

Designed to be run repeatedly — sources refresh every 10-60 min,
so each run finds new candidates. Checkpoints survive crashes.

Usage:
    python3 scripts/mega_harvest.py                     # one cycle
    python3 scripts/mega_harvest.py --loops 0           # infinite loop
    python3 scripts/mega_harvest.py --loops 5           # 5 cycles
    python3 scripts/mega_harvest.py --concurrency 100   # tune concurrency
    python3 scripts/mega_harvest.py --fetch-only        # just fetch, no probe
"""

import argparse
import asyncio
import json
import os
import random
import re
import signal
import socket
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import aiohttp
from aiohttp_socks import ProxyConnector

# ─── Paths ─────────────────────────────────────────────────────────────────
REPO       = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MASTER     = os.path.join(REPO, "proxies", "master_pool.json")
ALIVE      = os.path.join(REPO, "proxies", "alive.json")
CAND_FILE  = os.path.join(REPO, "scripts", "discovery", "sources", "mega_candidates.txt")
LOG_FILE   = os.path.join(REPO, "scripts", "discovery", "sources", "mega_harvest.log")

# ─── Probe targets ──────────────────────────────────────────────────────────
YNET_URL = "https://www.ynet.co.il/iphone/json/api/talkbacks/list/v2/yokra14737379/0/1"
IPIFY    = "https://api.ipify.org?format=json"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0",
    "Origin":     "https://www.ynet.co.il",
    "Referer":    "https://www.ynet.co.il/news/article/yokra14737379",
}
UA_FETCH = {"User-Agent": "Mozilla/5.0 (compatible; proxy-harvest/3.0)"}


# ═══════════════════════════════════════════════════════════════════════════
#  SOURCE LISTS — static GitHub raw files
# ═══════════════════════════════════════════════════════════════════════════
# (name, scheme_hint, url)
GITHUB_SOURCES = [
    # ── TheSpeedX (updates every 10 min) ──
    ("speedx_http",    "http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("speedx_s4",      "socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("speedx_s5",      "socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    # ── monosans (hourly) ──
    ("monosans_http",  "http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("monosans_s4",    "socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("monosans_s5",    "socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    # ── mmpx12 ──
    ("mmpx12_http",    "http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("mmpx12_s4",      "socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("mmpx12_s5",      "socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    # ── roosterkid ──
    ("roosterkid_http","http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("roosterkid_s4",  "socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("roosterkid_s5",  "socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    # ── jetkai ──
    ("jetkai_http",    "http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("jetkai_s4",      "socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("jetkai_s5",      "socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    # ── ShiftyTR ──
    ("shifty_http",    "http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("shifty_s4",      "socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("shifty_s5",      "socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    # ── clarketm ──
    ("clarketm",       "http",   "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    # ── sunny9577 ──
    ("sunny9577",      "http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    # ── hookzof ──
    ("hookzof_s5",     "socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    # ── proxifly ──
    ("proxifly",       "http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    # ── zloi-user/hideip.me ──
    ("zloi_http",      "http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt"),
    ("zloi_https",     "http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/https.txt"),
    ("zloi_s4",        "socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks4.txt"),
    ("zloi_s5",        "socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt"),
    # ── yakumo ──
    ("yakumo_http",    "http",   "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt"),
    ("yakumo_s4",      "socks4", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt"),
    ("yakumo_s5",      "socks5", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt"),
    # ── Vann-Dev ──
    ("vanndev_http",   "http",   "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt"),
    ("vanndev_s4",     "socks4", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks4.txt"),
    ("vanndev_s5",     "socks5", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks5.txt"),
    # ── prxchk ──
    ("prxchk_http",    "http",   "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("prxchk_s4",      "socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("prxchk_s5",      "socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    # ── yemixzy ──
    ("yemixzy_http",   "http",   "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt"),
    ("yemixzy_s4",     "socks4", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks4.txt"),
    ("yemixzy_s5",     "socks5", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt"),
    # ── MuRongPIG ──
    ("murong_http",    "http",   "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("murong_s4",      "socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("murong_s5",      "socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    # ── ErcinDedeoglu ──
    ("ercin_http",     "http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("ercin_s4",       "socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("ercin_s5",       "socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    # ── KangProxy ──
    ("kang_http",      "http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("kang_s4",        "socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("kang_s5",        "socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),
    # ── B4RC0DE ──
    ("b4_http",        "http",   "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt"),
    ("b4_s4",          "socks4", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt"),
    ("b4_s5",          "socks5", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt"),
    # ── Zaeem20 ──
    ("zaeem_http",     "http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
    ("zaeem_https",    "http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt"),
    ("zaeem_s4",       "socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt"),
    ("zaeem_s5",       "socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),
    # ── anonym0usWork1221 ──
    ("anon_http",      "http",   "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("anon_s4",        "socks4", "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt"),
    ("anon_s5",        "socks5", "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    # ── saschazesiger ──
    ("sascha_http",    "http",   "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/http.txt"),
    ("sascha_s4",      "socks4", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks4.txt"),
    ("sascha_s5",      "socks5", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks5.txt"),
    # ── rdavydov ──
    ("rdavy_http",     "http",   "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("rdavy_s4",       "socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("rdavy_s5",       "socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),
    # ── UserR3X ──
    ("userr3x_http",   "http",   "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/http.txt"),
    ("userr3x_s4",     "socks4", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks4.txt"),
    ("userr3x_s5",     "socks5", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks5.txt"),
    # ── HyperBeats ──
    ("hyper_http",     "http",   "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt"),
    ("hyper_s4",       "socks4", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks4.txt"),
    ("hyper_s5",       "socks5", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks5.txt"),
    # ── ALIILAPRO ──
    ("alii_http",      "http",   "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
    ("alii_s4",        "socks4", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt"),
    ("alii_s5",        "socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),
    # ── r00tee ──
    ("r00t_http",      "http",   "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt"),
    ("r00t_s4",        "socks4", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt"),
    ("r00t_s5",        "socks5", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt"),
    # ── berkay-digital ──
    ("berkay_http",    "http",   "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/http.txt"),
    ("berkay_s4",      "socks4", "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/socks4.txt"),
    ("berkay_s5",      "socks5", "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/socks5.txt"),
    # ── manuGMG ──
    ("manu_http",      "http",   "https://raw.githubusercontent.com/manuGMG/proxy-365/main/HTTP.txt"),
    ("manu_s4",        "socks4", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS4.txt"),
    ("manu_s5",        "socks5", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS5.txt"),
    # ── MishaKorzhik ──
    ("misha_http",     "http",   "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/http.txt"),
    ("misha_s4",       "socks4", "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/socks4.txt"),
    ("misha_s5",       "socks5", "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/socks5.txt"),
    # ── dpangestuw ──
    ("dpang_http",     "http",   "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
    ("dpang_s4",       "socks4", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt"),
    ("dpang_s5",       "socks5", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt"),
    # ── Volodichev ──
    ("volo_http",      "http",   "https://raw.githubusercontent.com/Volodichev/proxy-list/main/http.txt"),
    # ── proxifly by protocol ──
    ("proxifly_http",  "http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("proxifly_s4",    "socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt"),
    ("proxifly_s5",    "socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),
    # ── sunny9577 socks ──
    ("sunny_s4",       "socks4", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt"),
    ("sunny_s5",       "socks5", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"),
    # ── zevtyardt ──
    ("zev_http",       "http",   "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("zev_s4",         "socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("zev_s5",         "socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),
    # ── lalifeier ──
    ("lali_http",      "http",   "https://raw.githubusercontent.com/lalifeier/proxy-list/main/http.txt"),
    ("lali_https",     "http",   "https://raw.githubusercontent.com/lalifeier/proxy-list/main/https.txt"),
    ("lali_s4",        "socks4", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks4.txt"),
    ("lali_s5",        "socks5", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks5.txt"),
    # ── MrMarble ──
    ("mrmarble",       "http",   "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt"),
    # ── a2u ──
    ("a2u",            "http",   "https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.txt"),
    # ── tuanminpay ──
    ("tuan_http",      "http",   "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt"),
    ("tuan_s4",        "socks4", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks4.txt"),
    ("tuan_s5",        "socks5", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt"),
    # ── mertguvencli ──
    ("mert",           "http",   "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt"),
    # ── miroslavpejic85 ──
    ("miro",           "http",   "https://raw.githubusercontent.com/miroslavpejic85/proxy-list/main/proxy-list-raw.txt"),
    # ── proxyspace ──
    ("prxspace_http",  "http",   "https://raw.githubusercontent.com/proxyspace/proxyspace/master/http.txt"),
    ("prxspace_s4",    "socks4", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks4.txt"),
    ("prxspace_s5",    "socks5", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks5.txt"),
    # ── im-razvan ──
    ("razvan_http",    "http",   "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt"),
    ("razvan_s4",      "socks4", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks4.txt"),
    ("razvan_s5",      "socks5", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks5.txt"),
    # ── ObcbO ──
    ("obcbo_http",     "http",   "https://raw.githubusercontent.com/ObcbO/getproxy/master/http.txt"),
    ("obcbo_s4",       "socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/socks4.txt"),
    ("obcbo_s5",       "socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/socks5.txt"),
    # ── ProxyScrape community maintained ──
    ("pxscrape_http",  "http",   "https://raw.githubusercontent.com/proxyscrape/free-proxy-list/master/proxies/http.txt"),
    ("pxscrape_s4",    "socks4", "https://raw.githubusercontent.com/proxyscrape/free-proxy-list/master/proxies/socks4.txt"),
    ("pxscrape_s5",    "socks5", "https://raw.githubusercontent.com/proxyscrape/free-proxy-list/master/proxies/socks5.txt"),
    # ── caliphdev ──
    ("caliph_http",    "http",   "https://raw.githubusercontent.com/caliphdev/Starter-Proxy-Scraper/main/http.txt"),
    ("caliph_s4",      "socks4", "https://raw.githubusercontent.com/caliphdev/Starter-Proxy-Scraper/main/socks4.txt"),
    ("caliph_s5",      "socks5", "https://raw.githubusercontent.com/caliphdev/Starter-Proxy-Scraper/main/socks5.txt"),
    # ── UptimerBot ──
    ("uptimer_http",   "http",   "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt"),
    ("uptimer_s4",     "socks4", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks4.txt"),
    ("uptimer_s5",     "socks5", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks5.txt"),
    # ── almroot ──
    ("almroot_http",   "http",   "https://raw.githubusercontent.com/almroot/proxylist/master/list.txt"),
    # ── RX4096 ──
    ("rx4096_http",    "http",   "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt"),
    ("rx4096_s4",      "socks4", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks4.txt"),
    ("rx4096_s5",      "socks5", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks5.txt"),
    # ── proxy4parsing ──
    ("p4p_http",       "http",   "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt"),
    # ── hendrikbgr ──
    ("hendrik_http",   "http",   "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Finder/master/Proxy%20Finder/working_proxies.txt"),
    # ── aslamy ──
    ("aslamy_http",    "http",   "https://raw.githubusercontent.com/aslamy/Free-Proxy-List/master/Proxies/http.txt"),
]

# ═══════════════════════════════════════════════════════════════════════════
#  API FETCHERS — live services that return fresher proxies than static files
# ═══════════════════════════════════════════════════════════════════════════

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA_FETCH)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def parse_lines(scheme, body):
    """Parse ip:port lines, handling optional scheme:// prefixes."""
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            s, _, addr = line.partition("://")
            s = s.lower()
            if s in ("http", "https"):
                s = "http"
            elif s not in ("socks4", "socks5"):
                s = scheme
            out.append((s, addr.strip()))
        else:
            out.append((scheme, line))
    return out


def fetch_github_source(name, scheme, url):
    try:
        body = http_get(url, timeout=20)
        return parse_lines(scheme, body)
    except Exception:
        return []


def fetch_proxyscrape_api():
    out = []
    for proto in ("http", "socks4", "socks5"):
        for version in ("v2", "v3"):
            try:
                if version == "v3":
                    url = (f"https://api.proxyscrape.com/v3/free-proxy-list/get"
                           f"?request=displayproxies&protocol={proto}"
                           f"&timeout=20000&country=all&proxy_format=ipport&format=text")
                else:
                    url = (f"https://api.proxyscrape.com/v2/"
                           f"?request=getproxies&protocol={proto}"
                           f"&timeout=20000&country=all&ssl=all&anonymity=all")
                body = http_get(url, 30)
                out.extend(parse_lines(proto, body))
            except Exception:
                pass
    return out


def fetch_geonode_api():
    out = []
    try:
        for page in range(1, 31):
            url = (f"https://proxylist.geonode.com/api/proxy-list"
                   f"?limit=500&page={page}&sort_by=lastChecked&sort_type=desc")
            body = http_get(url, 30)
            data = json.loads(body).get("data", [])
            if not data:
                break
            for p in data:
                addr = f"{p.get('ip')}:{p.get('port')}"
                for proto in p.get("protocols", []):
                    s = "http" if proto in ("http", "https") else proto
                    if s in ("http", "socks4", "socks5"):
                        out.append((s, addr))
    except Exception:
        pass
    return out


def fetch_pld_api():
    out = []
    for pq, ps in (("http","http"),("https","http"),("socks4","socks4"),("socks5","socks5")):
        try:
            body = http_get(f"https://www.proxy-list.download/api/v1/get?type={pq}", 20)
            out.extend(parse_lines(ps, body))
        except Exception:
            pass
    return out


def fetch_openproxy_api():
    out = []
    for proto in ("http", "socks4", "socks5"):
        try:
            body = http_get(f"https://api.openproxy.space/lists/{proto}", 30)
            data = json.loads(body)
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and isinstance(entry.get("data"), list):
                        for a in entry["data"]:
                            a = str(a).strip()
                            if ":" in a:
                                out.append((proto, a))
        except Exception:
            pass
    return out


def fetch_freeproxylist_scrape():
    out = []
    urls = [
        ("http",   "https://free-proxy-list.net/"),
        ("http",   "https://www.sslproxies.org/"),
        ("http",   "https://www.us-proxy.org/"),
        ("http",   "https://free-proxy-list.net/anonymous-proxy.html"),
        ("socks5", "https://www.socks-proxy.net/"),
    ]
    for scheme, url in urls:
        try:
            body = http_get(url, 20)
            pairs = re.findall(
                r"<td>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td>\s*(\d{2,5})\s*</td>",
                body)
            for ip, port in pairs:
                out.append((scheme, f"{ip}:{port}"))
        except Exception:
            pass
    return out


def fetch_spys_scrape():
    out = []
    try:
        body = http_get("https://spys.me/proxy.txt", 20)
        for line in body.splitlines():
            m = re.match(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", line.strip())
            if m:
                out.append(("http", m.group(1)))
    except Exception:
        pass
    try:
        body = http_get("https://spys.me/socks.txt", 20)
        for line in body.splitlines():
            m = re.match(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", line.strip())
            if m:
                out.append(("socks5", m.group(1)))
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  GATHER ALL CANDIDATES
# ═══════════════════════════════════════════════════════════════════════════

def gather_all():
    """Fetch from all sources in parallel, return list of (scheme, addr)."""
    all_candidates = []
    errors = 0

    with ThreadPoolExecutor(max_workers=25) as ex:
        # GitHub sources
        gh_futs = {
            ex.submit(fetch_github_source, name, scheme, url): name
            for name, scheme, url in GITHUB_SOURCES
        }
        # API sources
        api_futs = {
            ex.submit(fetch_proxyscrape_api): "proxyscrape",
            ex.submit(fetch_geonode_api):     "geonode",
            ex.submit(fetch_pld_api):         "proxy-list.download",
            ex.submit(fetch_openproxy_api):   "openproxy.space",
            ex.submit(fetch_freeproxylist_scrape): "freeproxylist.net",
            ex.submit(fetch_spys_scrape):     "spys.me",
        }

        for fut in as_completed(gh_futs):
            name = gh_futs[fut]
            try:
                result = fut.result()
                all_candidates.extend(result)
                if result:
                    log(f"  [OK]  {name:<22} {len(result):>6}")
            except Exception:
                errors += 1

        for fut in as_completed(api_futs):
            name = api_futs[fut]
            try:
                result = fut.result()
                all_candidates.extend(result)
                log(f"  [OK]  {name:<22} {len(result):>6}")
            except Exception:
                errors += 1
                log(f"  [ERR] {name}")

    # Validate format — must be ip:port
    valid = []
    for scheme, addr in all_candidates:
        addr = addr.strip()
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$", addr):
            continue
        valid.append((scheme, addr))

    raw = len(valid)
    # Dedupe
    valid = list(dict.fromkeys(valid))
    log(f"\n  Raw: {raw}  Dedup: {len(valid)}  Errors: {errors}")
    return valid


# ═══════════════════════════════════════════════════════════════════════════
#  MASTER POOL MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def load_master():
    if os.path.exists(MASTER):
        try:
            return json.load(open(MASTER))
        except Exception:
            return []
    return []


def save_master(proxies):
    tmp = MASTER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(proxies, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MASTER)


def known_sets(master):
    """Return (known_addrs, known_ips) from master pool."""
    addrs = {p["addr"] for p in master if p.get("addr")}
    ips = {p["exit_ip"] for p in master if p.get("exit_ip")}
    return addrs, ips


# ═══════════════════════════════════════════════════════════════════════════
#  ASYNC PROBER
# ═══════════════════════════════════════════════════════════════════════════

async def probe_one(scheme, addr, ynet_timeout, ip_timeout):
    """Test a single proxy: can it reach ynet? What's its exit IP?"""
    url = f"{scheme}://{addr}"
    try:
        conn = ProxyConnector.from_url(url, rdns=True)
    except Exception:
        return None

    timeout = aiohttp.ClientTimeout(total=ynet_timeout)
    try:
        async with aiohttp.ClientSession(
            connector=conn, timeout=timeout, trust_env=False
        ) as session:
            t0 = time.time()
            async with session.get(YNET_URL, headers=HEADERS) as r:
                if r.status != 200:
                    return None
                body = await r.content.read(1200)
                if not any(t in body for t in (b"rss", b"talkback", b"success", b"<?xml")):
                    return None
                dt = int((time.time() - t0) * 1000)

            # Best-effort exit IP lookup
            ip = None
            try:
                async with session.get(
                    IPIFY, timeout=aiohttp.ClientTimeout(total=ip_timeout)
                ) as r:
                    if r.status == 200:
                        ip = json.loads(await r.text()).get("ip")
            except Exception:
                pass

            return {"scheme": scheme, "addr": addr, "exit_ip": ip, "ynet_ms": dt}
    except Exception:
        return None


STOP = False

async def probe_candidates(candidates, concurrency, ynet_timeout, ip_timeout,
                           known_addrs, known_ips, master):
    """Probe all candidates, appending hits to master in place."""
    global STOP
    sem = asyncio.Semaphore(concurrency)
    hits = 0
    tried = 0
    t0 = time.time()
    total = len(candidates)

    # Local dedup sets (copy from known)
    seen_addrs = set(known_addrs)
    seen_ips = set(known_ips)

    async def _probe(scheme, addr):
        nonlocal hits, tried
        if STOP:
            return
        async with sem:
            if STOP:
                return
            tried += 1
            rec = await probe_one(scheme, addr, ynet_timeout, ip_timeout)

        if not rec:
            return

        ip = rec["exit_ip"]
        if addr in seen_addrs:
            return
        if ip and ip in seen_ips:
            return

        seen_addrs.add(addr)
        if ip:
            seen_ips.add(ip)

        master.append(rec)
        hits += 1
        log(f"  HIT #{len(master):>5}  {scheme:6s} {addr:22s}  "
            f"exit={ip or '?':16s}  ynet={rec['ynet_ms']}ms")

        # Checkpoint every 20 hits
        if hits % 20 == 0:
            save_master(master)
            log(f"  checkpoint: {len(master)} total in master")

    # Create tasks in batches to avoid memory bloat
    batch_size = concurrency * 4
    for i in range(0, total, batch_size):
        if STOP:
            break
        batch = candidates[i:i + batch_size]
        tasks = [asyncio.create_task(_probe(s, a)) for s, a in batch]
        await asyncio.gather(*tasks, return_exceptions=True)

        if (i + batch_size) % 2000 < batch_size:
            dt = time.time() - t0
            rate = tried / max(dt, 0.1)
            log(f"  progress: tried={tried}/{total}  hits={hits}  "
                f"master={len(master)}  rate={rate:.0f}/s  "
                f"elapsed={dt:.0f}s")

    # Final save
    save_master(master)
    dt = time.time() - t0
    log(f"  DONE: tried={tried}  new_hits={hits}  master={len(master)}  "
        f"elapsed={dt:.0f}s")
    return hits


# ═══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global STOP

    ap = argparse.ArgumentParser(description="Mega Proxy Harvester")
    ap.add_argument("--loops", type=int, default=1,
                    help="Number of fetch→probe cycles (0 = infinite)")
    ap.add_argument("--concurrency", type=int, default=120,
                    help="Async probe concurrency (default 120)")
    ap.add_argument("--timeout", type=float, default=8.0,
                    help="Ynet probe timeout in seconds")
    ap.add_argument("--ip-timeout", type=float, default=5.0,
                    help="ipify lookup timeout")
    ap.add_argument("--pause", type=int, default=300,
                    help="Seconds between cycles (default 300 = 5 min)")
    ap.add_argument("--fetch-only", action="store_true",
                    help="Just fetch and count candidates, don't probe")
    ap.add_argument("--target", type=int, default=10000,
                    help="Stop when master pool reaches this size")
    args = ap.parse_args()

    def sighandler(*_):
        global STOP
        log("SIGNAL received — finishing current batch then stopping")
        STOP = True
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    loop_n = 0
    while True:
        loop_n += 1
        if args.loops > 0 and loop_n > args.loops:
            break
        if STOP:
            break

        log(f"\n{'='*70}")
        log(f" CYCLE {loop_n}  (target: {args.target})")
        log(f"{'='*70}")

        # Load master
        master = load_master()
        known_addrs, known_ips = known_sets(master)
        log(f"Master pool: {len(master)} proxies, {len(known_ips)} unique exit IPs")

        if len(master) >= args.target:
            log(f"TARGET REACHED: {len(master)} >= {args.target}")
            break

        # Fetch
        log("\nFetching sources...")
        candidates = gather_all()

        # Exclude known
        fresh = [(s, a) for s, a in candidates if a not in known_addrs]
        log(f"Fresh candidates (not in master): {len(fresh)}")

        if not fresh:
            log("No new candidates — waiting for source refresh...")
            if args.loops == 1:
                break
            time.sleep(args.pause)
            continue

        # Shuffle for fairness
        random.shuffle(fresh)

        if args.fetch_only:
            log(f"--fetch-only: {len(fresh)} candidates ready for probing")
            break

        # Probe
        log(f"\nProbing {len(fresh)} candidates (concurrency={args.concurrency})...")
        new_hits = asyncio.run(
            probe_candidates(
                fresh, args.concurrency, args.timeout, args.ip_timeout,
                known_addrs, known_ips, master
            )
        )

        master = load_master()  # re-read after probe saved
        log(f"\nCycle {loop_n} complete: +{new_hits} new, {len(master)} total")

        if len(master) >= args.target:
            log(f"TARGET REACHED: {len(master)} >= {args.target}")
            break

        if STOP:
            break

        if args.loops != 1:
            log(f"Sleeping {args.pause}s before next cycle...")
            for _ in range(args.pause):
                if STOP:
                    break
                time.sleep(1)

    # Final stats
    master = load_master()
    ips = {p["exit_ip"] for p in master if p.get("exit_ip")}
    no_ip = sum(1 for p in master if not p.get("exit_ip"))
    log(f"\n{'='*70}")
    log(f" FINAL: {len(master)} proxies | {len(ips)} unique exit IPs | {no_ip} unknown IP")
    log(f" Saved: {MASTER}")
    log(f"{'='*70}")


if __name__ == "__main__":
    main()
