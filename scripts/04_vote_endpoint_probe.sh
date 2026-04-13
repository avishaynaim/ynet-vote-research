#!/usr/bin/env bash
# Script 04: Probe the vote endpoint — validation, schema, and basic behaviour

ARTICLE_ID="yokra14737379"
TARGET_ID="98996389"   # A comment ID from the article
BASE="https://www.ynet.co.il"
VOTE_URL="$BASE/iphone/json/api/talkbacks/vote"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
COMMON_HEADERS=(-H "User-Agent: $UA" -H "Content-Type: application/json"
                -H "Origin: $BASE" -H "Referer: $BASE/news/article/$ARTICLE_ID")

echo "=== [1] Required fields validation — empty body ==="
curl -s -X POST "$VOTE_URL" \
    "${COMMON_HEADERS[@]}" \
    -d '{}' | python3 -m json.tool

echo ""
echo "=== [2] Validation — missing talkback_id ==="
curl -s -X POST "$VOTE_URL" \
    "${COMMON_HEADERS[@]}" \
    -d '{"article_id":"'"$ARTICLE_ID"'","talkback_like":true}' | python3 -m json.tool

echo ""
echo "=== [3] Validation — invalid vote_type ==="
curl -s -X POST "$VOTE_URL" \
    "${COMMON_HEADERS[@]}" \
    -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
         \"talkback_like\":true,\"talkback_unlike\":false,
         \"vote_type\":\"invalid\"}" | python3 -m json.tool

echo ""
echo "=== [4] Valid vote_type enum values ==="
for vtype in "2state" "3state"; do
    echo "--- vote_type=$vtype ---"
    curl -s -X POST "$VOTE_URL" \
        "${COMMON_HEADERS[@]}" \
        -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
             \"talkback_like\":true,\"talkback_unlike\":false,
             \"vote_type\":\"$vtype\"}" | python3 -m json.tool
done

echo ""
echo "=== [5] CORS / security headers inspection ==="
curl -s -X POST "$VOTE_URL" \
    "${COMMON_HEADERS[@]}" \
    -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
         \"talkback_like\":true,\"talkback_unlike\":false,\"vote_type\":\"2state\"}" \
    -D - 2>/dev/null | grep -E "HTTP|content-type|access-control|x-frame|osv|cache-control|set-cookie"

echo ""
echo "=== [6] X-Forwarded-For spoofing test ==="
echo "Sending with X-Forwarded-For: 8.8.8.8 ..."
curl -s -X POST "$VOTE_URL" \
    "${COMMON_HEADERS[@]}" \
    -H "X-Forwarded-For: 8.8.8.8" \
    -H "X-Real-IP: 8.8.8.8" \
    -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
         \"talkback_like\":true,\"talkback_unlike\":false,\"vote_type\":\"2state\"}" \
    -D - 2>/dev/null | grep -E "HTTP|set-cookie|success"
echo "(Count unchanged = X-FF header is ignored by server)"
