# Ynet Vote API — Investigation Findings
Date: 2026-05-11 (updated after full test suite)

## Endpoint
```
POST https://www.ynet.co.il/iphone/json/api/talkbacks/vote
Content-Type: application/json
Origin: https://www.ynet.co.il   ← REQUIRED (see blocking section)

{
  "article_id":      "<article_id>",
  "talkback_id":     <int>,
  "talkback_like":   true,
  "talkback_unlike": false,
  "vote_type":       "2state"
}
```

## Success response
```
HTTP 200
{"success": true}
Set-Cookie: talkback_<id>=True; Path=/
Cache-Control: private, max-age=100
```
Every 200 response body is always `{"success": true}` — no soft rejects at the
HTTP layer. BUT: a 200 does not guarantee the counter moved *within the next poll
window*. It means the vote was accepted; the counter update may be delayed
(see latency section below).

## Counter update latency — VARIABLE
**Range: 60s–300s+**, not a fixed value.

- High-activity talkbacks (many existing votes): updates in ~60–90s
- Low-activity talkbacks (0–2 existing votes): updates in 90–300s+

The `Cache-Control: max-age=100` on the vote response is a lower bound, not a
guarantee. Do not conclude a vote was dropped just because the counter hasn't
moved after 90s — wait at least 5 minutes before deciding a 200 was wasted.

Verified: votes that appeared to produce 0 delta during 3-minute polling windows
all showed the expected delta when re-checked 5–10 minutes later.

## Deduplication — per (IP, talkback_id, vote_type)

**The dedup key is the combination of exit IP + talkback_id + vote_type (like/unlike).**

Verified scenarios (each tested on a fresh talkback with fresh proxy):

| Scenario                                  | HTTP 200s | Δlikes | Δunlikes | Verdict             |
|-------------------------------------------|-----------|--------|----------|---------------------|
| 1 proxy, 1 like                           | 1         | +1     | 0        | Counted             |
| 1 proxy, 2 likes, 0s gap (same talkback)  | 2         | +1     | 0        | Only 1st counted    |
| 1 proxy, 2 likes, 6s gap (same talkback)  | 2         | +1     | 0        | Only 1st counted    |
| 1 proxy, 2 likes, 60s gap (same talkback) | 2         | +1     | 0        | Only 1st counted    |
| 1 proxy, 10 rapid likes (same talkback)   | 10        | +1     | 0        | Only 1st counted; all 10 accepted HTTP 200, no flood blocking |
| 1 proxy, like then dislike (same talkback)| 2         | +1     | +1       | BOTH counted        |
| 1 proxy, dislike then like (same talkback)| 2         | +1     | +1       | BOTH counted        |
| 1 proxy, like on 2 DIFFERENT talkbacks    | 2         | +1+1   | 0        | BOTH counted        |
| 2 different proxies, 1 like each          | 2         | +2     | 0        | BOTH counted        |

**Key rules:**
- Same IP can vote LIKE on a talkback exactly once.
- Same IP can also vote UNLIKE on the same talkback exactly once.
- Like and unlike are tracked independently — both can count from the same IP.
- Same IP voting the same type twice (like+like) = only first counts; server
  returns HTTP 200 for the duplicate but silently discards it.
- Same IP can vote on unlimited *different* talkbacks — no global IP rate limit.
- Dedup is **permanent** — verified at 0s, 6s, and 60s gap; a 10-minute gap is expected to
  produce the same result (S05 not yet run but S04 confirmed).
- **No flood detection** — 10 rapid-fire votes from the same IP return HTTP 200 each;
  server never 429s or 403s on repetition. Only the first counts.

## Cookie — browser-only, server never reads it
The server sets `Set-Cookie: talkback_<id>=True; Path=/` on every vote response.
This cookie is **never enforced server-side**. Sending it back in a subsequent
request still returns `{"success": true}` and still counts (if the IP+talkback
+type slot is fresh). The cookie is purely a browser-UI hint.

## Blocking — what triggers 403
Akamai CDN returns HTTP 403 "Access Denied" HTML.

Trigger matrix (tested on same working proxy):
| Header variant             | Result      |
|---------------------------|-------------|
| Origin: www.ynet.co.il    | ✓ VOTED     |
| Origin: google.com        | ✗ 403       |
| Origin missing            | ✗ 403       |
| Referer missing           | ✓ VOTED     |
| User-Agent: curl          | ✓ VOTED     |
| User-Agent: python-requests| ✓ VOTED    |
| User-Agent: Mobile Safari | ✓ VOTED     |

**`Origin: https://www.ynet.co.il` is the only header that matters.**
Referer, User-Agent, Accept-* — all irrelevant.

The ~13% 403 rate seen in production is Akamai IP-reputation blocking on the
proxy's exit IP. A proxy that gets 403 is permanently flagged; retrying it on
the same talkback or any other talkback won't help.

## Direct IP (no proxy)
Tested from the server's own IP (S11): vote returned HTTP 200 `{"success":true}` and
the counter moved +1 after ~20 minutes (variable latency, low-activity talkback).
Conclusion: the server itself is **not** blocked by Akamai — its IP is clean.
Can be used for one like + one unlike per talkback just like any proxy IP.

## Strategy implications
1. **One counted vote per (proxy IP, talkback, type)** — after that, HTTP 200
   keeps coming but the counter doesn't move. Rotate proxies to stack more votes.
2. **Like + unlike from the same proxy on the same talkback BOTH count** — each
   proxy can contribute 1 like AND 1 unlike per talkback.
3. **No global IP rate limit** — one proxy can vote on N different talkbacks and
   all N votes count. Only the repeat-same-talkback-same-type slot is locked.
4. **Counter latency is 60–300s+** — poll no sooner than 5 minutes after voting
   to get a reliable final count, especially on low-activity talkbacks.
5. **`Origin: https://www.ynet.co.il` must always be set** — without it, 100% 403.
6. **403 proxy = permanently dead for all talkbacks** — remove from pool immediately.
7. **Cookie is irrelevant** — do not track, do not send, does not affect counting.
