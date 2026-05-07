#!/usr/bin/env python3
"""Fetch additional public proxy lists beyond the original 31 in fetch_sources.py.
Writes raw files to sources/raw/<name>.txt. Run before build_candidates.py.
"""
import concurrent.futures as cf
import json, os, re, urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAW_DIR   = os.path.join(REPO_ROOT, "scripts", "discovery", "sources", "raw")
os.makedirs(RAW_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; proxy-fetch/2.0)"}

STATIC = [
    # Zaeem20 — https://github.com/Zaeem20/FREE_PROXIES_LIST
    ("zaeem_http",   "http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
    ("zaeem_https",  "http",   "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt"),
    ("zaeem_s4",     "socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt"),
    ("zaeem_s5",     "socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),
    # anonymousWork1221
    ("anon_http",    "http",   "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
    ("anon_s4",      "socks4", "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt"),
    ("anon_s5",      "socks5", "https://raw.githubusercontent.com/anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    # lalifeier
    ("lali_http",    "http",   "https://raw.githubusercontent.com/lalifeier/proxy-list/main/http.txt"),
    ("lali_https",   "http",   "https://raw.githubusercontent.com/lalifeier/proxy-list/main/https.txt"),
    ("lali_s4",      "socks4", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks4.txt"),
    ("lali_s5",      "socks5", "https://raw.githubusercontent.com/lalifeier/proxy-list/main/socks5.txt"),
    # zevtyardt
    ("zev_http",     "http",   "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("zev_s4",       "socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("zev_s5",       "socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),
    # saschazesiger
    ("sascha_http",  "http",   "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/http.txt"),
    ("sascha_s4",    "socks4", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks4.txt"),
    ("sascha_s5",    "socks5", "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/socks5.txt"),
    # fate0 proxylist (top-level JSON) — emit as http
    # handled separately
    # proxyspace
    ("prxspace_http","http",   "https://raw.githubusercontent.com/proxyspace/proxyspace/master/http.txt"),
    ("prxspace_https","http",  "https://raw.githubusercontent.com/proxyspace/proxyspace/master/https.txt"),
    ("prxspace_s4",  "socks4", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks4.txt"),
    ("prxspace_s5",  "socks5", "https://raw.githubusercontent.com/proxyspace/proxyspace/master/socks5.txt"),
    # a2u
    ("a2u_http",     "http",   "https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.txt"),
    # tuanminpay
    ("tuan_http",    "http",   "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt"),
    ("tuan_s4",      "socks4", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks4.txt"),
    ("tuan_s5",      "socks5", "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt"),
    # rdavydov (multi-protocol)
    ("rdavydov_http","http",   "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("rdavydov_s4",  "socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("rdavydov_s5",  "socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),
    # UserR3X
    ("userr3x_http", "http",   "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/http.txt"),
    ("userr3x_s4",   "socks4", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks4.txt"),
    ("userr3x_s5",   "socks5", "https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/socks5.txt"),
    # HyperBeats
    ("hyper_http",   "http",   "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt"),
    ("hyper_s4",     "socks4", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks4.txt"),
    ("hyper_s5",     "socks5", "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/socks5.txt"),
    # Volodichev
    ("volo_http",    "http",   "https://raw.githubusercontent.com/Volodichev/proxy-list/main/http.txt"),
    # ALIILAPRO
    ("alii_http",    "http",   "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
    ("alii_s4",      "socks4", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt"),
    ("alii_s5",      "socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),
    # r00tee
    ("r00t_http",    "http",   "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt"),
    ("r00t_s4",      "socks4", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt"),
    ("r00t_s5",      "socks5", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt"),
    # berkay-digital
    ("berkay_http",  "http",   "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/http.txt"),
    ("berkay_s4",    "socks4", "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/socks4.txt"),
    ("berkay_s5",    "socks5", "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies/socks5.txt"),
    # manuGMG
    ("manu_http",    "http",   "https://raw.githubusercontent.com/manuGMG/proxy-365/main/HTTP.txt"),
    ("manu_s4",      "socks4", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS4.txt"),
    ("manu_s5",      "socks5", "https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS5.txt"),
    # sheepkiller-dev
    ("sheep_http",   "http",   "https://raw.githubusercontent.com/sheepkiller-dev/Proxies-List/main/proxies.txt"),
    # mishakorzik
    ("misha_http",   "http",   "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/http.txt"),
    ("misha_s4",     "socks4", "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/socks4.txt"),
    ("misha_s5",     "socks5", "https://raw.githubusercontent.com/MishaKorzhik/He-Proxy/master/socks5.txt"),
    # MrMarble
    ("mrmar_http",   "http",   "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt"),
    # proxyShield
    ("pshield_http", "http",   "https://raw.githubusercontent.com/ShieldStaff/proxy/main/http.txt"),
    # TuanchauIT
    ("tuanch_http",  "http",   "https://raw.githubusercontent.com/TuanchauIT/ProxyChecker/main/proxy.txt"),
    # mertguvencli
    ("mert_http",    "http",   "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt"),
    # dpangestuw
    ("dpang_http",   "http",   "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
    ("dpang_s4",     "socks4", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt"),
    ("dpang_s5",     "socks5", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt"),
    # miroslavpejic85
    ("miro_http",    "http",   "https://raw.githubusercontent.com/miroslavpejic85/proxy-list/main/proxy-list-raw.txt"),
    # PROXY-List mirrors
    ("roost_https",  "http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    # Additional ProxyScrape variants
    # handled separately
]

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")

def fetch_static(name, scheme, url):
    try:
        body = http_get(url)
        with open(os.path.join(RAW_DIR, f"{name}.txt"), "w") as f:
            f.write(body)
        n = sum(1 for l in body.splitlines() if l.strip() and not l.startswith("#"))
        return (name, n, None)
    except Exception as e:
        return (name, 0, str(e)[:80])

def fetch_fate0():
    """fate0/proxylist emits JSONL, one record per line."""
    try:
        body = http_get("https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list")
        lines = []
        for line in body.splitlines():
            line = line.strip()
            if not line: continue
            try:
                r = json.loads(line)
                host = r.get("host"); port = r.get("port")
                t = r.get("type", "http")
                if t in ("https",): t = "http"
                if t not in ("http","socks4","socks5"): t = "http"
                if host and port:
                    lines.append(f"{t} {host}:{port}")
            except Exception:
                continue
        with open(os.path.join(RAW_DIR, "fate0.txt"), "w") as f:
            f.write("\n".join(lines))
        return ("fate0", len(lines), None)
    except Exception as e:
        return ("fate0", 0, str(e)[:80])

def fetch_proxyscrape_extended():
    """Broader ProxyScrape fetch — country=all, timeout higher, ssl/anonymity filters off."""
    out = []
    bases = [
        ("px_all_http",   "http",   "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=20000&country=all&ssl=all&anonymity=all"),
        ("px_all_s4",     "socks4", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=20000&country=all"),
        ("px_all_s5",     "socks5", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=20000&country=all"),
    ]
    for name, scheme, url in bases:
        try:
            body = http_get(url, 30)
            with open(os.path.join(RAW_DIR, f"{name}.txt"), "w") as f:
                f.write(body)
            n = sum(1 for l in body.splitlines() if l.strip())
            out.append((name, n, None))
        except Exception as e:
            out.append((name, 0, str(e)[:80]))
    return out

def fetch_advanced_name():
    """advanced.name — /proxies.txt (daily)"""
    try:
        body = http_get("https://advanced.name/freeproxy?type=http&export=txt")
        with open(os.path.join(RAW_DIR, "advanced_http.txt"), "w") as f:
            f.write(body)
        n = sum(1 for l in body.splitlines() if l.strip())
        return ("advanced_http", n, None)
    except Exception as e:
        return ("advanced_http", 0, str(e)[:80])

def fetch_proxynova():
    """Country-agnostic aggregator; scrape HTML table."""
    try:
        body = http_get("https://www.proxynova.com/proxy-server-list/", 30)
        # rows contain <abbr title="..."> or document.write scrapings; fallback regex
        hosts = re.findall(r"document\.write\('([0-9.]+)'\s*\+\s*'([0-9.]+)'\);\s*</script>\s*</td>\s*<td[^>]*>\s*<a[^>]*>\s*(\d+)", body)
        # simpler: ip:port pairs anywhere in the body
        pairs = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\s*[:.]?\s*(?:</[^>]+>\s*<[^>]+>\s*)?(\d{2,5})\b", body)
        lines = [f"http {ip}:{port}" for ip, port in pairs]
        with open(os.path.join(RAW_DIR, "proxynova.txt"), "w") as f:
            f.write("\n".join(lines))
        return ("proxynova", len(lines), None)
    except Exception as e:
        return ("proxynova", 0, str(e)[:80])

def fetch_freeproxylist_net():
    """free-proxy-list.net HTML scrape"""
    out = []
    for url, name in [
        ("https://free-proxy-list.net/", "fpl_main"),
        ("https://www.sslproxies.org/", "fpl_ssl"),
        ("https://www.us-proxy.org/", "fpl_us"),
        ("https://free-proxy-list.net/uk-proxy.html", "fpl_uk"),
        ("https://free-proxy-list.net/anonymous-proxy.html", "fpl_anon"),
        ("https://www.socks-proxy.net/", "fpl_socks"),
    ]:
        try:
            body = http_get(url, 20)
            pairs = re.findall(r"<td>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td>\s*(\d{2,5})\s*</td>", body)
            scheme = "socks5" if "socks" in name else "http"
            lines = [f"{scheme} {ip}:{port}" for ip, port in pairs]
            with open(os.path.join(RAW_DIR, f"{name}.txt"), "w") as f:
                f.write("\n".join(lines))
            out.append((name, len(lines), None))
        except Exception as e:
            out.append((name, 0, str(e)[:80]))
    return out

def main():
    total = 0; errors = 0
    with cf.ThreadPoolExecutor(max_workers=25) as ex:
        futs = [ex.submit(fetch_static, *s) for s in STATIC]
        futs += [ex.submit(fetch_fate0)]
        futs += [ex.submit(fetch_proxyscrape_extended)]
        futs += [ex.submit(fetch_advanced_name)]
        futs += [ex.submit(fetch_proxynova)]
        futs += [ex.submit(fetch_freeproxylist_net)]
        for fut in cf.as_completed(futs):
            r = fut.result()
            items = r if isinstance(r, list) else [r]
            for item in items:
                name, n = item[0], item[1]
                err = item[2] if len(item) > 2 else None
                flag = "OK " if err is None else "ERR"
                print(f"  [{flag}] {name:<20} {n:>7}  {err or ''}")
                total += n
                if err: errors += 1
    print(f"\nTotal new raw lines: {total}   errors: {errors}")

if __name__ == "__main__":
    main()
