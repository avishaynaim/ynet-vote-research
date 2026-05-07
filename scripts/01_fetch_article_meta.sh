#!/usr/bin/env bash
# Script 01: Fetch article HTML and extract JS bundles + widget configs
# Target: https://www.ynet.co.il/news/article/yokra14737379

ARTICLE_URL="https://www.ynet.co.il/news/article/yokra14737379"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUT_DIR="../results"

echo "=== [1] Fetching article HTML ==="
curl -s "$ARTICLE_URL" \
  -H "User-Agent: $UA" \
  -H "Accept-Language: he-IL,he;q=0.9,en-US;q=0.8" \
  -o "$OUT_DIR/article.html"

echo "=== [2] Extracting all JS file URLs ==="
grep -oE "https?://[^\"' ]+\.js[^\"' ]*" "$OUT_DIR/article.html" | sort -u \
  > "$OUT_DIR/js_files.txt"
cat "$OUT_DIR/js_files.txt"

echo ""
echo "=== [3] Extracting YITSiteWidgets configs ==="
python3 - <<'EOF'
import re, json

with open("../results/article.html") as f:
    content = f.read()

matches = re.findall(r"YITSiteWidgets\.push\(\[(.*?)\]\);", content, re.DOTALL)
print(f"Found {len(matches)} widget configs")
for i, m in enumerate(matches):
    print(f"\n--- Config {i+1} ---")
    print(m[:500])
EOF

echo ""
echo "=== [4] Checking isSpotim flag ==="
grep -o '"isSpotim":[^,}]*' "$OUT_DIR/article.html" | head -3

echo ""
echo "=== [5] Extracting article ID ==="
grep -o 'window\.articleId = "[^"]*"' "$OUT_DIR/article.html" | head -1
