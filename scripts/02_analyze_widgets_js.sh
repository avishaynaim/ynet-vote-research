#!/usr/bin/env bash
# Script 02: Download and analyze the Ynet widgets.js bundle for voting logic

WIDGETS_URL="https://ynet-pic1.yit.co.il/Common/frontend/site/prod/widgets.ce1eacd843df1022013e.js"
OUT_DIR="../results"

echo "=== [1] Downloading widgets.js ==="
curl -s "$WIDGETS_URL" -o "$OUT_DIR/widgets.js"
echo "Size: $(wc -c < "$OUT_DIR/widgets.js") bytes"

echo ""
echo "=== [2] Finding all API endpoints ==="
python3 -c "
import re
with open('$OUT_DIR/widgets.js') as f:
    content = f.read()
endpoints = set(re.findall(r'/(?:api|iphone/json/api)/[a-zA-Z0-9_/]+', content))
for ep in sorted(endpoints):
    print(ep)
"

echo ""
echo "=== [3] Extracting vote endpoint and state machine ==="
python3 - <<'EOF'
with open("../results/widgets.js") as f:
    content = f.read()

idx = content.find("talkbacks/vote")
if idx != -1:
    print(content[max(0, idx - 500):idx + 3500])
EOF

echo ""
echo "=== [4] Extracting article star rating logic ==="
python3 - <<'EOF'
with open("../results/widgets.js") as f:
    content = f.read()

idx = content.find("articleRating")
if idx != -1:
    print(content[max(0, idx - 300):idx + 2000])
EOF

echo ""
echo "=== [5] Checking for CSRF / auth token references near vote code ==="
python3 - <<'EOF'
import re
with open("../results/widgets.js") as f:
    content = f.read()

for term in ["Authorization", "Bearer", "csrf", "x-csrf", "auth_token"]:
    idx = content.find(term)
    if idx != -1:
        ctx = content[max(0, idx - 100):idx + 200]
        if "vote" in ctx.lower() or "talkback" in ctx.lower():
            print(f"=== {term} near vote code ===")
            print(ctx)
EOF
