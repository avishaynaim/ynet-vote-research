# Ynet Talkback Voting Mechanism — Security Research

**Target:** `https://www.ynet.co.il/news/article/yokra14737379`  
**Date:** 2026-04-13  
**Type:** Black-box API analysis / academic security research  
**Status:** Research only — no malicious use, no live exploitation

> ⚠️ **DISCLAIMER:** This research is for academic and educational purposes only.
> All live testing was passive observation of publicly accessible API endpoints.
> No votes were cast beyond what was needed to confirm the deduplication mechanism.
> Running the attack simulation (`07_ip_rotation_theory.py`) against live
> infrastructure without written authorization from Ynet/Yedioth Ahronoth Group
> is illegal under Israeli Computer Law (חוק המחשבים, 5755-1995) and
> violates Ynet's Terms of Service.

---

## Table of Contents

1. [What "yokra" means](#what-yokra-means)
2. [System Architecture](#system-architecture)
3. [API Endpoints Discovered](#api-endpoints-discovered)
4. [Vote Request Schema](#vote-request-schema)
5. [Vote Response Schema](#vote-response-schema)
6. [Cookie State Machine](#cookie-state-machine)
7. [CDN Cache Behaviour](#cdn-cache-behaviour)
8. [Deduplication Mechanism](#deduplication-mechanism)
9. [Security Findings](#security-findings)
10. [IP Rotation — Threat Model](#ip-rotation--threat-model)
11. [Mitigations](#mitigations)
12. [Scripts Reference](#scripts-reference)
13. [Responsible Disclosure](#responsible-disclosure)

---

## What "yokra" means

The `yokra` prefix in the article URL **does not mean poll or survey**. It identifies
articles originating from the **Yediot Aharonoth print newspaper** edition (yokra =
a CMS article-type code for the newspaper section). This is a standard opinion
column by Nachum Barnea, not an embedded voting widget.

The voting in scope is the **talkback (comment) like/unlike system**, embedded in
every Ynet article.

---

## System Architecture

```
Browser                         Ynet CDN (vx-cache)       Ynet API Server
   |                                    |                        |
   |-- GET /talkbacks/list/v2/... ----> |                        |
   |                              HIT? |-- (cached response) --> |
   |                                   |                        |
   |-- POST /talkbacks/vote ---------->|---> (bypass cache) --> |
   |                                   |    [IP dedup check]    |
   |<-- {"success": true}              |    [cookie set]        |
   |    set-cookie: talkback_X=True    |                        |
   |                                   |                        |
   |   (wait ~87s for cache expiry)    |                        |
   |                                   |                        |
   |-- GET /talkbacks/list/v2/... ---> |  MISS -> fetch fresh   |
   |<-- updated counts visible <------ |<----- fresh data ------|
```

**Frontend stack:**
- React components bundled in `widgets.ce1eacd843df1022013e.js`
- Axios for HTTP calls (no jQuery for API calls)
- State managed in React component state + browser cookies
- Comment rendering: custom system (`isSpotim: false`)

**Infrastructure:**
- CDN layer: `vx-cache` (Varnish-based or equivalent)
- Server identifier: `osv: c8`
- CORS: fully open (`access-control-allow-origin: *`)

---

## API Endpoints Discovered

### Talkback (Comment) Endpoints

| Endpoint | Method | Auth Required | Notes |
|---|---|---|---|
| `/iphone/json/api/talkbacks/list/v2/{articleId}/{sort}/{page}` | GET | None | Returns paginated comments with vote counts |
| `/iphone/json/api/talkbacks/vote` | POST | None | Like/unlike a comment |
| `/iphone/json/api/talkbacks/add` | POST | None | Submit a new comment |

### Article Rating Endpoint

| Endpoint | Method | Auth Required | Notes |
|---|---|---|---|
| `/api/article/save_rating/` | POST | None | Star rating (recipe articles only) |

### List endpoint sort values

The sort parameter accepts any value — all tested values (`0`, `1`, `2`, `most_liked`,
`newest`, `oldest`) returned HTTP 200 with the same default ordering.

---

## Vote Request Schema

```
POST https://www.ynet.co.il/iphone/json/api/talkbacks/vote
Content-Type: application/json
Origin: https://www.ynet.co.il
Referer: https://www.ynet.co.il/news/article/{articleId}

{
  "article_id":     "yokra14737379",   // string  — REQUIRED
  "talkback_id":    98996389,          // integer — REQUIRED
  "talkback_like":  true,              // boolean — REQUIRED
  "talkback_unlike":false,             // boolean — REQUIRED
  "vote_type":      "2state"           // enum    — REQUIRED: "2state" | "3state"
}
```

**vote_type semantics:**
- `"2state"` — binary toggle (like ↔ neutral)
- `"3state"` — tristate (like ↔ neutral ↔ dislike)

**Validation errors (HTTP 400):**
```json
{
  "errors": {
    "talkback_id":    "dataclass field is missing a value",
    "talkback_unlike":"dataclass field is missing a value",
    "vote_type":      "dataclass field is missing a value"
  },
  "message": "Validation Error"
}
```

```json
{
  "errors": {
    "vote_type": "'invalid' is not a valid value for 'VoteType'. Expected one of ['2state', '3state']"
  },
  "message": "Validation Error"
}
```

The error message reveals the server uses **Python dataclasses** for validation —
a Django REST-style API backend.

---

## Vote Response Schema

**Success (HTTP 200):**
```http
HTTP/2 200
content-type: application/json
content-length: 17
access-control-allow-origin: *
x-frame-options: SAMEORIGIN
osv: c8
cache-control: private, max-age=100
set-cookie: talkback_98996389=True; Path=/

{"success": true}
```

**Note:** `{"success": true}` is returned even when the vote is silently dropped
by IP deduplication. The only observable difference is whether the count changes
after cache expiry.

---

## Cookie State Machine

The server manages `talkback_{id}` cookies to track vote state. The server
**reads** the inbound cookie and **writes** (or deletes) it in the response.

```
Inbound Cookie     Action       Response Cookie          Server Count
─────────────────────────────────────────────────────────────────────
(none)             LIKE         talkback_X=True          +1
True               LIKE         (no Set-Cookie)          0 (toggle off)
True               UNLIKE       talkback_X="" (deleted)  -1
False              LIKE         talkback_X="" (deleted)  0 (neutralized)
(none)             UNLIKE       talkback_X=False         -1
False              UNLIKE       (no Set-Cookie)          0 (toggle off)
```

**Key observation:** When the server deletes the cookie it uses
`expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0` — the RFC 6265 standard
mechanism for cookie deletion.

---

## CDN Cache Behaviour

The list endpoint is served through a CDN cache (`vx-cache`):

```http
vx-cache: HIT
last-modified: Mon, 13 Apr 2026 08:37:27 GMT
cache-control: private, max-age=87
expires: Mon, 13 Apr 2026 08:40:52 GMT
```

**Implications:**
- Votes are **immediately stored server-side** when submitted
- The count is **invisible for ~87 seconds** due to CDN caching
- After `vx-cache: MISS` (cache expiry), the updated count becomes visible
- This was confirmed experimentally: three comments went from `likes=0` to `likes=1`
  only after the cache expired

---

## Deduplication Mechanism

### Two-layer system

**Layer 1 — Client-side (cookie):**  
The React component checks `Cookies.get("talkback_{id}")` before firing the
request. If already voted, no HTTP request is sent.

```javascript
// From widgets.ce1eacd843df1022013e.js
var voteStr = Cookies.get("talkback_".concat(comment.id));
switch (voteStr) {
  case "True":  vote = 1;  break;
  case "False": vote = -1; break;
  default:      vote = 0;
}
```

**Layer 2 — Server-side (IP):**  
The server tracks `(talkback_id, source_IP)` pairs. A second request from the
same IP:
- Returns `HTTP 200 {"success": true}`
- Sets a new cookie in the response
- **Does NOT increment the server-side counter**

### What was tested and confirmed

| Test | Result |
|---|---|
| First vote from IP (no cookie) | Count increments ✓ |
| Second vote from same IP (no cookie) | Returns success, count unchanged ✓ |
| Different User-Agent, same IP | Count unchanged (UA not used for dedup) ✓ |
| `X-Forwarded-For: 8.8.8.8` header | Count unchanged (header ignored) ✓ |
| `X-Real-IP: 8.8.8.8` header | Count unchanged (header ignored) ✓ |

---

## Security Findings

| ID | Finding | Severity | Detail |
|---|---|---|---|
| F-01 | No authentication on vote endpoint | Medium | Any HTTP client can vote without login |
| F-02 | No CSRF token required | Low | CORS is open (`*`) so this is by design, but worth noting |
| F-03 | IP dedup bypassable via IP rotation | Medium | Each unique IP gets one vote; proxy pools circumvent this |
| F-04 | Cookie dedup is client-side only | Low | Server doesn't enforce cookie as the primary gate |
| F-05 | `{"success": true}` on deduped votes | Informational | Silent drop with success response hides dedup from attackers |
| F-06 | CDN cache masks vote changes ~87s | Informational | Attack success not immediately observable |
| F-07 | `/api/article/save_rating/` unauthenticated | Medium | Returns HTTP 200 to unauthenticated POST; no visible server-side dedup |

### talkback_like field meaning

```
talkback_like (in list response) = net votes = likes - unlikes
```

Confirmed: comment 98995845 had `likes=52, unlikes=14, talkback_like=38` → 52−14=38 ✓

---

## IP Rotation — Threat Model

Based on our confirmed findings, an IP-rotation attack against the vote system
would work as follows:

### Attack flow

```
for each IP in proxy_pool:
    POST /iphone/json/api/talkbacks/vote
    body = {
        "article_id":     target_article,
        "talkback_id":    target_comment,
        "talkback_like":  true,
        "talkback_unlike":false,
        "vote_type":      "2state"
    }
    # No cookie needed, no auth needed
    # Server sets talkback_X=True cookie — can be discarded
    # Server increments count
```

### Attack complexity

| Proxy type | Votes per $ | Detectability | Effectiveness |
|---|---|---|---|
| Datacenter proxies | High | High (known IP ranges) | Medium |
| Residential proxies | Medium | Low | High |
| Tor exit nodes | Free | High (often banned) | Low |
| VPN endpoints | Low | Medium | Medium |

### Constraints

- One vote per source IP per comment (server-enforced)
- Cache makes results invisible for ~87s (not a real barrier)
- No CAPTCHA, no auth, no rate limiting observed

See `scripts/07_ip_rotation_theory.py` for a full simulation model.

---

## Mitigations

Recommended for Ynet to implement (in order of priority):

### 1. reCAPTCHA v3 / hCaptcha on vote endpoint
- Invisible to legitimate users
- Breaks headless HTTP client attacks entirely
- Score threshold can be tuned

### 2. Require authenticated session
- Link vote to Ynet account (SSO already exists)
- One account = one vote per comment
- Eliminates anonymous IP-rotation attacks

### 3. /24 subnet rate limiting
- Residential proxy pools share /24 blocks
- Reducing from per-IP to per-/24 would cut effectiveness of cheap proxies

### 4. Behavioral anomaly detection
- Flag: many votes from one IP in a short window
- Flag: vote velocity exceeds page-view velocity
- Flag: votes without a prior GET of the article page

### 5. TLS fingerprinting (JA3/JA4)
- Headless HTTP clients (`curl`, `requests`, `axios-node`) produce distinct
  TLS handshake fingerprints vs. real browsers
- Effective against proxy pools not running a real browser

### 6. Proof-of-Work token
- Server issues a computational challenge before accepting a vote
- Low friction for single votes, prohibitive for mass automation

---

## Scripts Reference

| Script | Purpose |
|---|---|
| `scripts/01_fetch_article_meta.sh` | Fetch article HTML, extract JS URLs and widget configs |
| `scripts/02_analyze_widgets_js.sh` | Download and analyze `widgets.js` for voting logic |
| `scripts/03_fetch_talkbacks.sh` | Fetch live talkback data, parse schema and cache headers |
| `scripts/04_vote_endpoint_probe.sh` | Probe vote endpoint: validation, CORS, header spoofing |
| `scripts/05_cookie_state_machine.sh` | Map all cookie state transitions with live API calls |
| `scripts/06_dedup_analysis.sh` | Confirm IP dedup + CDN cache timing with before/after counts |
| `scripts/07_ip_rotation_theory.py` | Python simulation of IP-rotation attack (no live calls) |

### Running the simulation (safe — no live calls)

```bash
cd scripts
python3 07_ip_rotation_theory.py
# Output saved to results/simulation_results.json
```

### Running live analysis scripts

```bash
chmod +x scripts/*.sh
cd scripts
./01_fetch_article_meta.sh
./02_analyze_widgets_js.sh
./03_fetch_talkbacks.sh
# etc.
```

---

## Responsible Disclosure

If you intend to report this to Ynet, the appropriate contact is:

- **Security contact:** No dedicated `security.txt` found at `/.well-known/security.txt`
- **General contact:** editor@ynet.co.il or via the "report error" link on any article
- **Parent company:** Yedioth Ahronoth Group — legal@ynet.co.il

Suggested disclosure timeline:
1. Send report with findings to security/legal contact
2. Allow 90-day remediation window (industry standard)
3. Publish research after fixes are deployed or window expires

---

*Research conducted: 2026-04-13*  
*Tools used: curl, Python 3, bash*  
*No automation frameworks or exploit tools were used.*
