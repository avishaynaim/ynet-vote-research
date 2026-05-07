#!/usr/bin/env bash
# Script 05: Map the server-side cookie state machine for vote deduplication
# The server reads AND writes talkback_{id} cookies to track vote state.

ARTICLE_ID="yokra14737379"
TARGET_ID="98996402"
BASE="https://www.ynet.co.il"
VOTE_URL="$BASE/iphone/json/api/talkbacks/vote"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

vote() {
    local cookie="$1"
    local like="$2"
    local unlike="$3"
    local label="$4"

    echo "--- $label ---"
    ARGS=(-s -X POST "$VOTE_URL"
          -H "User-Agent: $UA"
          -H "Content-Type: application/json"
          -H "Origin: $BASE"
          -H "Referer: $BASE/news/article/$ARTICLE_ID"
          -d "{\"article_id\":\"$ARTICLE_ID\",\"talkback_id\":$TARGET_ID,
               \"talkback_like\":$like,\"talkback_unlike\":$unlike,
               \"vote_type\":\"2state\"}")
    if [ -n "$cookie" ]; then
        ARGS+=(-H "Cookie: talkback_${TARGET_ID}=${cookie}")
    fi
    curl "${ARGS[@]}" -D - 2>/dev/null \
        | grep -E "HTTP|set-cookie|success"
    echo ""
}

echo "=== Cookie State Machine: talkback_$TARGET_ID ==="
echo ""
echo "Transition table:"
echo "  (no cookie)  + LIKE   -> expect: set-cookie=True"
echo "  cookie=True  + LIKE   -> expect: no cookie (toggle off)"
echo "  cookie=True  + UNLIKE -> expect: set-cookie deleted (expires=1970)"
echo "  cookie=False + LIKE   -> expect: set-cookie deleted (neutralized)"
echo "  (no cookie)  + UNLIKE -> expect: set-cookie=False"
echo "  cookie=False + UNLIKE -> expect: no cookie (already unliked)"
echo ""

vote ""      "true"  "false" "Step 1: no cookie  -> LIKE"
vote "True"  "true"  "false" "Step 2: True cookie -> LIKE (toggle off)"
vote "True"  "false" "true"  "Step 3: True cookie -> UNLIKE"
vote "False" "true"  "false" "Step 4: False cookie -> LIKE"
vote ""      "false" "true"  "Step 5: no cookie   -> UNLIKE"
vote "False" "false" "true"  "Step 6: False cookie -> UNLIKE (toggle off)"

echo ""
echo "=== Summary ==="
echo "Server manages cookie state — it is not purely client-side."
echo "Cookie values: True=liked, False=unliked, absent=neutral"
echo "The set-cookie with expires=1970 means the server is DELETING the cookie."
