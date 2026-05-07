#!/usr/bin/env bash
# Script 03: Fetch live talkback (comment) data for the article
# Discovers: URL format, data schema, vote counts

ARTICLE_ID="yokra14737379"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUT_DIR="../results"
BASE="https://www.ynet.co.il"

echo "=== [1] Testing sort parameter variants ==="
for sort in 0 1 2 most_liked newest oldest; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        "$BASE/iphone/json/api/talkbacks/list/v2/$ARTICLE_ID/$sort/1" \
        -H "User-Agent: $UA")
    echo "sort=$sort -> HTTP $STATUS"
done

echo ""
echo "=== [2] Fetching page 1 (sort=0) ==="
curl -s "$BASE/iphone/json/api/talkbacks/list/v2/$ARTICLE_ID/0/1" \
    -H "User-Agent: $UA" \
    -H "Accept: application/json" \
    -H "Referer: $BASE/news/article/$ARTICLE_ID" \
    -o "$OUT_DIR/talkbacks_page1.json"

echo ""
echo "=== [3] Parsing response ==="
python3 - <<'EOF'
import json

with open("../results/talkbacks_page1.json") as f:
    data = json.load(f)

channel = data["rss"]["channel"]
print(f"Total comments : {channel['sum_talkbacks']}")
print(f"Total discussions: {channel['sum_discussions']}")
print(f"Has more pages : {channel['hasMore']}")
print(f"Items on page 1 : {len(channel['item'])}")

print("\n--- Top 5 comments ---")
for item in channel["item"][:5]:
    print(f"  ID: {item['id']}")
    print(f"    Author : {item['author']}")
    print(f"    likes  : {item.get('likes', 0)}")
    print(f"    unlikes: {item.get('unlikes', 0)}")
    print(f"    talkback_like (net): {item.get('talkback_like', 0)}")
    print(f"    recommended: {item.get('recommended')}")
    print(f"    Keys   : {list(item.keys())}")
    print()
EOF

echo "=== [4] Finding recommended (high-voted) comments ==="
python3 - <<'EOF'
import json

with open("../results/talkbacks_page1.json") as f:
    data = json.load(f)

print("Recommended comments (likes > 30):")
for item in data["rss"]["channel"]["item"]:
    if item.get("likes", 0) > 30:
        print(f"  ID:{item['id']} likes:{item['likes']} unlikes:{item.get('unlikes',0)} "
              f"net:{item.get('talkback_like',0)} recommended:{item.get('recommended')}")
EOF

echo ""
echo "=== [5] GET response headers (cache info) ==="
curl -sI "$BASE/iphone/json/api/talkbacks/list/v2/$ARTICLE_ID/0/1" \
    -H "User-Agent: $UA" | grep -E "cache-control|vx-cache|last-modified|expires|date"
