#!/usr/bin/env python3
"""
Proxy Keeper — background daemon that keeps alive.json fresh.

Each cycle:
  1. Fetch candidates from GitHub/API sources
  2. Re-probe a random sample of existing master_pool entries
  3. Probe all candidates (asyncio, concurrency=300 — coroutines not threads)
  4. Write hits to alive.json immediately every FLUSH_EVERY hits + reload server
  5. Merge all survivors into master_pool.json
  6. Sleep CYCLE_MINUTES, repeat

Run in background:
    python3 proxy_keeper.py > /tmp/proxy_keeper.log 2>&1 &
"""

import json
import os
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector as _ProxyConnector
import requests as req_lib
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import source_manager as _sm

# ── Paths ──────────────────────────────────────────────────────────────────
REPO   = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(REPO, "proxies", "master_pool.json")
ALIVE  = os.path.join(REPO, "proxies", "alive.json")

# ── Tuning ─────────────────────────────────────────────────────────────────
CYCLE_MINUTES     = 5     # sleep between cycles
WORKERS           = 120   # thread-pool workers for fetch phase (not probe)
PROBE_TIMEOUT     = 7.0   # per-proxy timeout seconds (5s was too short, killed good proxies)
ASYNC_CONCURRENCY = 1500  # concurrent async probes — no thread-limit constraint
RESAMPLE_SIZE   = 400   # existing master entries to re-validate per cycle — KEEP LOW
                        # high values destroy the pool during network outages
MIN_SURVIVORS   = 30    # refuse to overwrite alive.json below this
FLUSH_EVERY     = 25    # write alive.json + reload server after this many new hits
SERVER_RELOAD   = "http://127.0.0.1:5001/admin/reload"

# ── Probe target — GET talkback list (does NOT burn vote capacity) ──────────
# Using GET instead of POST means we don't spend any real votes during probing.
# A proxy that can GET the talkback list API can almost certainly POST votes too.
# Yield: ~2-5% (vs ~0.3% for POST vote) because Ynet doesn't dedup GETs.
YNET_BASE    = "https://www.ynet.co.il"
HEADERS  = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":       "application/json, text/plain, */*",
    "Origin":       YNET_BASE,
    "Referer":      f"{YNET_BASE}/",
}

KNOWN_ARTICLES_FILE   = os.path.join(REPO, "results", "known_articles.json")
SERVER_KNOWN_ARTICLES = "http://127.0.0.1:5001/api/known_articles"
SERVER_USED_PROXIES   = "http://127.0.0.1:5001/api/used_proxies"
DEFAULT_ARTICLE       = "yokra14737379"

# ── URL → source registry key mapping ────────────────────────────────────────
# Maps each URL in SOURCES to its canonical source key (matching mega_harvest.py
# GITHUB_SOURCES names). Proxies fetched from each URL are tagged with their
# specific source so scores in source_registry.json go to the right source.
URL_TO_SOURCE = {
    # TheSpeedX PROXY-List
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt":    "speedx_http",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt":  "speedx_s4",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt":  "speedx_s5",
    # TheSpeedX SOCKS-List
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt":    "speedx_sockslist_http",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt":  "speedx_sockslist_s4",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt":  "speedx_sockslist_s5",
    # proxyspace.pro direct
    "https://proxyspace.pro/http.txt":   "prxspace_http",
    "https://proxyspace.pro/socks4.txt": "prxspace_s4",
    "https://proxyspace.pro/socks5.txt": "prxspace_s5",
    # monosans
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt":              "monosans_http",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt":            "monosans_s4",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt":            "monosans_s5",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt":    "monosans_http",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks4.txt":  "monosans_s4",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks5.txt":  "monosans_s5",
    # mmpx12
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt":   "mmpx12_http",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt": "mmpx12_s4",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt": "mmpx12_s5",
    # ErcinDedeoglu
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt":   "ercin_http",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt": "ercin_s4",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt": "ercin_s5",
    # proxifly
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt":            "proxifly",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt":  "proxifly_http",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt": "proxifly_https",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt":"proxifly_s4",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt":"proxifly_s5",
    # hookzof
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt": "hookzof_s5",
    # prxchk
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt":   "prxchk_http",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt": "prxchk_s4",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt": "prxchk_s5",
    # MuRongPIG
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt":   "murong_http",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt": "murong_s4",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt": "murong_s5",
    # yemixzy
    "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt":   "yemixzy_http",
    "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt": "yemixzy_s5",
    "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks4.txt": "yemixzy_s4",
    # zloi-user
    "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt": "zloi_s5",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt":   "zloi_http",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks4.txt": "zloi_s4",
    # roosterkid
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt": "roosterkid_s5",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt": "roosterkid_s4",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt":  "roosterkid_http",
    # B4RC0DE
    "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt": "b4_s5",
    "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt": "b4_s4",
    "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt":   "b4_http",
    # ShiftyTR
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt": "shifty_s5",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt": "shifty_s4",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt":   "shifty_http",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt":  "shifty_http",
    # jetkai
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt": "jetkai_s5",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt": "jetkai_s4",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt":   "jetkai_http",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt":  "jetkai_http",
    # rdavydov
    "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt": "rdavy_s5",
    "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt": "rdavy_s4",
    "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt":   "rdavy_http",
    # sunny9577
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt": "sunny_s5",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt":   "sunny9577",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt": "sunny_s4",
    # HyperBeats
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks5.txt": "hyper_s5",
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks4.txt": "hyper_s4",
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt":   "hyper_http",
    # Zaeem20
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt": "zaeem_s5",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt": "zaeem_s4",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt":   "zaeem_http",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt":  "zaeem_https",
    # KangProxy
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt": "kang_s5",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt": "kang_s4",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt":   "kang_http",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt":     "kang_http",
    # ALIILAPRO
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt": "alii_s5",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt": "alii_s4",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt":   "alii_http",
    # casals-ar
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5": "casals_s5",
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks4": "casals_s4",
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http":   "casals_http",
    # zevtyardt
    "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt": "zev_s5",
    "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt": "zev_s4",
    "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt":   "zev_http",
    # RX4096
    "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks5.txt": "rx4096_s5",
    "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks4.txt": "rx4096_s4",
    "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt":   "rx4096_http",
    # ObcbO
    "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt": "obcbo_s5",
    "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks4.txt": "obcbo_s4",
    "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt":   "obcbo_http",
    # UptimerBot
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks5.txt": "uptimer_s5",
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks4.txt": "uptimer_s4",
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt":   "uptimer_http",
    # caliphdev
    "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks5.txt": "caliph_s5",
    "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks4.txt": "caliph_s4",
    "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/http.txt":   "caliph_http",
    # anonym0usWork1221
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt": "anon_s5",
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt": "anon_s4",
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt":   "anon_http",
    # yakumo/elliottophellia
    "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt": "yakumo_s5",
    "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt": "yakumo_s4",
    "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt":     "yakumo_http",
    # dpangestuw
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt":   "dpang_http",
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt": "dpang_s4",
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt": "dpang_s5",
    # tuanminpay
    "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt":   "tuan_http",
    "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks4.txt": "tuan_s4",
    "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt": "tuan_s5",
    # proxy4parsing
    "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt":    "p4p_http",
    "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/hproxy.txt":  "p4p_hproxy",
    # saschazesiger
    "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/http.txt":   "sascha_http",
    "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks4.txt": "sascha_s4",
    "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks5.txt": "sascha_s5",
    # r00tee
    "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt":  "r00t_http",
    "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt": "r00t_s4",
    "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt": "r00t_s5",
    # lalifeier
    "https://raw.githubusercontent.com/lalifeier/proxy-list/main/http.txt":   "lali_http",
    "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks4.txt": "lali_s4",
    "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks5.txt": "lali_s5",
    # vakhov
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt":   "vakhov_http",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks4.txt": "vakhov_s4",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt": "vakhov_s5",
    # manuGMG
    "https://raw.githubusercontent.com/manuGMG/proxy-365/main/HTTP.txt":   "manu_http",
    "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS4.txt": "manu_s4",
    "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS5.txt": "manu_s5",
    # saisuiu Chinese
    "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/cnfree.txt": "saisuiu_cn",
    # sunny9577 socks4
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt": "sunny_s4",
    # UserR3X
    "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/http.txt":   "userr3x_http",
    "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks4.txt": "userr3x_s4",
    "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks5.txt": "userr3x_s5",
    # Vann-Dev
    "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt":   "vanndev_http",
    "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks4.txt": "vanndev_s4",
    "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks5.txt": "vanndev_s5",
    # im-razvan
    "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt":   "razvan_http",
    "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks4.txt": "razvan_s4",
    "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks5.txt": "razvan_s5",
    # MrMarble
    "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt": "mrmarble",
    # themiralay
    "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt": "miralay",
    # a2u
    "https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.txt": "a2u",
    # proxyspace GitHub mirror
    "https://raw.githubusercontent.com/proxyspace/proxyspace/master/http.txt":   "prxspace_http",
    "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks4.txt": "prxspace_s4",
    "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks5.txt": "prxspace_s5",
    # saisuiu free list
    "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt": "saisuiu_free",
    # mmpx12 https
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt": "mmpx12_https",
    # proxylist-to
    "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/http.txt":   "proxylto_http",
    "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks5.txt": "proxylto_s5",
    "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks4.txt": "proxylto_s4",
    # roma8ok (931 http + 134 socks5 verified in cycle 9)
    "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-http.txt":   "roma8ok_http",
    "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-socks5.txt": "roma8ok_s5",
    # sunny9577 combined proxies.txt (all protocols, 1513 IPs)
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt": "sunny_all",
    # ── VPSLabCloud ──────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt":   "vpslab_http",
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks4_all.txt": "vpslab_s4",
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt": "vpslab_s5",
    # ── gfpcom wiki ──────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http.txt":   "gfpcom_http",
    "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks4.txt": "gfpcom_s4",
    "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks5.txt": "gfpcom_s5",
    # ── ClearProxy ───────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/http/raw/all.txt":   "clearproxy_http",
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/socks4/raw/all.txt": "clearproxy_s4",
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/socks5/raw/all.txt": "clearproxy_s5",
    # ── Thordata ─────────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt":   "thordata_http",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks4.txt": "thordata_s4",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt": "thordata_s5",
    # ── BlackSnowDot ─────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/BlackSnowDot/proxylist-update-every-minute/main/http.txt":  "blacksnow_http",
    "https://raw.githubusercontent.com/BlackSnowDot/proxylist-update-every-minute/main/socks.txt": "blacksnow_socks",
    # ── vmheaven ─────────────────────────────────────────────────────────────
    "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/http.txt":   "vmheaven_http",
    "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/https.txt":  "vmheaven_http",
    "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks4.txt": "vmheaven_s4",
    "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks5.txt": "vmheaven_s5",
    # ── officialputuid/ProxyForEveryone ──────────────────────────────────────
    "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/http/http.txt":    "proxyforall_http",
    "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/https/https.txt":  "proxyforall_http",
    "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/socks4/socks4.txt":"proxyforall_s4",
    "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/socks5/socks5.txt":"proxyforall_s5",
    # ── fyvri/fresh-proxy-list ───────────────────────────────────────────────
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/http.txt":   "fyvri_http",
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/https.txt":  "fyvri_http",
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/socks4.txt": "fyvri_s4",
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/socks5.txt": "fyvri_s5",
    # ── ebrasha/abdal-proxy-hub ──────────────────────────────────────────────
    "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/http-proxy-list-by-EbraSha.txt":   "abdal_http",
    "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/https-proxy-list-by-EbraSha.txt":  "abdal_http",
    "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks4-proxy-list-by-EbraSha.txt": "abdal_s4",
    "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks5-proxy-list-by-EbraSha.txt": "abdal_s5",
    # ── iplocate/free-proxy-list ─────────────────────────────────────────────
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt":   "iplocate_http",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks4.txt": "iplocate_s4",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt": "iplocate_s5",
    # ── databay-labs/free-proxy-list ─────────────────────────────────────────
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt":   "databayl_http",
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt": "databayl_s4",
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt": "databayl_s5",
    # ── gitrecon1455/fresh-proxy-list ────────────────────────────────────────
    "https://raw.githubusercontent.com/gitrecon1455/fresh-proxy-list/main/proxylist.txt": "gitrecon_all",
    # ── dinoz0rg/proxy-list ──────────────────────────────────────────────────
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt":   "dinoz_http",
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt": "dinoz_s5",
    # ── Skillter/ProxyGather ─────────────────────────────────────────────────
    "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-http.txt":  "skillter_http",
    "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-socks5.txt":"skillter_s5",
    # ── stormsia/proxy-list ──────────────────────────────────────────────────
    "https://raw.githubusercontent.com/stormsia/proxy-list/main/working_proxies.txt": "stormsia_all",
    # ── ZaidGuy/proxy-list-1 ─────────────────────────────────────────────────
    "https://raw.githubusercontent.com/ZaidGuy/proxy-list-1/main/online-proxies/txt/proxies-http.txt":   "zaid_http",
    "https://raw.githubusercontent.com/ZaidGuy/proxy-list-1/main/online-proxies/txt/proxies-socks5.txt": "zaid_s5",
}

SOURCES = [
    # ── TheSpeedX PROXY-List (huge, updated frequently) ───────────────────
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    # ── TheSpeedX SOCKS-List (separate repo, different IP pool ~8k) ───────
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    # ── proxyspace.pro direct (fresher than GitHub mirror) ────────────────
    ("http",   "https://proxyspace.pro/http.txt"),
    ("socks4", "https://proxyspace.pro/socks4.txt"),
    ("socks5", "https://proxyspace.pro/socks5.txt"),
    # ── monosans ──────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/socks5.txt"),
    # ── mmpx12 ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    # ── ErcinDedeoglu ─────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    # ── proxifly ──────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    # ── hookzof ───────────────────────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    # ── prxchk ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    # ── MuRongPIG ─────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    # ── yemixzy ───────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt"),
    # ── zloi-user ─────────────────────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt"),
    ("http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks4.txt"),
    # ── Additional sources (new) ──────────────────────────────────────────
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    ("socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("socks5", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt"),
    ("socks4", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt"),
    ("http",   "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt"),
    ("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
    ("socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt"),
    ("socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"),
    ("http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
    ("http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt"),
    ("socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt"),
    ("http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5"),
    ("socks4", "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks4"),
    ("http",   "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http"),
    ("socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/socks4.txt"),
    ("http",   "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt"),
    ("http",   "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt"),
    ("socks4", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt"),
    ("http",   "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt"),
    # ── dpangestuw (4.9k http, 4.4k socks5, 3.1k socks4 — high yield) ────────
    ("http",   "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt"),
    ("socks5", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt"),
    # ── tuanminpay (14k http, 14k socks5, 11k socks4) ────────────────────────
    ("http",   "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt"),
    # ── proxy4parsing (19k http entries + hproxy.txt ~11k entries) ──────────
    ("http",   "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt"),
    ("http",   "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/hproxy.txt"),
    # ── saschazesiger ────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks5.txt"),
    # ── proxifly by protocol (separate from /all endpoint) ───────────────────
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt"),
    ("socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt"),
    ("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),
    # ── r00tee ────────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt"),
    ("socks4", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt"),
    # ── lalifeier ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/lalifeier/proxy-list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks5.txt"),
    # ── vakhov fresh list ────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),
    # ── manuGMG ──────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/manuGMG/proxy-365/main/HTTP.txt"),
    ("socks4", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS4.txt"),
    ("socks5", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS5.txt"),
    # ── saisuiu Chinese proxy pool ───────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/cnfree.txt"),
    # ── sunny9577 socks4 (http already present) ───────────────────────────────
    ("socks4", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt"),
    # ── yemixzy socks4 (http+socks5 already present) ─────────────────────────
    ("socks4", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks4.txt"),
    # ── UserR3X ──────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks5.txt"),
    # ── Vann-Dev ─────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks5.txt"),
    # ── im-razvan ────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/im-razvan/proxy_list/main/socks5.txt"),
    # ── MrMarble ─────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt"),
    # ── themiralay ───────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt"),
    # ── a2u ──────────────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.txt"),
    # ── proxyspace GitHub mirror ─────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/proxyspace/proxyspace/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks5.txt"),
    # ── saisuiu free list (broader pool, different from cnfree.txt) ──────────
    ("http",   "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt"),
    # ── mmpx12 https (separate pool from http.txt) ───────────────────────────
    ("http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
    # ── proxylist-to ─────────────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/socks4.txt"),
    # ── roma8ok (931 http + 134 socks5, validated cycle 9) ───────────────────
    ("http",   "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-http.txt"),
    ("socks5", "https://raw.githubusercontent.com/roma8ok/proxy-list/main/proxy-list-socks5.txt"),
    # ── sunny9577 combined proxies.txt (1513 IPs all protocols) ──────────────
    ("http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt"),
    # ── VPSLabCloud — every 15 min ────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt"),
    ("socks4", "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks4_all.txt"),
    ("socks5", "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt"),
    # ── gfpcom — 1.5M proxies, every 30 min (wiki storage) ───────────────────
    ("http",   "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks5.txt"),
    # ── ClearProxy — verified every 5 min ────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/http/raw/all.txt"),
    ("socks4", "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/socks4/raw/all.txt"),
    ("socks5", "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/socks5/raw/all.txt"),
    # ── Thordata — auto-verified ──────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt"),
    # ── BlackSnowDot — every minute ───────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/BlackSnowDot/proxylist-update-every-minute/main/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/BlackSnowDot/proxylist-update-every-minute/main/socks.txt"),
    # ── vmheaven — 10k+ HTTP, every 15 min ────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/http.txt"),
    ("http",   "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/https.txt"),
    ("socks4", "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks5.txt"),
    # ── officialputuid/ProxyForEveryone — 10k+ HTTP + SOCKS5 ──────────────────
    ("http",   "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/http/http.txt"),
    ("http",   "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/https/https.txt"),
    ("socks4", "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/socks4/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/officialputuid/ProxyForEveryone/main/socks5/socks5.txt"),
    # ── fyvri/fresh-proxy-list — 10k+ SOCKS5, hourly (archive branch) ─────────
    ("http",   "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/http.txt"),
    ("http",   "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/https.txt"),
    ("socks4", "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/archive/storage/classic/socks5.txt"),
    # ── ebrasha/abdal-proxy-hub — 5k+ HTTP + SOCKS5, every 10 min ────────────
    ("http",   "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/http-proxy-list-by-EbraSha.txt"),
    ("http",   "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/https-proxy-list-by-EbraSha.txt"),
    ("socks4", "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks4-proxy-list-by-EbraSha.txt"),
    ("socks5", "https://raw.githubusercontent.com/ebrasha/abdal-proxy-hub/main/socks5-proxy-list-by-EbraSha.txt"),
    # ── iplocate/free-proxy-list — 10k+ SOCKS5, every 30 min ─────────────────
    ("http",   "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt"),
    # ── databay-labs/free-proxy-list — 5k+ HTTP, every 5 min ─────────────────
    ("http",   "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt"),
    # ── gitrecon1455/fresh-proxy-list — 8k+ mixed, every 10 min ──────────────
    ("http",   "https://raw.githubusercontent.com/gitrecon1455/fresh-proxy-list/main/proxylist.txt"),
    # ── dinoz0rg/proxy-list — checked proxies ─────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt"),
    # ── Skillter/ProxyGather — verified every 30 min ──────────────────────────
    ("http",   "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-http.txt"),
    ("socks5", "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-socks5.txt"),
    # ── stormsia/proxy-list — protocol:// prefix format ───────────────────────
    ("http",   "https://raw.githubusercontent.com/stormsia/proxy-list/main/working_proxies.txt"),
    # ── ZaidGuy/proxy-list-1 ──────────────────────────────────────────────────
    ("http",   "https://raw.githubusercontent.com/ZaidGuy/proxy-list-1/main/online-proxies/txt/proxies-http.txt"),
    ("socks5", "https://raw.githubusercontent.com/ZaidGuy/proxy-list-1/main/online-proxies/txt/proxies-socks5.txt"),
]

# ── Additional live API sources (fetched directly, not from GitHub) ─────────
import datetime as _dt

def _fetch_geoxy():
    """geoxy.io elite-only proxies — API token from floppydata.com page JS."""
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


def _fetch_fate0():
    """fate0/proxylist — JSON objects one per line, each with host/port/type fields.
    Format: {"host":"ip","port":N,"type":"http|socks5"} — updated daily."""
    TYPE_MAP = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5"}
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list",
            headers={"User-Agent": "proxy-keeper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", errors="ignore")
        out = []
        for line in text.splitlines():
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


def _fetch_hidemium():
    """hidemium.io — 78 proxies as escaped JSON in page HTML."""
    import re as _re
    out = []
    try:
        body = _http_get("https://hidemium.io/free-proxy/", 15)
        addrs  = _re.findall(r'\\\\\"address\\\\\":\\\\\"([\d.:]+)\\\\\"', body)
        protos = _re.findall(r'\\\\\"protocols\\\\\":\[\\\\\"([a-z0-9]+)\\\\\"', body)
        for addr, proto in zip(addrs, protos):
            scheme = "socks5" if proto == "socks5" else "socks4" if proto == "socks4" else "http"
            if ":" in addr:
                out.append((scheme, addr))
    except Exception:
        pass
    return out


def _fetch_databay():
    """databay.com free proxy API — verified proxies."""
    out = []
    for proto, scheme in (("socks5", "socks5"), ("http", "http"), ("socks4", "socks4")):
        try:
            body = _http_get(f"https://databay.com/api/v1/proxy-list?protocol={proto}&format=txt", 20)
            out.extend(_parse_lines(scheme, body))
        except Exception:
            pass
    return out


def _fetch_checkerproxy_keeper():
    """checkerproxy.net daily archive — pre-verified proxies from last 24-48 h."""
    TYPE_MAP = {1: "http", 2: "http", 3: "socks4", 4: "socks5"}
    out = []
    for delta in range(3):
        try:
            d = (_dt.date.today() - _dt.timedelta(days=delta)).strftime("%Y-%m-%d")
            req = urllib.request.Request(
                f"https://checkerproxy.net/api/archive/{d}",
                headers={"User-Agent": "proxy-keeper/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                items = json.loads(r.read())
            for item in items:
                addr   = (item.get("addr") or "").strip()
                scheme = TYPE_MAP.get(item.get("type", 1), "http")
                if addr and ":" in addr:
                    out.append((scheme, addr))
            if out:
                break
        except Exception:
            pass
    return out


# ══════════════════════════════════════════════════════════════════════════
# Self-healing backfill: tag untagged master_pool entries at cycle start
# by fetching the top high-hit-rate sources and matching addrs.
#
# Ordered by observed hit-rate (high → low). These small lists (~1-15k IPs
# each) are fetched in parallel so the backfill completes in <20s.
_BACKFILL_SOURCES = [
    # (source_key, scheme, url)  — only sources with known >= 2% hit rate
    ("anon_s5",    "socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    ("ercin_s5",   "socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    ("monosans_s5","socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("hookzof_s5", "socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("alii_s5",    "socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),
    ("zaeem_s5",   "socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),
    ("tuan_s5",    "socks5", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt"),
    ("speedx_sockslist_s5","socks5","https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("anon_http",  "http",   "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("mmpx12_http","http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("speedx_http","http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("monosans_http","http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("ercin_http", "http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("dpang_http", "http",   "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
    ("dpang_s5",   "socks5", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt"),
    ("proxifly",   "http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    ("jetkai_s5",  "socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    ("jetkai_http","http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("speedx_sockslist_http","http","https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("r00t_http",  "http",   "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt"),
]

def _quick_backfill(master_pool):
    """Fetch top-N high-hit-rate sources in parallel and tag untagged master entries.

    Called at start of run_cycle() before the probe phase.
    Returns count of entries newly tagged.
    """
    untagged = [p for p in master_pool if not p.get("source")]
    if not untagged:
        return 0

    # Build addr→source map from fetched lists
    addr_to_src = {}

    def _fetch_source(item):
        src_key, scheme, url = item
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "proxy-keeper/backfill"})
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read().decode("utf-8", errors="ignore")
            addrs = {}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip scheme:// prefix if present
                if "://" in line:
                    _, _, line = line.partition("://")
                    line = line.strip()
                parts = line.split(":")
                if len(parts) == 2:
                    try:
                        int(parts[1])
                        addrs[line] = src_key
                    except ValueError:
                        pass
            return addrs
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(_fetch_source, item) for item in _BACKFILL_SOURCES]
        for fut in as_completed(futs):
            try:
                result = fut.result()
                # First-seen source wins (sources are ordered high→low hit-rate)
                for addr, src in result.items():
                    if addr not in addr_to_src:
                        addr_to_src[addr] = src
            except Exception:
                pass

    # Tag untagged entries
    tagged = 0
    for p in untagged:
        addr = p.get("addr", "")
        src = addr_to_src.get(addr)
        if src:
            p["source"] = src
            tagged += 1

    return tagged


def load_known_articles():
    """Return list of article IDs. Try server API → local file → config fallback."""
    try:
        req = urllib.request.urlopen(SERVER_KNOWN_ARTICLES, timeout=5)
        data = json.loads(req.read())
        ids = data.get("article_ids", [])
        if ids:
            return ids
    except Exception:
        pass
    try:
        data = json.load(open(KNOWN_ARTICLES_FILE))
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    try:
        cfg = json.load(open(os.path.join(REPO, "config.json")))
        return [cfg.get("article_id", "yokra14737379")]
    except Exception:
        return ["yokra14737379"]


def fetch_article_targets(article_ids):
    """
    Fetch page-1 talkback IDs for each article directly from Ynet (no proxy).
    Returns {article_id: [talkback_id, ...]}. Articles that fail are skipped.
    """
    targets = {}
    for article_id in article_ids:
        url = f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{article_id}/0/1"
        try:
            r = req_lib.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                continue
            ch = r.json().get("rss", {}).get("channel", {}) or {}
            items = ch.get("item", []) or []
            ids = [c["id"] for c in items if c.get("id")]
            if ids:
                targets[article_id] = ids
                log(f"  article {article_id}: {len(ids)} comments loaded")
            else:
                log(f"  article {article_id}: 0 comments (skipped)")
        except Exception as e:
            log(f"  article {article_id}: fetch failed ({e})")
    return targets


def load_used_proxy_addrs():
    """
    Return the set of proxy addresses that already cast a successful vote this
    server session (via /api/used_proxies). Falls back to alive.json addresses.
    """
    try:
        req = urllib.request.urlopen(SERVER_USED_PROXIES, timeout=5)
        data = json.loads(req.read())
        used = set(data.get("used_proxies", []))
        if used:
            return used
    except Exception:
        pass
    try:
        alive = json.load(open(ALIVE))
        return {p["addr"] for p in alive}
    except Exception:
        return set()


# ══════════════════════════════════════════════════════════════════════════
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_master():
    if not os.path.exists(MASTER):
        return []
    try:
        return json.load(open(MASTER))
    except Exception:
        return []


def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def reload_server():
    try:
        req = urllib.request.Request(SERVER_RELOAD, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            log(f"  server reloaded → {resp.get('loaded')} proxies")
    except Exception as e:
        log(f"  server reload skipped ({e})")


# ══════════════════════════════════════════════════════════════════════════
def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-keeper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _parse_lines(scheme, text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith("#"):
            parts = line.split(":")
            if len(parts) == 2:
                try:
                    int(parts[1])
                    out.append((scheme, line))
                except ValueError:
                    pass
    return out


def _parse_lines_with_scheme(text):
    """Parse lines that may have scheme:// prefixes (e.g. socks5://1.2.3.4:1080)."""
    out = []
    VALID = {"http", "https", "socks4", "socks5"}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            s, _, addr = line.partition("://")
            s = s.lower()
            if s == "https":
                s = "http"
            if s not in VALID:
                continue
            addr = addr.strip()
            parts = addr.split(":")
            if len(parts) == 2:
                try:
                    int(parts[1])
                    out.append((s, addr))
                except ValueError:
                    pass
        else:
            parts = line.split(":")
            if len(parts) == 2:
                try:
                    int(parts[1])
                    out.append(("http", line))
                except ValueError:
                    pass
    return out


def fetch_candidates(known_addrs):
    candidates = []  # list of (scheme, addr, source_key)
    sm = _sm.get()

    def _fetch_one_source(item):
        scheme, url = item
        src_key = URL_TO_SOURCE.get(url, "keeper")
        try:
            sm.ensure_source(src_key, url=url, scheme=scheme, category="github")
            body = _http_get(url, timeout=12)
            found = _parse_lines(scheme, body)
            if found:
                sm.record_harvest(src_key, len(found))
            return [(s, a, src_key) for s, a in found]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=30) as ex:
        for result in ex.map(_fetch_one_source, SOURCES, timeout=60):
            candidates.extend(result)

    # All-protocol endpoint — one call, 7k+ proxies with scheme:// prefix
    try:
        url = ("https://api.proxyscrape.com/v3/free-proxy-list/get"
               "?request=displayproxies&protocol=all"
               "&timeout=10000&country=all&proxy_format=protocolipport&format=text")
        sm.ensure_source("proxyscrape", url="https://api.proxyscrape.com", scheme="mixed", category="api")
        body = _http_get(url, 45)
        found = list(_parse_lines_with_scheme(body))
        for s, a in found:
            candidates.append((s, a, "proxyscrape"))
        if found:
            sm.record_harvest("proxyscrape", len(found))
    except Exception:
        pass

    for proto in ("http", "socks4", "socks5"):
        # Standard bulk fetch
        try:
            url = (f"https://api.proxyscrape.com/v3/free-proxy-list/get"
                   f"?request=displayproxies&protocol={proto}"
                   f"&timeout=20000&country=all&proxy_format=ipport&format=text")
            body = _http_get(url, 30)
            found = _parse_lines(proto, body)
            for s, a in found:
                candidates.append((s, a, "proxyscrape"))
            if found:
                sm.record_harvest("proxyscrape", len(found))
        except Exception:
            pass
        # Elite + fast subset (more likely to bypass Ynet detection)
        try:
            url = (f"https://api.proxyscrape.com/v2/"
                   f"?request=getproxies&protocol={proto}"
                   f"&timeout=1000&country=all&ssl=all&anonymity=elite")
            body = _http_get(url, 15)
            found = _parse_lines(proto, body)
            for s, a in found:
                candidates.append((s, a, "proxyscrape"))
            if found:
                sm.record_harvest("proxyscrape", len(found))
        except Exception:
            pass
        # Elite proxies with 5000ms timeout — larger pool than 1000ms tier
        try:
            src_key5k = f"proxyscrape_elite5k_{proto[:4]}"
            sm.ensure_source(src_key5k, url=f"https://api.proxyscrape.com/v2/?protocol={proto}&timeout=5000&anonymity=elite", scheme=proto, category="api")
            url = (f"https://api.proxyscrape.com/v2/"
                   f"?request=getproxies&protocol={proto}"
                   f"&timeout=5000&country=all&ssl=all&anonymity=elite")
            body = _http_get(url, 20)
            found = _parse_lines(proto, body)
            for s, a in found:
                candidates.append((s, a, src_key5k))
            if found:
                sm.record_harvest(src_key5k, len(found))
        except Exception:
            pass

    # fate0/proxylist — JSON-per-line format with host/port/type fields
    try:
        sm.ensure_source("fate0_proxylist", url="https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list", scheme="mixed", category="api")
        f0 = _fetch_fate0()
        for s, a in f0:
            candidates.append((s, a, "fate0_proxylist"))
        if f0:
            sm.record_harvest("fate0_proxylist", len(f0))
        log(f"  fate0/proxylist: {len(f0)} candidates")
    except Exception:
        pass

    # checkerproxy.net — pre-verified daily archive (highest-quality free source)
    try:
        sm.ensure_source("checkerproxy", url="https://checkerproxy.net/api/archive/", scheme="mixed", category="api")
        cp = _fetch_checkerproxy_keeper()
        for s, a in cp:
            candidates.append((s, a, "checkerproxy"))
        if cp:
            sm.record_harvest("checkerproxy", len(cp))
        log(f"  checkerproxy.net: {len(cp)} candidates")
    except Exception:
        pass

    # geoxy.io — elite-only verified proxies via floppydata.com API token
    try:
        sm.ensure_source("geoxy.io", url="https://geoxy.io/proxies", scheme="mixed", category="api")
        gx = _fetch_geoxy()
        for s, a in gx:
            candidates.append((s, a, "geoxy.io"))
        if gx:
            sm.record_harvest("geoxy.io", len(gx))
        log(f"  geoxy.io (elite): {len(gx)} candidates")
    except Exception:
        pass

    # hidemium.io — 78 verified proxies embedded as JSON in page HTML
    try:
        sm.ensure_source("hidemium.io", url="https://hidemium.io/free-proxy/", scheme="mixed", category="api")
        hm = _fetch_hidemium()
        for s, a in hm:
            candidates.append((s, a, "hidemium.io"))
        if hm:
            sm.record_harvest("hidemium.io", len(hm))
        log(f"  hidemium.io: {len(hm)} candidates")
    except Exception:
        pass

    # databay.com — live verified proxy API
    try:
        sm.ensure_source("databay.com", url="https://databay.com/api/v1/proxy-list", scheme="mixed", category="api")
        db = _fetch_databay()
        for s, a in db:
            candidates.append((s, a, "databay.com"))
        if db:
            sm.record_harvest("databay.com", len(db))
        log(f"  databay.com: {len(db)} candidates")
    except Exception:
        pass

    # free-proxy-list.net /en/ — HTML table, up to 100 pages
    sm.ensure_source("freeproxylist", url="https://free-proxy-list.net/en/", scheme="http", category="api")
    import re as _re
    _fpl_count = 0
    for _fpl_url in (
        ["https://free-proxy-list.net/en/"]
        + [f"https://free-proxy-list.net/en/?page={p}" for p in range(2, 101)]
        + ["https://free-proxy-list.net/anonymous-proxy.html"]
    ):
        _prev = _fpl_count
        try:
            body = _http_get(_fpl_url, 15)
            pairs = _re.findall(
                r"<td>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td>\s*(\d{2,5})\s*</td>",
                body)
            for ip, port in pairs:
                candidates.append(("http", f"{ip}:{port}", "freeproxylist"))
                _fpl_count += 1
        except Exception:
            break
        if _fpl_count == _prev and _fpl_url != "https://free-proxy-list.net/anonymous-proxy.html":
            break
    if _fpl_count:
        sm.record_harvest("freeproxylist", _fpl_count)
    log(f"  free-proxy-list.net/en/: scraped ({_fpl_count} found)")

    seen = set(known_addrs)
    fresh = []
    for s, a, src in candidates:
        if a not in seen:
            seen.add(a)
            fresh.append((s, a, src))
    return fresh


# ══════════════════════════════════════════════════════════════════════════
# Probe using requests (same library as server) so validated proxies actually
# work for votes — aiohttp and requests behave differently with SOCKS proxies.

def _get_probe_url(targets):
    """Pick a talkback list URL to GET through the proxy (doesn't burn any votes)."""
    if targets:
        article_id = random.choice(list(targets.keys()))
    else:
        try:
            article_id = json.load(open(os.path.join(REPO, "config.json"))).get(
                "article_id", DEFAULT_ARTICLE)
        except Exception:
            article_id = DEFAULT_ARTICLE
    return f"{YNET_BASE}/iphone/json/api/talkbacks/list/v2/{article_id}/0/1"


async def _probe_one_async(scheme, addr, targets, used_addrs):
    proxy_url = f"{scheme}://{addr}"
    url = _get_probe_url(targets)
    t0 = time.time()
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
    try:
        if scheme in ("socks4", "socks5"):
            connector = _ProxyConnector.from_url(proxy_url, rdns=True)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.get(url, headers=HEADERS, timeout=timeout) as r:
                    ms = int((time.time() - t0) * 1000)
        else:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=HEADERS, proxy=proxy_url, timeout=timeout) as r:
                    ms = int((time.time() - t0) * 1000)
        return {
            "scheme":       scheme,
            "addr":         addr,
            "exit_ip":      addr.split(":")[0],
            "ynet_ms":      ms,
            "already_used": addr in used_addrs,
        }
    except Exception:
        return None


async def _probe_async(candidates, targets, used_addrs, on_flush, prev_alive):
    hits = []
    tested_addrs = set()
    done = 0
    last_flush = 0
    total = len(candidates)
    semaphore = asyncio.Semaphore(ASYNC_CONCURRENCY)
    lock = asyncio.Lock()
    loop = asyncio.get_event_loop()

    async def _task(scheme, addr, src_key):
        nonlocal done, last_flush
        async with semaphore:
            rec = await _probe_one_async(scheme, addr, targets, used_addrs)

        flush_args = None
        async with lock:
            done += 1
            tested_addrs.add(addr)
            if rec:
                if not rec.get("source") and src_key:
                    rec["source"] = src_key
                hits.append(rec)
                effective_src = rec.get("source") or src_key
                if effective_src:
                    try:
                        _sm.get().record_probe_hit(effective_src)
                    except Exception:
                        pass
                if len(hits) - last_flush >= FLUSH_EVERY:
                    last_flush = len(hits)
                    flush_args = (list(hits), set(tested_addrs), prev_alive)
            if done % 500 == 0 or done == total:
                vote_ok = sum(1 for h in hits if h.get("vote_ok"))
                log(f"  probed {done}/{total}  reachable: {len(hits)}  vote_ok: {vote_ok}")

        if flush_args:
            await loop.run_in_executor(None, on_flush, *flush_args)

    await asyncio.gather(*[_task(s, a, src) for s, a, src in candidates])
    return hits, tested_addrs


def probe_all(candidates, targets, used_addrs, on_flush, prev_alive=None):
    """
    Probe all candidates concurrently via asyncio (ASYNC_CONCURRENCY=1500).
    Calls on_flush(hits, tested_addrs, prev_alive) every FLUSH_EVERY hits.
    candidates = list of (scheme, addr, source_key) 3-tuples.
    """
    return asyncio.run(_probe_async(candidates, targets, used_addrs, on_flush, prev_alive))


# ══════════════════════════════════════════════════════════════════════════
def flush_alive(hits, tested_addrs=None, prev_alive=None):
    """
    Write alive.json, merging with still-untested entries from the previous cycle.

    During a cycle we only know about proxies we've actually probed so far.
    Proxies from the previous alive.json that haven't been re-tested yet are kept
    as-is — they're still the best information we have about those addresses.
    As the cycle progresses, each tested address either becomes a new hit (updated)
    or a confirmed miss (dropped). By cycle end alive.json converges to only
    the addresses confirmed alive in the current cycle.
    """
    # Enrich hits AND prev_alive carry-overs with source tags from master_pool
    try:
        master_src = json.load(open(MASTER))
        _src_map = {p["addr"]: p.get("source") for p in master_src if p.get("addr") and p.get("source")}
        for h in hits:
            if not h.get("source") and h.get("addr") in _src_map:
                h["source"] = _src_map[h["addr"]]
    except Exception:
        _src_map = {}

    merged = list(hits)  # confirmed alive in current cycle so far

    if prev_alive and tested_addrs is not None:
        hit_addrs = {h["addr"] for h in hits}
        for p in prev_alive:
            if p["addr"] not in tested_addrs and p["addr"] not in hit_addrs:
                # Enrich carried-over entry with source tag if missing
                if not p.get("source") and p.get("addr") in _src_map:
                    p = {**p, "source": _src_map[p["addr"]]}
                merged.append(p)

    if len(merged) < MIN_SURVIVORS:
        return
    # Sort: socks5 first (best tunneling), then socks4, then http/https.
    # Within each scheme, fastest (lowest ynet_ms) first.
    SCHEME_RANK = {"socks5": 0, "socks4": 1, "http": 2, "https": 2}
    sorted_hits = sorted(
        merged,
        key=lambda x: (SCHEME_RANK.get(x.get("scheme", "http"), 2), x.get("ynet_ms", 99999))
    )
    atomic_write(ALIVE, sorted_hits)
    log(f"  flushed {len(sorted_hits)} to alive.json"
        f"  (current-cycle: {len(hits)}, carried-over: {len(sorted_hits)-len(hits)})")
    reload_server()


# ══════════════════════════════════════════════════════════════════════════
def _ynet_reachable():
    """Quick DNS + TCP check — returns True if www.ynet.co.il is reachable."""
    import socket as _sock
    try:
        _sock.setdefaulttimeout(5)
        _sock.getaddrinfo("www.ynet.co.il", 443)
        return True
    except Exception:
        return False


def run_cycle(cycle_num):
    log(f"=== Cycle #{cycle_num} start ===")

    # Safety check: if Ynet DNS is down, ALL probes will fail and the pool
    # will be aggressively pruned for no reason. Skip the cycle entirely.
    if not _ynet_reachable():
        log("  ⚠ Ynet unreachable (DNS/network) — skipping cycle to protect pool")
        return

    # Ensure "keeper" source exists in registry for scoring new candidates found here
    try:
        _sm.get().ensure_source("keeper", url="proxy_keeper.py", scheme="mixed", category="keeper")
    except Exception:
        pass

    master = load_master()
    known_addrs = {p["addr"] for p in master}
    log(f"  master_pool: {len(master)} entries")

    # Self-healing backfill: tag untagged master entries using top sources
    try:
        tagged_n = _quick_backfill(master)
        if tagged_n > 0:
            log(f"  backfill: tagged {tagged_n} previously-untagged master entries")
            # Persist the newly-tagged entries immediately so the next flush includes them
            atomic_write(MASTER, master)
    except Exception as _bf_err:
        log(f"  backfill skipped ({_bf_err})")

    # Load real articles + their comments to use as vote targets
    log("Phase 0: loading vote targets...")
    article_ids = load_known_articles()
    log(f"  known articles: {article_ids}")
    targets = fetch_article_targets(article_ids)
    if not targets:
        log("  WARNING: no comments fetched — probes will use connectivity-only fallback")
    total_comments = sum(len(v) for v in targets.values())
    log(f"  {len(targets)} articles · {total_comments} comments available as targets")

    # Load proxy addresses that already voted successfully this server session
    used_addrs = load_used_proxy_addrs()
    log(f"  used_proxies (already voted): {len(used_addrs)}")

    # Snapshot existing alive.json FIRST — needed to build probe list and for merging.
    try:
        prev_alive = json.load(open(ALIVE))
        log(f"  prev alive.json: {len(prev_alive)} proxies (will carry over un-retested)")
    except Exception:
        prev_alive = []

    # Fetch new candidates
    log("Phase 1: fetching candidates...")
    t0 = time.time()
    new_candidates = fetch_candidates(known_addrs)
    log(f"  {len(new_candidates)} new candidates in {time.time()-t0:.0f}s")

    # Re-probe a sample of existing entries + all new candidates
    resample = random.sample(master, min(RESAMPLE_SIZE, len(master)))
    resampled_addrs = {p["addr"] for p in resample}
    # 3-tuples: (scheme, addr, source_key)
    resample_pairs = [(p["scheme"], p["addr"], p.get("source", "keeper")) for p in resample]

    master_by_addr = {p["addr"]: p for p in master}
    already_queued = set(resampled_addrs)

    # Always re-test every proxy currently in alive.json — critical for accuracy.
    alive_to_probe = []
    for p in prev_alive:
        if p["addr"] not in already_queued:
            alive_to_probe.append((p["scheme"], p["addr"], p.get("source", "keeper")))
            already_queued.add(p["addr"])
    if alive_to_probe:
        log(f"  +{len(alive_to_probe)} alive.json proxies added to probe")

    # Always probe every currently-used proxy too.
    used_to_probe = [
        (master_by_addr[a]["scheme"], a, master_by_addr[a].get("source", "keeper"))
        for a in used_addrs
        if a in master_by_addr and a not in already_queued
    ]
    if used_to_probe:
        log(f"  +{len(used_to_probe)} currently-used proxies added to probe")

    all_candidates = resample_pairs + alive_to_probe + used_to_probe + new_candidates
    random.shuffle(all_candidates)

    log(f"Phase 2: probing {len(all_candidates)} total (workers={WORKERS})...")

    def _flush(hits, tested, prev):
        flush_alive(hits, tested, prev)

    t0 = time.time()
    hits, tested_addrs = probe_all(all_candidates, targets, used_addrs, _flush, prev_alive)
    elapsed = time.time() - t0
    vote_ok_count    = sum(1 for h in hits if h.get("vote_ok"))
    already_used_ct  = sum(1 for h in hits if h.get("already_used"))
    fresh_votes      = sum(1 for h in hits if h.get("vote_ok") and not h.get("already_used"))
    log(f"  done: {len(hits)} reachable  already_used: {already_used_ct}  in {elapsed:.0f}s")

    if len(hits) < MIN_SURVIVORS:
        log(f"  only {len(hits)} survivors — skipping master save")
        return

    # Final flush — merge with prev_alive so proxies not re-tested this cycle
    # survive into the next cycle. Only proxies confirmed dead (tested & missed)
    # are dropped. This lets the alive pool GROW across cycles rather than reset.
    flush_alive(hits, tested_addrs=tested_addrs, prev_alive=prev_alive)

    # Rebuild master: keep un-sampled entries + survivors from resample + new hits
    hit_map = {h["addr"]: h for h in hits}
    hit_addrs = set(hit_map)

    kept = []
    for p in master:
        if p["addr"] not in resampled_addrs:
            kept.append(p)
        elif p["addr"] in hit_addrs:
            # Merge: original entry fields first, probe result fields override.
            # This preserves source tag and any other original metadata that
            # probe_one() does not set (e.g. country, tags added by harvest).
            merged_entry = {**p, **hit_map[p["addr"]]}
            # Ensure source is never lost — original wins if probe result has none
            if p.get("source") and not hit_map[p["addr"]].get("source"):
                merged_entry["source"] = p["source"]
            merged_entry["failures"] = 0  # reset on success
            kept.append(merged_entry)
        else:
            # Failed this probe — need 110 consecutive failures before removal
            fail_count = p.get("failures", 0) + 1
            if fail_count < 110:
                kept.append({**p, "failures": fail_count})

    new_entries = [h for h in hits if h["addr"] not in known_addrs]
    # Ensure every new entry has a source tag — use specific URL source if set,
    # otherwise fall back to "keeper" (meaning found during keeper's fetch cycle)
    for h in new_entries:
        if not h.get("source"):
            h["source"] = "keeper"
    merged = kept + new_entries

    pruned = len(resample) - sum(1 for p in resample if p["addr"] in hit_addrs)
    log(f"  pruned {pruned} dead | +{len(new_entries)} new | master total {len(merged)}")
    atomic_write(MASTER, merged)
    log(f"=== Cycle #{cycle_num} done ===\n")


def main():
    log(f"proxy_keeper starting  cycle={CYCLE_MINUTES}min  workers={WORKERS}  resample={RESAMPLE_SIZE}  timeout={PROBE_TIMEOUT}s  probe=GET")
    cycle = 1
    while True:
        if not _ynet_reachable():
            log(f"  ⚠ Ynet DNS down — waiting 60s before retry (pool protected)")
            time.sleep(60)
            continue
        try:
            run_cycle(cycle)
        except Exception as e:
            log(f"cycle #{cycle} crashed: {e}")
        cycle += 1
        log(f"sleeping {CYCLE_MINUTES} min...")
        time.sleep(CYCLE_MINUTES * 60)


if __name__ == "__main__":
    main()
