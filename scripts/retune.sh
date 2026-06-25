#!/usr/bin/env bash
# 定期再チューニングを cron から叩くためのラッパ。
# 例(crontab): 毎月1日 03:00 に実行
#   0 3 1 * * /Users/shino3/Project/auto-trading/scripts/retune.sh >> /Users/shino3/Project/auto-trading/state/cron.log 2>&1
# 週次にするなら test-days を 7 にし、毎週月曜などに設定する。
set -euo pipefail

# このスクリプトの場所からプロジェクトルートを解決（cron でも cwd に依存しない）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

exec ./.venv/bin/python -m src.retune --test-days 30 --runs 120
