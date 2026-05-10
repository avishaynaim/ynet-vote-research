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
import glob
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import source_manager as _sm

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
    # ── TheSpeedX PROXY-List (updates every 10 min) ──
    ("speedx_http",    "http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("speedx_s4",      "socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("speedx_s5",      "socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    # ── TheSpeedX SOCKS-List (separate repo, different IP pool ~8k) ──
    ("speedx_sockslist_http", "http",   "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("speedx_sockslist_s4",   "socks4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("speedx_sockslist_s5",   "socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
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
    # ── ObcbO (files live under file/ subdirectory) ──
    ("obcbo_http",     "http",   "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt"),
    ("obcbo_s4",       "socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks4.txt"),
    ("obcbo_s5",       "socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt"),
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
    ("p4p_hproxy",     "http",   "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/hproxy.txt"),
    # ── hendrikbgr ──
    ("hendrik_http",   "http",   "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Finder/master/Proxy%20Finder/working_proxies.txt"),
    ("hendrik2_http",  "http",   "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt"),
    # ── aslamy ──
    ("aslamy_http",    "http",   "https://raw.githubusercontent.com/aslamy/Free-Proxy-List/master/Proxies/http.txt"),
    # ── casals-ar (huge lists, updated frequently) ──
    ("casals_http",    "http",   "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http"),
    ("casals_s5",      "socks5", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5"),
    ("casals_s4",      "socks4", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks4"),
    # ── themiralay ──
    ("miralay",        "http",   "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt"),
    # ── vakhov fresh list ──
    ("vakhov_http",    "http",   "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
    ("vakhov_s5",      "socks5", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),
    # ── saisuiu Chinese proxies ──
    ("saisuiu_cn",     "http",   "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/cnfree.txt"),
    # ── saisuiu free list (broader pool, different from cnfree.txt) ──
    ("saisuiu_free",   "http",   "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt"),
    # ── mmpx12 https (separate pool from http.txt) ──
    ("mmpx12_https",   "http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
    # ── proxylist-to (tested 749 http + 195 s5 + 260 s4) ──
    ("proxylto_http",  "http",   "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/http.txt"),
    ("proxylto_s5",    "socks5", "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks5.txt"),
    ("proxylto_s4",    "socks4", "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks4.txt"),
    # ── roma8ok (931 http + 134 socks5 verified in cycle 9) ──
    ("roma8ok_http",   "http",   "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-http.txt"),
    ("roma8ok_s5",     "socks5", "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-socks5.txt"),
    # ── sunny9577 combined proxies.txt (all protocols, 1513 IPs, overlaps generated/) ──
    ("sunny_all",      "http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt"),
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
    # All-protocol endpoint — single call returns 7k+ proxies with scheme:// prefix
    # This uses proxy_format=protocolipport so each line is e.g. socks5://1.2.3.4:1080
    try:
        url = ("https://api.proxyscrape.com/v3/free-proxy-list/get"
               "?request=displayproxies&protocol=all"
               "&timeout=10000&country=all&proxy_format=protocolipport&format=text")
        body = http_get(url, 45)
        out.extend(parse_lines("http", body))  # parse_lines handles scheme:// prefixes
    except Exception:
        pass
    for proto in ("http", "socks4", "socks5"):
        # Standard bulk fetch (all proxies, broad timeout)
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
        # High-quality subset: fast (timeout≤1000ms) + elite anonymity only
        # These are less likely to be detected/blocked by Ynet
        try:
            url = (f"https://api.proxyscrape.com/v2/"
                   f"?request=getproxies&protocol={proto}"
                   f"&timeout=1000&country=all&ssl=all&anonymity=elite")
            body = http_get(url, 15)
            out.extend(parse_lines(proto, body))
        except Exception:
            pass
        # Elite proxies with 5000ms timeout — broader pool than 1000ms tier
        # socks5 returns ~2000+ elite IPs vs ~few hundred at 1000ms
        try:
            url = (f"https://api.proxyscrape.com/v2/"
                   f"?request=getproxies&protocol={proto}"
                   f"&timeout=5000&country=all&ssl=all&anonymity=elite")
            body = http_get(url, 20)
            out.extend(parse_lines(proto, body))
        except Exception:
            pass
    return out


def fetch_proxyscrape_country(sm=None):
    """Fetch proxies from top-yield countries via proxyscrape API.

    Countries chosen for highest free-proxy volume: CN, RU, VN, BR, IN.
    Fetches http + socks5 per country (10 parallel calls) to find IPs not in
    the global all-country results (country pools often differ from global dumps).

    Returns dict of {source_key: [(scheme, addr), ...]} for per-key harvest tracking.
    """
    TOP_COUNTRIES = ["CN", "RU", "VN", "BR", "IN"]
    tasks = []
    for cc in TOP_COUNTRIES:
        for proto in ("http", "socks5"):
            url = (f"https://api.proxyscrape.com/v2/"
                   f"?request=getproxies&protocol={proto}"
                   f"&country={cc}&timeout=10000&ssl=all&anonymity=all")
            src_key = f"proxyscrape_{cc}_{proto[:4]}"
            tasks.append((src_key, proto, url))

    # Register each per-country-protocol source
    if sm:
        for src_key, proto, url in tasks:
            sm.ensure_source(src_key, url=url, scheme=proto, category="api")

    per_key: dict = {}
    def _fetch(task):
        src_key, proto, url = task
        try:
            body = http_get(url, 20)
            return src_key, parse_lines(proto, body)
        except Exception:
            return src_key, []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_fetch, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                src_key, result = fut.result()
                per_key[src_key] = per_key.get(src_key, []) + result
            except Exception:
                pass

    # Record harvest counts per key
    if sm:
        for src_key, proxies in per_key.items():
            if proxies:
                sm.record_harvest(src_key, len(proxies))

    return per_key


def fetch_geoxy():
    """geoxy.io — elite-only verified proxies (1000+ IPs, avg ping metadata).
    API token sourced from floppydata.com/free-proxy/ page JS."""
    try:
        req = urllib.request.Request(
            "https://geoxy.io/proxies?count=99999",
            headers={
                "Authorization": "BgPXfhUc8CAhK7wGOqzqz9m77j3sH7",
                "Content-Type":  "application/json",
                "User-Agent":    "Mozilla/5.0",
            })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        out = []
        for p in data:
            addr = p.get("address", "")
            if not addr or ":" not in addr:
                continue
            for proto in p.get("protocols", ["http"]):
                scheme = proto.lower()
                if scheme in ("socks4", "socks5", "http", "https"):
                    out.append((scheme if scheme != "https" else "http", addr))
        return out
    except Exception:
        return []


def fetch_proxyspace_direct():
    """proxyspace.pro direct URLs — different/fresher content than GitHub mirror."""
    out = []
    for scheme, path in [("http", "http.txt"), ("socks4", "socks4.txt"), ("socks5", "socks5.txt")]:
        try:
            body = http_get(f"https://proxyspace.pro/{path}", 20)
            out.extend(parse_lines(scheme, body))
        except Exception:
            pass
    return out


def fetch_geonode_api():
    out = []
    GEONODE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

    def _geonode_page(url):
        """Fetch a geonode API page; on 403 or empty, retry once with explicit UA."""
        try:
            return http_get(url, 30)
        except Exception as e:
            err_str = str(e)
            # Retry with explicit User-Agent on 403 or connection errors
            if "403" in err_str or "Forbidden" in err_str or not err_str:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": GEONODE_UA,
                                                                "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return r.read().decode(errors="replace")
                except Exception:
                    pass
            return None

    try:
        for page in range(1, 31):
            url = (f"https://proxylist.geonode.com/api/proxy-list"
                   f"?limit=500&page={page}&sort_by=lastChecked&sort_type=desc"
                   f"&anonymityLevel=elite%2Canonymous")
            body = _geonode_page(url)
            if not body:
                break
            try:
                data = json.loads(body).get("data", [])
            except Exception:
                break
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
        # /en/ variant — 300 proxies per page, different pool
        ("http",   "https://free-proxy-list.net/en/"),
        ("http",   "https://free-proxy-list.net/en/?page=2"),
        ("http",   "https://free-proxy-list.net/en/?page=3"),
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


def fetch_checkerproxy():
    """
    checkerproxy.net daily archive — the highest-value free source because
    proxies here were actually verified within the last 24-48 h by an
    independent prober, not just scraped and published.  Type field:
      1=HTTP  2=HTTPS  3=SOCKS4  4=SOCKS5
    We pull today + yesterday in case today's archive is still building.
    """
    TYPE_MAP = {1: "http", 2: "http", 3: "socks4", 4: "socks5"}
    import datetime
    out = []
    for delta in range(3):
        try:
            d = (datetime.date.today() - datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
            body = http_get(f"https://checkerproxy.net/api/archive/{d}", 30)
            items = json.loads(body)
            for item in items:
                addr   = (item.get("addr") or "").strip()
                scheme = TYPE_MAP.get(item.get("type", 1), "http")
                if addr:
                    out.append((scheme, addr))
            if out:
                break   # got results — no need to go further back
        except Exception:
            pass
    return out


def fetch_proxydb():
    """
    proxydb.net — scrape proxy entries using multiple extraction strategies.
    Strategy 1: href="/IP/PORT#protocol" anchor links.
    Strategy 2: separate IP and port table cells extracted via regex.
    Strategy 3: plain IP:PORT pattern anywhere in the page.
    Fetches elite+anonymous HTTP and SOCKS5 pages (offsets 0, 15, 30).
    """
    out = []
    SCHEME_MAP = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5"}
    # Strategy 1: href anchor links (original page format)
    LINK_RE   = re.compile(r'href="/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{2,5})#(https?|socks4|socks5)"', re.I)
    # Strategy 2: adjacent IP and port cells in table rows
    IP_RE     = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
    PORT_RE   = re.compile(r"\b(\d{2,5})\b")
    # Strategy 3: bare IP:PORT anywhere
    BARE_RE   = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})")
    seen = set()

    def _extract(body, scheme):
        # S1: anchor links with protocol hint
        for ip, port, ptype in LINK_RE.findall(body):
            addr = f"{ip}:{port}"
            if addr not in seen:
                seen.add(addr)
                out.append((SCHEME_MAP.get(ptype.lower(), scheme), addr))
        # S2: table cells — look for <td> containing only an IP, next <td> only a port
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.DOTALL | re.I)
        for row in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.I)
            tds_clean = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
            for i in range(len(tds_clean) - 1):
                ip_m   = IP_RE.fullmatch(tds_clean[i])
                port_m = PORT_RE.fullmatch(tds_clean[i + 1]) if i + 1 < len(tds_clean) else None
                if ip_m and port_m:
                    addr = f"{ip_m.group(1)}:{port_m.group(1)}"
                    if addr not in seen:
                        seen.add(addr)
                        out.append((scheme, addr))
        # S3: fallback bare IP:PORT scan
        for ip, port in BARE_RE.findall(body):
            addr = f"{ip}:{port}"
            if addr not in seen:
                seen.add(addr)
                out.append((scheme, addr))

    # Fetch anonymous+elite HTTP, SOCKS5, SOCKS4 — highest bypass potential
    for proto, scheme in (("http&anonimity=elite,anonymous", "http"),
                          ("socks5", "socks5"),
                          ("socks4&anonimity=elite,anonymous", "socks4")):
        for offset in (0, 15, 30):
            try:
                url = f"https://proxydb.net/?protocol={proto}&offset={offset}"
                body = http_get(url, 15)
                _extract(body, scheme)
            except Exception:
                pass
    return out


def fetch_fate0():
    """fate0/proxylist — JSON objects one per line, each with host/port/type fields.
    Format: {"host":"ip","port":N,"type":"http|socks5"} — updated daily."""
    TYPE_MAP = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5"}
    try:
        body = http_get(
            "https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list", 30)
        out = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                host = obj.get("host", "").strip()
                port = obj.get("port")
                ptype = str(obj.get("type", "http")).lower()
                scheme = TYPE_MAP.get(ptype, "http")
                if host and port and ":" not in host:
                    out.append((scheme, f"{host}:{port}"))
            except Exception:
                pass
        return out
    except Exception:
        return []


def fetch_proxyscan():
    """
    proxyscan.io API — returns recently-tested proxies in plain JSON.
    Free tier, no key required, returns up to 100 per request.
    """
    out = []
    SCHEME_MAP = {"HTTP": "http", "HTTPS": "http", "SOCKS4": "socks4", "SOCKS5": "socks5"}
    for proto in ("http", "socks4", "socks5"):
        try:
            body = http_get(
                f"https://www.proxyscan.io/api/proxy?format=json&type={proto}&limit=100&ping=500&uptime=50",
                20)
            items = json.loads(body)
            for p in items:
                ip   = p.get("Ip", "")
                port = p.get("Port", "")
                ptype = p.get("Type", ["HTTP"])[0] if isinstance(p.get("Type"), list) else p.get("Type", "HTTP")
                scheme = SCHEME_MAP.get(str(ptype).upper(), "http")
                if ip and port:
                    out.append((scheme, f"{ip}:{port}"))
        except Exception:
            pass
    return out


def fetch_freeproxyworld():
    """
    freeproxy.world — IP and Port are in separate <td> cells, so we parse
    adjacent table cells rather than looking for IP:PORT inline.
    Scrapes pages 1-5 of each protocol type.
    """
    out = []
    IP_RE   = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
    PORT_RE = re.compile(r"(\d{2,5})")
    TD_RE   = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
    seen = set()
    for scheme, ptype in [("http", "http"), ("socks5", "socks5"), ("socks4", "socks4")]:
        for page in range(1, 6):
            try:
                body = http_get(
                    f"https://freeproxy.world/?type={ptype}&page={page}", 15)
                rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.DOTALL)
                for row in rows:
                    cols = TD_RE.findall(row)
                    if len(cols) < 2:
                        continue
                    ip_m   = IP_RE.search(re.sub(r"<[^>]+>", "", cols[0]))
                    port_m = PORT_RE.search(re.sub(r"<[^>]+>", "", cols[1]))
                    if ip_m and port_m:
                        addr = f"{ip_m.group(1)}:{port_m.group(1)}"
                        if addr not in seen:
                            seen.add(addr)
                            out.append((scheme, addr))
            except Exception:
                pass
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  GATHER ALL CANDIDATES
# ═══════════════════════════════════════════════════════════════════════════

def gather_all(sm=None):
    """Fetch from all sources in parallel, return list of (source_key, scheme, addr)."""
    all_candidates = []   # [(source_key, scheme, addr), ...]
    per_source: dict[str, int] = {}
    errors = 0

    # Register all known sources in the registry
    if sm:
        for name, scheme, url in GITHUB_SOURCES:
            sm.ensure_source(name, url=url, scheme=scheme, category="github")
        for api_name, url in [
            ("proxyscrape",              "https://api.proxyscrape.com"),
            ("proxyscrape_elite5k_sock", "https://api.proxyscrape.com/v2/?protocol=socks5&timeout=5000&anonymity=elite"),
            ("proxyscrape_elite5k_s4",   "https://api.proxyscrape.com/v2/?protocol=socks4&timeout=5000&anonymity=elite"),
            ("proxyscrape_elite5k_http", "https://api.proxyscrape.com/v2/?protocol=http&timeout=5000&anonymity=elite"),
            ("geonode",                  "https://proxylist.geonode.com"),
            ("proxy-list.download",      "https://www.proxy-list.download"),
            ("openproxy.space",          "https://api.openproxy.space"),
            ("freeproxylist.net",        "https://free-proxy-list.net"),
            ("spys.me",                  "https://spys.me"),
            ("checkerproxy.net",         "https://checkerproxy.net"),
            ("proxydb.net",              "https://proxydb.net"),
            ("proxyscan.io",             "https://www.proxyscan.io"),
            ("freeproxy.world",          "https://freeproxy.world"),
            ("proxyspace.pro",           "https://proxyspace.pro"),
            ("geoxy.io",                 "https://geoxy.io"),
            ("fate0",                    "https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list"),
        ]:
            sm.ensure_source(api_name, url=url, scheme="mixed", category="api")

    # Per-country proxyscrape keys are registered inside fetch_proxyscrape_country()
    _country_fut_key = "_proxyscrape_country_"

    with ThreadPoolExecutor(max_workers=25) as ex:
        # GitHub sources
        gh_futs = {
            ex.submit(fetch_github_source, name, scheme, url): name
            for name, scheme, url in GITHUB_SOURCES
        }
        # API sources — note fetch_proxyscrape_country returns a dict, handled separately
        api_futs = {
            ex.submit(fetch_proxyscrape_api):                "proxyscrape",
            ex.submit(fetch_geonode_api):                    "geonode",
            ex.submit(fetch_pld_api):                        "proxy-list.download",
            ex.submit(fetch_openproxy_api):                  "openproxy.space",
            ex.submit(fetch_freeproxylist_scrape):           "freeproxylist.net",
            ex.submit(fetch_spys_scrape):                    "spys.me",
            ex.submit(fetch_checkerproxy):                   "checkerproxy.net",
            ex.submit(fetch_proxydb):                        "proxydb.net",
            ex.submit(fetch_proxyscan):                      "proxyscan.io",
            ex.submit(fetch_freeproxyworld):                 "freeproxy.world",
            ex.submit(fetch_proxyspace_direct):              "proxyspace.pro",
            ex.submit(fetch_geoxy):                          "geoxy.io",
            ex.submit(fetch_fate0):                          "fate0",
            ex.submit(fetch_proxyscrape_country, sm): _country_fut_key,
        }

        for fut in as_completed(gh_futs):
            name = gh_futs[fut]
            try:
                result = fut.result()
                for scheme, addr in result:
                    all_candidates.append((name, scheme, addr))
                per_source[name] = len(result)
                if result:
                    log(f"  [OK]  {name:<30} {len(result):>6}")
            except Exception:
                errors += 1

        for fut in as_completed(api_futs):
            name = api_futs[fut]
            try:
                result = fut.result()
                if name == _country_fut_key:
                    # result is a dict: {source_key: [(scheme, addr), ...]}
                    total_country = 0
                    for src_key, proxies in result.items():
                        for scheme, addr in proxies:
                            all_candidates.append((src_key, scheme, addr))
                        if proxies:
                            per_source[src_key] = len(proxies)
                            total_country += len(proxies)
                            log(f"  [OK]  {src_key:<30} {len(proxies):>6}")
                else:
                    for scheme, addr in result:
                        all_candidates.append((name, scheme, addr))
                    per_source[name] = len(result)
                    log(f"  [OK]  {name:<30} {len(result):>6}")
            except Exception:
                errors += 1
                log(f"  [ERR] {name}")

    # Fetch from registry-discovered sources not in the hardcoded lists
    # Per-country proxyscrape keys follow pattern proxyscrape_CC_proto
    _country_keys = {
        f"proxyscrape_{cc}_{proto[:4]}"
        for cc in ["CN", "RU", "VN", "BR", "IN"]
        for proto in ("http", "socks5")
    }
    if sm:
        hardcoded_keys = {name for name, _, _ in GITHUB_SOURCES} | _country_keys | {
            "proxyscrape", "geonode", "proxy-list.download",
            "openproxy.space", "freeproxylist.net", "spys.me", "checkerproxy.net",
            "proxydb.net", "proxyscan.io", "freeproxy.world", "proxyspace.pro",
            "geoxy.io", "fate0",
        }
        discovered = [
            (key, info["url"], info.get("scheme", "http"))
            for key, info in sm.ranked_sources()
            if key not in hardcoded_keys and info.get("url") and info.get("score", 0) >= 0
        ]
        if discovered:
            log(f"  Fetching {len(discovered)} registry-discovered sources...")
            with ThreadPoolExecutor(max_workers=min(20, len(discovered))) as disc_ex:
                disc_futs2 = {
                    disc_ex.submit(fetch_github_source, key, scheme, url): key
                    for key, url, scheme in discovered
                }
                for fut in as_completed(disc_futs2):
                    key = disc_futs2[fut]
                    try:
                        result = fut.result()
                        if result:
                            for scheme, addr in result:
                                all_candidates.append((key, scheme, addr))
                            per_source[key] = len(result)
                            log(f"  [OK]  {key:<30} {len(result):>6}  (discovered)")
                    except Exception:
                        pass

    # Update harvest counts in registry
    if sm:
        for key, count in per_source.items():
            if count > 0:
                sm.record_harvest(key, count)

    # Validate format — must be ip:port
    valid = []
    seen_addrs: set[str] = set()
    for source_key, scheme, addr in all_candidates:
        addr = addr.strip()
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$", addr):
            continue
        if addr in seen_addrs:
            continue
        seen_addrs.add(addr)
        valid.append((source_key, scheme, addr))

    log(f"\n  Raw: {len(all_candidates)}  Dedup: {len(valid)}  Errors: {errors}")
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
    # Sort: socks5 first (best tunneling), then socks4, then http/https.
    # Within each scheme group, sort by ynet_ms ascending (fastest first).
    _SCHEME_RANK = {"socks5": 0, "socks4": 1, "http": 2, "https": 2}
    sorted_proxies = sorted(
        proxies,
        key=lambda x: (_SCHEME_RANK.get(x.get("scheme", "http"), 2), x.get("ynet_ms", 99999))
    )
    tmp = MASTER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted_proxies, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MASTER)


def known_sets(master):
    """Return (known_addrs, known_ips) from ALL pool_*.json files on disk.

    Reads every pool_*.json in the proxies/ directory so that machines don't
    re-probe addresses already discovered by other machines after a git pull.
    The local master pool is included automatically since it's a pool_*.json file.
    """
    addrs = {p["addr"] for p in master if p.get("addr")}
    ips   = {p["exit_ip"] for p in master if p.get("exit_ip")}

    proxies_dir = os.path.join(REPO, "proxies")
    for fpath in glob.glob(os.path.join(proxies_dir, "pool_*.json")):
        if os.path.abspath(fpath) == os.path.abspath(MASTER):
            continue  # already counted above
        try:
            for p in json.load(open(fpath)):
                if p.get("addr"):
                    addrs.add(p["addr"])
                if p.get("exit_ip"):
                    ips.add(p["exit_ip"])
        except Exception:
            pass

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
    """Probe all candidates, appending hits to master in place.

    candidates: list of (source_key, scheme, addr)
    """
    global STOP
    sem = asyncio.Semaphore(concurrency)
    hits = 0
    tried = 0
    t0 = time.time()
    total = len(candidates)

    seen_addrs = set(known_addrs)
    seen_ips   = set(known_ips)

    async def _probe(source_key, scheme, addr):
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

        rec["source"] = source_key   # tag with origin source
        master.append(rec)
        hits += 1
        log(f"  HIT #{len(master):>5}  {scheme:6s} {addr:22s}  "
            f"exit={ip or '?':16s}  ynet={rec['ynet_ms']}ms  src={source_key}")

        # Record that this source produced a proxy that actually reached YNET
        # This builds the hit_rate quality signal in source_registry.json
        try:
            _sm.get().record_probe_hit(source_key)
        except Exception:
            pass

        if hits % 20 == 0:
            save_master(master)
            log(f"  checkpoint: {len(master)} total in master")

    batch_size = concurrency * 4
    for i in range(0, total, batch_size):
        if STOP:
            break
        batch = candidates[i:i + batch_size]
        tasks = [asyncio.create_task(_probe(sk, s, a)) for sk, s, a in batch]
        await asyncio.gather(*tasks, return_exceptions=True)

        if (i + batch_size) % 2000 < batch_size:
            dt = time.time() - t0
            rate = tried / max(dt, 0.1)
            log(f"  progress: tried={tried}/{total}  hits={hits}  "
                f"master={len(master)}  rate={rate:.0f}/s  "
                f"elapsed={dt:.0f}s")

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

def _run_sync():
    sync_script = os.path.join(os.path.dirname(REPO), "sync_pools.py")
    if not os.path.exists(sync_script):
        sync_script = os.path.join(REPO, "..", "sync_pools.py")
    sync_script = os.path.normpath(sync_script)
    if not os.path.exists(sync_script):
        log("sync_pools.py not found — skipping sync")
        return
    log("── sync start ──")
    import subprocess
    r = subprocess.run([sys.executable, sync_script], cwd=os.path.dirname(sync_script))
    log(f"── sync done (exit {r.returncode}) ──")


def main():
    global STOP

    ap = argparse.ArgumentParser(description="Mega Proxy Harvester")
    ap.add_argument("--loops", type=int, default=1,
                    help="Number of fetch→probe cycles (0 = infinite)")
    ap.add_argument("--concurrency", type=int, default=200,
                    help="Async probe concurrency (default 200)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="Ynet probe timeout in seconds")
    ap.add_argument("--ip-timeout", type=float, default=5.0,
                    help="ipify lookup timeout")
    ap.add_argument("--pause", type=int, default=300,
                    help="Seconds between cycles (default 300 = 5 min)")
    ap.add_argument("--fetch-only", action="store_true",
                    help="Just fetch and count candidates, don't probe")
    ap.add_argument("--target", type=int, default=10000,
                    help="Stop when master pool reaches this size")
    _hostname = socket.gethostname().replace(" ", "_").replace("/", "_")
    _default_output = os.path.join(REPO, "proxies", f"pool_{_hostname}.json")
    ap.add_argument("--output", default=_default_output,
                    help=f"Pool file for this machine (default: pool_<hostname>.json = {_default_output!r})")
    ap.add_argument("--sync", action="store_true",
                    help="Run sync_pools.py (git pull+merge+push) after each cycle")
    args = ap.parse_args()

    global MASTER
    MASTER = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(MASTER), exist_ok=True)
    log(f"Output file: {MASTER}")

    def sighandler(*_):
        global STOP
        log("SIGNAL received — finishing current batch then stopping")
        STOP = True
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    # Source manager — tracks per-source scores across all runs
    sm = _sm.SourceManager()
    log(f"Source registry: {sm.stats()}")

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

        # Reset bad sources that have been idle long enough for a second chance
        reset_n = sm.maybe_reset_bad_sources()
        if reset_n:
            log(f"  Reset {reset_n} bad sources — they get a fresh chance")

        # Load master
        master = load_master()
        known_addrs, known_ips = known_sets(master)
        log(f"Own pool: {len(master)} proxies | known across all machines: {len(known_addrs)} addrs")

        if args.target > 0 and len(master) >= args.target:
            log(f"TARGET REACHED: {len(master)} >= {args.target}")
            break

        # Fetch — pass sm so sources are registered and harvest counts recorded
        log("\nFetching sources...")
        candidates = gather_all(sm=sm)

        # Exclude known
        fresh = [(sk, s, a) for sk, s, a in candidates if a not in known_addrs]
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

        if args.sync and new_hits > 0:
            _run_sync()

        if args.target > 0 and len(master) >= args.target:
            log(f"TARGET REACHED: {len(master)} >= {args.target}")
            if args.sync:
                _run_sync()
            break

        if STOP:
            if args.sync:
                _run_sync()
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
