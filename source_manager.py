#!/usr/bin/env python3
"""
Source-aware proxy registry.

Each proxy source (GitHub repo, API endpoint, scrape page) gets a score
based on real YNET vote success/failure:
  - proxy from source succeeds against YNET → source score +1
  - proxy from source fails / connection error → source score -1

Bad sources are periodically RESET (not permanently blacklisted) so they
get a fresh chance after RESET_COOLDOWN hours. This handles sites that
temporarily go offline or get new IP pools.

Usage:
    from source_manager import SourceManager, get
    sm = get()                          # singleton
    sm.record_result("1.2.3.4:1080", success=True)
    sm.print_leaderboard()
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone

REPO          = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(REPO, "proxies", "source_registry.json")
POOL_PATH     = os.path.join(REPO, "proxies", "master_pool.json")

RESET_THRESHOLD = -20   # score below this → eligible for reset
RESET_COOLDOWN  = 6     # hours idle before a bad source gets a second chance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceManager:
    def __init__(self, registry_path=REGISTRY_PATH, pool_path=POOL_PATH):
        self.registry_path = registry_path
        self.pool_path     = pool_path
        self._lock         = threading.Lock()
        self._registry     = self._load_registry()
        self._addr_cache:  dict[str, str] = {}
        self._cache_mtime: float = 0.0

    # ── persistence ───────────────────────────────────────────────────────────

    def _load_registry(self) -> dict:
        if os.path.exists(self.registry_path):
            try:
                return json.load(open(self.registry_path, encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self):
        """Write registry to disk, merging additive counters from the on-disk state first.

        Multiple processes (proxy_keeper, mega_harvest) share the same registry file.
        Each process has its own in-memory state loaded at startup.  Without merging,
        a slower-updating process can overwrite probe_hits/harvested increments made
        by a faster process.  We merge additive fields from disk before each write so
        increments from other processes are never lost.
        """
        # ADDITIVE fields — take max(in_memory, on_disk) so neither process rewinds the other
        ADDITIVE = ("harvested", "probe_hits", "tested", "succeeded", "failed", "score")
        try:
            on_disk = json.load(open(self.registry_path, encoding="utf-8"))
            for key, disk_entry in on_disk.items():
                if key in self._registry:
                    mem = self._registry[key]
                    for field in ADDITIVE:
                        dv = disk_entry.get(field, 0) or 0
                        mv = mem.get(field, 0) or 0
                        if dv > mv:
                            mem[field] = dv
                else:
                    # Entry exists on disk but not in memory — keep it
                    self._registry[key] = disk_entry
        except Exception:
            pass  # If disk read fails, proceed with in-memory state

        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        tmp = self.registry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._registry, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.registry_path)

    # ── source registration ───────────────────────────────────────────────────

    def ensure_source(self, key: str, url: str = "", scheme: str = "", category: str = ""):
        """Create registry entry for a source if it doesn't exist yet."""
        with self._lock:
            if key not in self._registry:
                self._registry[key] = {
                    "url":            url,
                    "scheme":         scheme,
                    "category":       category,
                    "score":          0,
                    "harvested":      0,
                    "probe_hits":     0,
                    "tested":         0,
                    "succeeded":      0,
                    "failed":         0,
                    "last_harvested": None,
                    "last_result":    None,
                    "reset_count":    0,
                }
                self._save()

    # ── harvest tracking ──────────────────────────────────────────────────────

    def record_harvest(self, key: str, count: int):
        """Called after fetching from a source — count = raw proxies found."""
        with self._lock:
            if key not in self._registry:
                return
            self._registry[key]["harvested"] = self._registry[key].get("harvested", 0) + count
            self._registry[key]["last_harvested"] = _now()
            self._save()

    # ── probe hit tracking ────────────────────────────────────────────────────

    def record_probe_hit(self, key: str):
        """Called when a proxy from this source passes the YNET probe (HIT).

        A probe_hit means the proxy reached YNET and returned a valid response —
        higher quality signal than just being harvested from the source list.
        hit_rate = probe_hits / harvested * 100 shows source quality.
        """
        if not key:
            return
        with self._lock:
            if key not in self._registry:
                return
            self._registry[key]["probe_hits"] = self._registry[key].get("probe_hits", 0) + 1
            self._save()

    # ── addr → source lookup (cached) ─────────────────────────────────────────

    def _refresh_addr_cache(self):
        try:
            mtime = os.path.getmtime(self.pool_path)
        except OSError:
            return
        if mtime <= self._cache_mtime:
            return
        try:
            pool = json.load(open(self.pool_path, encoding="utf-8"))
            self._addr_cache = {
                p["addr"]: p["source"]
                for p in pool
                if p.get("addr") and p.get("source")
            }
            self._cache_mtime = mtime
        except Exception:
            pass

    # ── vote result tracking ──────────────────────────────────────────────────

    def record_result(self, addr: str, success: bool):
        """Update score of the source that produced this proxy.

        Call this after every YNET vote attempt:
            sm.record_result(proxy_addr, success=(http_status == 200))
        """
        with self._lock:
            self._refresh_addr_cache()
            key = self._addr_cache.get(addr)
            if not key or key not in self._registry:
                return
            src = self._registry[key]
            src["tested"]      = src.get("tested", 0) + 1
            src["last_result"] = _now()
            if success:
                src["succeeded"] = src.get("succeeded", 0) + 1
                src["score"]     = src.get("score", 0) + 1
            else:
                src["failed"] = src.get("failed", 0) + 1
                src["score"]  = src.get("score", 0) - 1
            self._save()

    # ── second-chance reset logic ─────────────────────────────────────────────

    def maybe_reset_bad_sources(self) -> int:
        """Reset sources that have been below threshold for RESET_COOLDOWN hours.

        Returns number of sources reset. Sources are never permanently dropped —
        they might have gotten fresh IPs since they last failed.
        """
        now = datetime.now(timezone.utc)
        reset_n = 0
        with self._lock:
            for key, info in self._registry.items():
                if info.get("score", 0) >= RESET_THRESHOLD:
                    continue
                lh = info.get("last_harvested")
                if lh:
                    try:
                        age_h = (
                            now - datetime.fromisoformat(lh).replace(tzinfo=timezone.utc)
                        ).total_seconds() / 3600
                        if age_h < RESET_COOLDOWN:
                            continue
                    except Exception:
                        pass
                self._registry[key]["score"]       = 0
                self._registry[key]["reset_count"] = info.get("reset_count", 0) + 1
                reset_n += 1
            if reset_n:
                self._save()
        return reset_n

    # ── reporting ─────────────────────────────────────────────────────────────

    def ranked_sources(self) -> list:
        with self._lock:
            return sorted(
                list(self._registry.items()),
                key=lambda x: x[1].get("score", 0),
                reverse=True,
            )

    def print_leaderboard(self, n: int = 25):
        rows = self.ranked_sources()
        print(f"\n  {'Source':<32} {'score':>6} {'ok':>5} {'fail':>5} {'tested':>6} {'harvested':>9}")
        print("  " + "-" * 67)
        for key, info in rows[:n]:
            print(f"  {key:<32} {info.get('score',0):>6} "
                  f"{info.get('succeeded',0):>5} {info.get('failed',0):>5} "
                  f"{info.get('tested',0):>6} {info.get('harvested',0):>9}")
        pos = sum(1 for _, v in rows if v.get("score", 0) > 0)
        neg = sum(1 for _, v in rows if v.get("score", 0) < 0)
        print(f"\n  Total: {len(rows)} sources | {pos} positive | {neg} negative | "
              f"{len(rows)-pos-neg} neutral")

    def alive_yield_ratio(self, alive_path: str = "") -> dict:
        """Return per-source alive-proxy counts and yield ratios.

        Reads alive.json to count how many currently-alive proxies came from
        each source. Returns dict of source_key → {alive, harvested, ratio}.
        """
        if not alive_path:
            alive_path = os.path.join(REPO, "proxies", "alive.json")
        try:
            alive = json.load(open(alive_path, encoding="utf-8"))
        except Exception:
            return {}
        from collections import Counter
        c = Counter(p["source"] for p in alive if p.get("source"))
        result = {}
        with self._lock:
            for key, alive_count in c.items():
                harvested = self._registry.get(key, {}).get("harvested", 0)
                ratio = alive_count / harvested if harvested > 0 else 0.0
                result[key] = {
                    "alive":     alive_count,
                    "harvested": harvested,
                    "ratio":     ratio,
                }
        return result

    def print_leaderboard_extended(self, n: int = 25):
        """Extended leaderboard with ok_rate, probe_hits, hit_rate and alive count columns.

        Columns:
          probe_hits — count of proxies from this source that passed YNET probe
          hit_rate   — probe_hits / harvested * 100 (source quality signal)
          ok_rate    — succeeded / tested (vote success rate, populated later)
          alive      — currently alive proxies from this source
        """
        rows = self.ranked_sources()
        alive_data = self.alive_yield_ratio()
        print(f"\n  {'Source':<32} {'score':>6} {'ok':>5} {'fail':>5} {'tested':>6} {'harvested':>9} {'p_hits':>7} {'hit%':>6} {'ok_rate':>8} {'alive':>6}")
        print("  " + "-" * 98)
        for key, info in rows[:n]:
            tested = info.get("tested", 0)
            succeeded = info.get("succeeded", 0)
            harvested = info.get("harvested", 0)
            probe_hits = info.get("probe_hits", 0)
            ok_rate = f"{succeeded/tested:.1%}" if tested > 0 else "—"
            hit_rate = f"{probe_hits/harvested*100:.1f}%" if harvested > 0 and probe_hits > 0 else "—"
            alive_count = alive_data.get(key, {}).get("alive", 0)
            alive_str = str(alive_count) if alive_count > 0 else "—"
            print(f"  {key:<32} {info.get('score',0):>6} "
                  f"{succeeded:>5} {info.get('failed',0):>5} "
                  f"{tested:>6} {harvested:>9} {probe_hits:>7} {hit_rate:>6} {ok_rate:>8} {alive_str:>6}")
        pos = sum(1 for _, v in rows if v.get("score", 0) > 0)
        neg = sum(1 for _, v in rows if v.get("score", 0) < 0)
        print(f"\n  Total: {len(rows)} sources | {pos} positive | {neg} negative | "
              f"{len(rows)-pos-neg} neutral")

    def stats(self) -> dict:
        with self._lock:
            r = self._registry
        return {
            "total":    len(r),
            "positive": sum(1 for v in r.values() if v.get("score", 0) > 0),
            "negative": sum(1 for v in r.values() if v.get("score", 0) < 0),
            "neutral":  sum(1 for v in r.values() if v.get("score", 0) == 0),
        }

    def add_source(self, key: str, url: str, scheme: str = "mixed", category: str = "discovered") -> bool:
        """Add a brand-new source (e.g. discovered by the agent). Returns True if new."""
        with self._lock:
            if key in self._registry:
                return False
            self._registry[key] = {
                "url":            url,
                "scheme":         scheme,
                "category":       category,
                "score":          0,
                "harvested":      0,
                "probe_hits":     0,
                "tested":         0,
                "succeeded":      0,
                "failed":         0,
                "last_harvested": None,
                "last_result":    None,
                "reset_count":    0,
            }
            self._save()
            return True


# ── singleton ─────────────────────────────────────────────────────────────────

_default: SourceManager | None = None


def get() -> SourceManager:
    global _default
    if _default is None:
        _default = SourceManager()
    return _default


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sm = get()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "leaderboard"
    if cmd == "leaderboard":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        sm.print_leaderboard(n)
    elif cmd == "leaderboard_ext":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 25
        sm.print_leaderboard_extended(n)
    elif cmd == "stats":
        print(json.dumps(sm.stats(), indent=2))
    elif cmd == "reset":
        n = sm.maybe_reset_bad_sources()
        print(f"Reset {n} bad sources")
    elif cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: source_manager.py add <key> <url> [scheme]")
            sys.exit(1)
        key, url = sys.argv[2], sys.argv[3]
        scheme = sys.argv[4] if len(sys.argv) > 4 else "mixed"
        added = sm.add_source(key, url, scheme, "manual")
        print(f"{'Added' if added else 'Already exists'}: {key}")
    else:
        print(f"Commands: leaderboard [n], leaderboard_ext [n], stats, reset, add <key> <url> [scheme]")
