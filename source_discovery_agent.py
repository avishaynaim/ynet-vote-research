#!/usr/bin/env python3
"""
Source Discovery Agent — finds new proxy list URLs and adds them to the registry.

Searches GitHub for repositories that publish fresh proxy lists, validates that
they actually return ip:port data, and registers new ones in source_registry.json.

Runs automatically at the start of each harvest cycle, so the source pool grows
over time without manual intervention.

Usage:
    python3 source_discovery_agent.py           # discover and add new sources
    python3 source_discovery_agent.py --dry-run # show what would be added
    python3 source_discovery_agent.py --report  # show current registry stats
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import source_manager as _sm

UA = {"User-Agent": "Mozilla/5.0 (compatible; proxy-discovery/1.0)"}
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

IP_PORT_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}")

# ── known file patterns inside a GitHub repo that contain ip:port lists ────────
PROXY_FILE_PATTERNS = [
    "http.txt", "https.txt", "socks4.txt", "socks5.txt",
    "HTTP.txt", "HTTPS.txt", "SOCKS4.txt", "SOCKS5.txt",
    "proxies/http.txt", "proxies/https.txt",
    "proxies/socks4.txt", "proxies/socks5.txt",
    "proxy_files/http_proxies.txt",
    "proxy_files/socks4_proxies.txt",
    "proxy_files/socks5_proxies.txt",
    "all.txt", "proxy.txt", "proxy-list-raw.txt",
    "results/http/global/http_checked.txt",
    "results/socks4/global/socks4_checked.txt",
    "results/socks5/global/socks5_checked.txt",
    "online/http.txt", "online/socks4.txt", "online/socks5.txt",
    "online-proxies/txt/proxies-http.txt",
    "online-proxies/txt/proxies-socks4.txt",
    "online-proxies/txt/proxies-socks5.txt",
    "generated/http_proxies.txt", "generated/socks4_proxies.txt", "generated/socks5_proxies.txt",
]

SCHEME_HINTS = {
    "http.txt": "http",   "https.txt": "http",
    "HTTP.txt": "http",   "HTTPS.txt": "http",
    "socks4.txt": "socks4", "SOCKS4.txt": "socks4",
    "socks5.txt": "socks5", "SOCKS5.txt": "socks5",
}


# ── GitHub search ─────────────────────────────────────────────────────────────

def gh_get(url: str, timeout: int = 20) -> dict | list:
    headers = dict(UA)
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def search_github_repos(queries: list[str], per_page: int = 30) -> list[dict]:
    """Search GitHub for repos likely to contain proxy lists."""
    repos = {}
    for q in queries:
        try:
            url = (f"https://api.github.com/search/repositories"
                   f"?q={urllib.parse.quote(q)}&sort=updated&order=desc&per_page={per_page}")
            data = gh_get(url)
            for item in data.get("items", []):
                full_name = item["full_name"]
                if full_name not in repos:
                    repos[full_name] = item
            time.sleep(1.2)  # GitHub rate limit: 10 req/min unauthenticated
        except Exception as e:
            print(f"  [search] {q}: {e}")
    return list(repos.values())


# ── URL validation ────────────────────────────────────────────────────────────

def _fetch_raw(url: str, timeout: int = 15) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(8192).decode(errors="replace")
    except Exception:
        return None


def validate_proxy_url(raw_url: str, min_ips: int = 10) -> tuple[bool, int]:
    """Return (valid, ip_count) — valid if at least min_ips ip:port found."""
    body = _fetch_raw(raw_url)
    if not body:
        return False, 0
    ips = IP_PORT_RE.findall(body)
    return len(ips) >= min_ips, len(ips)


def probe_repo_for_proxy_files(repo_full_name: str) -> list[tuple[str, str, str]]:
    """Try each known file pattern for a repo and return (key, url, scheme) for each that works.

    key   = safe identifier for the source (e.g. "owner_repo_http")
    url   = raw.githubusercontent.com URL
    scheme = http / socks4 / socks5
    """
    owner, repo = repo_full_name.split("/", 1)
    # Try to find default branch
    try:
        info = gh_get(f"https://api.github.com/repos/{repo_full_name}", timeout=10)
        default_branch = info.get("default_branch", "main")
    except Exception:
        default_branch = "main"

    found = []
    for pattern in PROXY_FILE_PATTERNS:
        basename = os.path.basename(pattern)
        scheme = SCHEME_HINTS.get(basename, "http")
        url = (f"https://raw.githubusercontent.com/{repo_full_name}"
               f"/{default_branch}/{pattern}")
        valid, count = validate_proxy_url(url, min_ips=20)
        if valid:
            safe_owner = re.sub(r"[^a-zA-Z0-9_]", "_", owner).lower()
            safe_repo  = re.sub(r"[^a-zA-Z0-9_]", "_", repo).lower()
            safe_pat   = re.sub(r"[^a-zA-Z0-9_]", "_", basename).rstrip("_txt").rstrip("_")
            key = f"{safe_owner}_{safe_repo}_{safe_pat}"[:50]
            found.append((key, url, scheme, count))
    return found


# ── known static API endpoints to verify ─────────────────────────────────────

STATIC_ENDPOINTS = [
    # (key, url, scheme)
    ("proxyscrape_v3_http",  "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&proxy_format=ipport&format=text", "http"),
    ("proxyscrape_v3_s4",    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks4&proxy_format=ipport&format=text", "socks4"),
    ("proxyscrape_v3_s5",    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&proxy_format=ipport&format=text", "socks5"),
    ("openproxyspace_http",  "https://openproxy.space/list/http", "http"),
    ("openproxyspace_socks4","https://openproxy.space/list/socks4", "socks4"),
    ("openproxyspace_socks5","https://openproxy.space/list/socks5", "socks5"),
    ("hidemy_http",          "https://hidemy.name/en/proxy-list/?type=h#list", "http"),
    ("spys_proxy_txt",       "https://spys.me/proxy.txt", "http"),
    ("spys_socks_txt",       "https://spys.me/socks.txt", "socks5"),
    ("pubproxy_http",        "http://pubproxy.com/api/proxy?format=txt&type=http&limit=100", "http"),
    ("pubproxy_s5",          "http://pubproxy.com/api/proxy?format=txt&type=socks5&limit=100", "socks5"),
    ("geonode_api_http",     "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http", "http"),
    ("proxylist_download_http",  "https://www.proxy-list.download/api/v1/get?type=http", "http"),
    ("proxylist_download_s4",    "https://www.proxy-list.download/api/v1/get?type=socks4", "socks4"),
    ("proxylist_download_s5",    "https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
]

GITHUB_SEARCH_QUERIES = [
    "proxy list http socks5 updated:>2025-01-01",
    "free proxy list socks4 txt updated:>2025-01-01",
    "proxy-scraper automatic updated:>2025-01-01",
    "fresh proxy list daily updated:>2025-06-01",
    "working proxies http socks updated:>2025-06-01",
]

import urllib.parse


def run_discovery(dry_run: bool = False, verbose: bool = True) -> int:
    sm = _sm.get()
    added = 0

    # 1. Verify known static endpoints
    if verbose:
        print("\n── Checking static API endpoints ──")
    for key, url, scheme in STATIC_ENDPOINTS:
        valid, count = validate_proxy_url(url, min_ips=10)
        if valid:
            if verbose:
                print(f"  OK   {key:<35} {count:>5} IPs  {url[:60]}")
            if not dry_run:
                if sm.add_source(key, url, scheme, "api"):
                    added += 1
                    if verbose:
                        print(f"       → ADDED to registry")
        else:
            if verbose:
                print(f"  FAIL {key:<35}  (no valid ip:port data)")

    # 2. Search GitHub for new repos
    if verbose:
        print("\n── Searching GitHub for new proxy repos ──")
    try:
        repos = search_github_repos(GITHUB_SEARCH_QUERIES, per_page=20)
        if verbose:
            print(f"  Found {len(repos)} candidate repos")

        def _probe_repo(repo):
            try:
                return probe_repo_for_proxy_files(repo["full_name"])
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_probe_repo, r): r for r in repos}
            for fut in as_completed(futs):
                repo = futs[fut]
                for key, url, scheme, count in (fut.result() or []):
                    if verbose:
                        print(f"  FOUND {key:<35} {count:>5} IPs  [{repo['full_name']}]")
                    if not dry_run:
                        if sm.add_source(key, url, scheme, "github_discovered"):
                            added += 1
                            if verbose:
                                print(f"        → ADDED to registry")
    except Exception as e:
        if verbose:
            print(f"  GitHub search failed: {e}")

    if verbose:
        print(f"\n  Discovery complete — {added} new sources added")
        sm.print_leaderboard(15)
    return added


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Proxy source discovery agent")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be added without writing to registry")
    ap.add_argument("--report", action="store_true",
                    help="Just print current registry leaderboard")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.report:
        sm = _sm.get()
        sm.print_leaderboard(50)
        print("\nStats:", json.dumps(sm.stats(), indent=2))
        sys.exit(0)

    n = run_discovery(dry_run=args.dry_run, verbose=not args.quiet)
    sys.exit(0 if n >= 0 else 1)
