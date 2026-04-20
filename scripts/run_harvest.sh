#!/bin/bash
# Continuous harvest wrapper — runs mega_harvest.py in infinite loop mode.
# Sources refresh every 10-60 min, so each cycle finds fresh candidates.
# Ctrl+C stops gracefully (SIGINT → checkpoint → exit).
#
# Usage:
#   ./scripts/run_harvest.sh              # run until 10K
#   ./scripts/run_harvest.sh 15000        # custom target
#
# Monitor:
#   tail -f scripts/discovery/sources/mega_harvest.log
#   python3 -c "import json; d=json.load(open('proxies/master_pool.json')); print(len(d))"

TARGET=${1:-10000}
cd "$(dirname "$0")/.."

echo "========================================"
echo " Continuous Proxy Harvest"
echo " Target: $TARGET working proxies"
echo " Log: scripts/discovery/sources/mega_harvest.log"
echo "========================================"

exec python3 scripts/mega_harvest.py \
    --loops 0 \
    --concurrency 120 \
    --timeout 8 \
    --pause 120 \
    --target "$TARGET"
