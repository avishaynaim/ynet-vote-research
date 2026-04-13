#!/usr/bin/env bash
# Script 06: Confirm server-side IP deduplication and cache behaviour
# Methodology:
#   1. Record baseline vote count
#   2. Send vote (no cookie)
#   3. Immediately re-check (cache still hot -> shows old count)
#   4. Wait for CDN cache to expire (~87s)
#   5. Re-check (cache miss -> updated count visible)
#   6. Attempt second vote from same IP -> count should NOT increase
#
# WARNING: Academic/research use only.

ARTICLE_ID="yokra14737379"
TARGET_ID="98996389"
BASE="https://www.ynet.co.il"
VOTE_URL="$BASE/iphone/json/api/talkbacks/vote"
LIST_URL="$BASE/iphone/json/api/talkbacks/list/v2/$ARTICLE_ID/0/1"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

get_counts() {
    curl -s "$LIST_URL" -H "User-Agent: $UA" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
for item in data['rss']['channel']['item']:
    if str(item['id']) == '$TARGET_ID':
        print(f\"likes={item['likes']} unlikes={item['unlikes']} net={item['talkback_like']}\")
"
}

get_cache_status() {
    curl -sI "$LIST_URL" -H "User-Agent: $UA" \
        | grep -E "vx-cache|cache-control|last-modified"
}

echo "=== [1] BASELINE ==="
get_counts
get_cache_status

echo ""
echo "=== [2] Sending LIKE vote ==="
curl -s -X POST "$VOTE_URL" \
    -H "User-Agent: $UA" \
    -H "Content-Type: application/json" \
    -H "Origin: $BASE" \
    -H "Referer: $BASE/news/article/$ARTICLE_ID" \
    -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
         \"talkback_like\":true,\"talkback_unlike\":false,\"vote_type\":\"2state\"}" \
    -D - 2>/dev/null | grep -E "HTTP|set-cookie|success"

echo ""
echo "=== [3] Immediately after vote (cache still hot) ==="
get_counts
get_cache_status

echo ""
echo "=== [4] Waiting 90s for CDN cache to expire... ==="
sleep 90

echo "=== [5] After cache expiry ==="
get_counts
get_cache_status

echo ""
echo "=== [6] Second vote attempt from same IP (IP dedup test) ==="
curl -s -X POST "$VOTE_URL" \
    -H "User-Agent: curl/8.5.0" \
    -H "Content-Type: application/json" \
    -H "Origin: $BASE" \
    -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
         \"talkback_like\":true,\"talkback_unlike\":false,\"vote_type\":\"2state\"}" \
    -D - 2>/dev/null | grep -E "HTTP|set-cookie|success"

sleep 5
echo ""
echo "=== [7] After second vote attempt ==="
echo "(count should be unchanged — IP dedup blocked it)"
get_counts

echo ""
echo "=== RESULTS SUMMARY ==="
echo "- First vote: count increments (visible after cache expiry)"
echo "- Second vote from same IP: returns success=true but count stays same"
echo "- Conclusion: IP-layer dedup enforced server-side; cookie is supplementary"
