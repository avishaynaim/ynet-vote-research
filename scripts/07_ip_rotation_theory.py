#!/usr/bin/env python3
"""
Script 07: IP-rotation vote amplification model.

Demonstrates how an IP-rotation attack works against the Ynet talkback voting
system based on the deduplication behaviour we observed.
"""

import os
import time
import random
import json
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data model (mirrors what the real API expects)
# ---------------------------------------------------------------------------

@dataclass
class VoteRequest:
    article_id: str
    talkback_id: int
    talkback_like: bool
    talkback_unlike: bool
    vote_type: str = "2state"          # enum: "2state" | "3state"

    def to_json(self) -> str:
        return json.dumps({
            "article_id":     self.article_id,
            "talkback_id":    self.talkback_id,
            "talkback_like":  self.talkback_like,
            "talkback_unlike":self.talkback_unlike,
            "vote_type":      self.vote_type,
        })


@dataclass
class VoteResponse:
    http_status: int
    success: bool
    set_cookie: Optional[str]    # talkback_{id}=True / False / ""(deleted)


# ---------------------------------------------------------------------------
# Simulated IP pool
# ---------------------------------------------------------------------------

class IPPool:
    """
    Simulates a pool of source IPs.
    In a real attack this would be:
      - Residential proxies (e.g. Bright Data, Oxylabs, Smartproxy)
      - Tor exit nodes (rate-limited, often banned)
      - VPN endpoints (limited count, shared IPs)
      - Botnet C2
      - Cloud VMs with elastic IPs (traceable)
    """

    def __init__(self, ips: list[str]):
        self.ips = ips
        self._used: set[str] = set()   # tracks IPs that have already voted

    def get_fresh_ip(self) -> Optional[str]:
        available = [ip for ip in self.ips if ip not in self._used]
        if not available:
            return None
        ip = random.choice(available)
        self._used.add(ip)
        return ip

    @property
    def exhausted(self) -> bool:
        return len(self._used) >= len(self.ips)

    @property
    def votes_cast(self) -> int:
        return len(self._used)


# ---------------------------------------------------------------------------
# Voting engine
# ---------------------------------------------------------------------------

class VotingEngine:
    """
    Models the vote-and-check cycle.

    Observed real-world timings:
      - Vote endpoint response: ~200-400ms
      - List cache TTL: ~87 seconds (vx-cache HIT, private max-age)
      - After cache miss: updated count visible
    """

    VOTE_ENDPOINT   = "POST /iphone/json/api/talkbacks/vote"
    LIST_ENDPOINT   = "GET  /iphone/json/api/talkbacks/list/v2/{article_id}/0/1"
    CACHE_TTL_SEC   = 87      # observed CDN cache TTL
    REQUIRED_FIELDS = ["article_id", "talkback_id", "talkback_like",
                        "talkback_unlike", "vote_type"]
    VALID_VOTE_TYPES = {"2state", "3state"}

    def __init__(self, article_id: str, talkback_id: int, initial_likes: int = 0):
        self.article_id    = article_id
        self.talkback_id   = talkback_id
        self._real_likes   = initial_likes   # ground truth (server-side)
        self._cached_likes = initial_likes   # what the CDN serves
        self._last_cache_refresh = time.time()
        self._ip_vote_log: dict[str, bool] = {}   # ip -> voted

    def _refresh_cache_if_expired(self):
        if time.time() - self._last_cache_refresh > self.CACHE_TTL_SEC:
            self._cached_likes = self._real_likes
            self._last_cache_refresh = time.time()

    def get_visible_likes(self) -> int:
        """What a client sees when they call the list endpoint."""
        self._refresh_cache_if_expired()
        return self._cached_likes

    def submit_vote(self, req: VoteRequest, source_ip: str) -> VoteResponse:
        """
        Simulates server-side vote processing.

        Dedup logic (from observed behaviour):
          - If source_ip already voted -> accept the request, return success,
            but do NOT increment the counter.
          - Cookie state is managed but IP is the gate.
        """
        # Simulate validation
        if req.vote_type not in self.VALID_VOTE_TYPES:
            return VoteResponse(400, False, None)

        already_voted = source_ip in self._ip_vote_log

        cookie_value = None
        if req.talkback_like and not already_voted:
            self._real_likes += 1
            self._ip_vote_log[source_ip] = True
            cookie_value = f"talkback_{self.talkback_id}=True; Path=/"
        elif req.talkback_like and already_voted:
            # Returns success but silently drops the vote
            cookie_value = f"talkback_{self.talkback_id}=True; Path=/"
        elif req.talkback_unlike:
            cookie_value = (f"talkback_{self.talkback_id}=\"\"; "
                            "expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0; Path=/")

        return VoteResponse(200, True, cookie_value)


# ---------------------------------------------------------------------------
# Attack simulation
# ---------------------------------------------------------------------------

def simulate_ip_rotation_attack(
    article_id: str,
    talkback_id: int,
    ip_pool: IPPool,
    initial_likes: int = 5,
    delay_between_votes: float = 0.5,   # seconds
) -> dict:
    """
    Runs the attack and returns a report.
    """
    engine = VotingEngine(article_id, talkback_id, initial_likes)
    results = {
        "article_id":    article_id,
        "talkback_id":   talkback_id,
        "initial_likes": initial_likes,
        "votes_attempted": 0,
        "votes_accepted":  0,
        "votes_dropped":   0,
        "final_real_likes": 0,
        "final_visible_likes": 0,
        "ips_used": [],
        "log": [],
    }

    print(f"\n{'='*60}")
    print(f"SIMULATED IP-ROTATION ATTACK")
    print(f"Target: article={article_id}, comment={talkback_id}")
    print(f"IP pool size: {len(ip_pool.ips)}")
    print(f"Initial likes: {initial_likes}")
    print(f"{'='*60}\n")

    while not ip_pool.exhausted:
        ip = ip_pool.get_fresh_ip()
        if ip is None:
            break

        req = VoteRequest(
            article_id=article_id,
            talkback_id=talkback_id,
            talkback_like=True,
            talkback_unlike=False,
            vote_type="2state",
        )

        resp = engine.submit_vote(req, source_ip=ip)
        results["votes_attempted"] += 1

        visible = engine.get_visible_likes()
        real = engine._real_likes

        if resp.set_cookie and "True" in resp.set_cookie:
            results["votes_accepted"] += 1
            status = "ACCEPTED"
        else:
            results["votes_dropped"] += 1
            status = "DROPPED (IP dedup)"

        log_entry = {
            "ip": ip,
            "status": status,
            "real_likes": real,
            "visible_likes": visible,
        }
        results["log"].append(log_entry)
        results["ips_used"].append(ip)

        print(f"  IP: {ip:>15}  [{status:<22}]  "
              f"real={real}  visible={visible}")

        time.sleep(delay_between_votes)

    results["final_real_likes"]    = engine._real_likes
    results["final_visible_likes"] = engine.get_visible_likes()

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"  Votes attempted : {results['votes_attempted']}")
    print(f"  Votes accepted  : {results['votes_accepted']}")
    print(f"  Votes dropped   : {results['votes_dropped']}")
    print(f"  Initial likes   : {initial_likes}")
    print(f"  Final likes     : {results['final_real_likes']}")
    print(f"  Lift            : +{results['final_real_likes'] - initial_likes}")
    print(f"{'='*60}\n")

    return results


# ---------------------------------------------------------------------------
# Mitigation analysis
# ---------------------------------------------------------------------------

MITIGATIONS = """
=== MITIGATIONS AGAINST IP-ROTATION VOTE ABUSE ===

Current protections (observed):
  [✓] IP-based deduplication (server-side, effective against single-IP abuse)
  [✓] Cookie state tracking (client-side enforcement layer)
  [✗] No CAPTCHA on vote endpoint
  [✗] No authentication required
  [✗] No rate limiting per IP observed (X-Forwarded-For ignored correctly)
  [✗] No fingerprinting beyond IP
  [✗] CORS is fully open (access-control-allow-origin: *)

Recommended mitigations (in order of cost/effectiveness):
  1. CAPTCHA on first vote per session (hCaptcha / reCAPTCHA v3)
     - High friction for bots, low friction for real users (invisible)
     - Would break headless curl-based attacks completely

  2. Require authenticated session for voting
     - Link vote to Ynet account instead of IP
     - Eliminates anonymous IP-rotation attacks

  3. Rate limiting per /24 subnet (not just /32 IP)
     - Residential proxy pools often share /24 blocks
     - Would reduce effectiveness of cheap proxies

  4. Behavioral anomaly detection
     - Flag IPs that vote on many comments in short windows
     - Compare vote velocity vs. page view velocity

  5. Proof-of-Work token (server-issues a challenge, client must solve)
     - No user-visible friction
     - Computationally expensive for large-scale automation

  6. TLS fingerprinting (JA3/JA4)
     - Headless HTTP clients have distinct TLS fingerprints vs. browsers
     - Would catch curl/requests-based bots even through proxies

Attack complexity assessment:
  - Without mitigations: LOW (any proxy pool works)
  - With reCAPTCHA v3 + auth: HIGH (requires account farm + CAPTCHA solving)
  - With JA3 + behavioral: VERY HIGH (requires real browser automation per IP)
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Simulate a pool of 10 distinct IPs (e.g. 10 VPN endpoints / proxies)
    fake_ip_pool = IPPool([
        "203.0.113.1",   # TEST-NET — RFC 5737 documentation IPs
        "203.0.113.2",
        "203.0.113.3",
        "203.0.113.4",
        "203.0.113.5",
        "198.51.100.1",
        "198.51.100.2",
        "198.51.100.3",
        "198.51.100.4",
        "198.51.100.5",
    ])

    results = simulate_ip_rotation_attack(
        article_id="yokra14737379",
        talkback_id=98996389,
        ip_pool=fake_ip_pool,
        initial_likes=1,
        delay_between_votes=0.1,
    )

    print(MITIGATIONS)

    # Save results (path relative to this script's location)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(script_dir, "..", "results", "simulation_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Simulation results saved to {os.path.normpath(out_path)}")
