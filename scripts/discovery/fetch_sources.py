#!/usr/bin/env python3
"""Fetch every public proxy list into scripts/discovery/sources/raw/.
Parallel HTTP pulls + per-source timeout. Writes one .txt per source.

After this, run build_candidates.py to merge+dedupe into candidates.txt.
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.request

REPO_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAW_DIR     = os.path.join(REPO_ROOT, "scripts", "discovery", "sources", "raw")
os.makedirs(RAW_DIR, exist_ok=True)

UA = {"User-Agent": "proxy-fetch/1.0"}

# Static raw GitHub lists — (out_name, scheme_hint, url)
STATIC = [
    ("speedx_http",        "http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("speedx_s4",          "socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("speedx_s5",          "socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("monosans_http",      "http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("monosans_s4",        "socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("monosans_s5",        "socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("mmpx12_http",        "http",   "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("mmpx12_s4",          "socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("mmpx12_s5",          "socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    ("roosterkid_http",    "http",   "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("roosterkid_s4",      "socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("roosterkid_s5",      "socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    ("jetkai_http",        "http",   "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("jetkai_s4",          "socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("jetkai_s5",          "socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    ("shiftytr_http",      "http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("shiftytr_s4",        "socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("shiftytr_s5",        "socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    ("clarketm_http",      "http",   "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    ("sunny9577_http",     "http",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    ("hookzof_s5",         "socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("proxifly_mixed",     "http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    ("zloi_http",          "http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/http.txt"),
    ("zloi_https",         "http",   "https://raw.githubusercontent.com/zloi-user/hideip.me/master/https.txt"),
    ("zloi_s4",            "socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks4.txt"),
    ("zloi_s5",            "socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt"),
    ("yakumo_http",        "http",   "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt"),
    ("yakumo_s4",          "socks4", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt"),
    ("yakumo_s5",          "socks5", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt"),
    ("vanndev_http",       "http",   "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt"),
    ("vanndev_s4",         "socks4", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks4.txt"),
    ("vanndev_s5",         "socks5", "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks5.txt"),
    ("prxchk_http",        "http",   "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("prxchk_s4",          "socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("prxchk_s5",          "socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),
    ("yemixzy_http",       "http",   "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/http.txt"),
    ("yemixzy_s4",         "socks4", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks4.txt"),
    ("yemixzy_s5",         "socks5", "https://raw.githubusercontent.com/yemixzy/proxy-list/master/proxies/socks5.txt"),
    ("murong_http",        "http",   "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("murong_s4",          "socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("murong_s5",          "socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    ("ercin_http",         "http",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("ercin_s4",           "socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("ercin_s5",           "socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),
    ("kang_http",          "http",   "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("kang_s4",            "socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("kang_s5",            "socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),
    ("b4_http",            "http",   "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt"),
    ("b4_s4",              "socks4", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt"),
    ("b4_s5",              "socks5", "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt"),
]


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def fetch_static(name, scheme, url):
    try:
        body = http_get(url)
        out_path = os.path.join(RAW_DIR, f"{name}.txt")
        with open(out_path, "w") as f:
            f.write(body)
        n = sum(1 for line in body.splitlines() if line.strip() and not line.startswith("#"))
        return (name, n, None)
    except Exception as e:
        return (name, 0, str(e)[:80])


def fetch_proxyscrape():
    lines = []
    for proto in ("http", "socks4", "socks5"):
        try:
            url = ("https://api.proxyscrape.com/v3/free-proxy-list/get"
                   f"?request=displayproxies&protocol={proto}"
                   "&timeout=10000&country=all&proxy_format=ipport&format=text")
            body = http_get(url)
            out_path = os.path.join(RAW_DIR, f"proxyscrape_{proto}.txt")
            with open(out_path, "w") as f:
                f.write(body)
            lines.append((f"proxyscrape_{proto}", sum(1 for l in body.splitlines() if l.strip())))
        except Exception as e:
            lines.append((f"proxyscrape_{proto}", 0, str(e)[:80]))
    return lines


def fetch_geonode():
    all_lines = []
    try:
        for page in range(1, 21):
            url = ("https://proxylist.geonode.com/api/proxy-list"
                   f"?limit=500&page={page}&sort_by=lastChecked&sort_type=desc")
            body = http_get(url)
            data = json.loads(body).get("data", [])
            if not data: break
            for p in data:
                addr = f"{p.get('ip')}:{p.get('port')}"
                for proto in p.get("protocols", []):
                    s = "http" if proto in ("http", "https") else proto
                    if s in ("http", "socks4", "socks5"):
                        all_lines.append(f"{s} {addr}")
        with open(os.path.join(RAW_DIR, "geonode.txt"), "w") as f:
            f.write("\n".join(all_lines))
        return [("geonode", len(all_lines))]
    except Exception as e:
        return [("geonode", 0, str(e)[:80])]


def fetch_pld():
    results = []
    for proto_query, proto_store in (("http", "http"), ("https", "http"),
                                     ("socks4", "socks4"), ("socks5", "socks5")):
        try:
            url = f"https://www.proxy-list.download/api/v1/get?type={proto_query}"
            body = http_get(url, timeout=20)
            out_path = os.path.join(RAW_DIR, f"pld_{proto_query}.txt")
            with open(out_path, "w") as f:
                f.write(body)
            results.append((f"pld_{proto_query}", sum(1 for l in body.splitlines() if l.strip())))
        except Exception as e:
            results.append((f"pld_{proto_query}", 0, str(e)[:80]))
    return results


def fetch_openproxy():
    results = []
    for proto in ("http", "socks4", "socks5"):
        try:
            url = f"https://api.openproxy.space/lists/{proto}"
            body = http_get(url)
            data = json.loads(body)
            lines = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and isinstance(entry.get("data"), list):
                        for a in entry["data"]:
                            a = str(a).strip()
                            if ":" in a: lines.append(a)
            with open(os.path.join(RAW_DIR, f"openproxy_{proto}.txt"), "w") as f:
                f.write("\n".join(lines))
            results.append((f"openproxy_{proto}", len(lines)))
        except Exception as e:
            results.append((f"openproxy_{proto}", 0, str(e)[:80]))
    return results


def main():
    total = 0
    errors = 0
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(fetch_static, *s) for s in STATIC]
        futures += [ex.submit(fetch_proxyscrape)]
        futures += [ex.submit(fetch_geonode)]
        futures += [ex.submit(fetch_pld)]
        futures += [ex.submit(fetch_openproxy)]
        for fut in cf.as_completed(futures):
            r = fut.result()
            if isinstance(r, list):
                for item in r:
                    name, n = item[0], item[1]
                    err = item[2] if len(item) > 2 else None
                    flag = "OK " if err is None else "ERR"
                    print(f"  [{flag}] {name:<30} {n:>7}  {err or ''}")
                    total += n
                    if err: errors += 1
            else:
                name, n, err = r
                flag = "OK " if err is None else "ERR"
                print(f"  [{flag}] {name:<30} {n:>7}  {err or ''}")
                total += n
                if err: errors += 1
    print(f"\nTotal raw lines: {total}   Sources with errors: {errors}")


if __name__ == "__main__":
    main()
