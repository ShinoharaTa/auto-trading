#!/usr/bin/env bash
# 日次Discord通知（cron から 18:00 JST に叩く）。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
# DISCORD_WEBHOOK_URL を .env から読み込む（cron は環境変数を継承しないため）
[ -f "$DIR/.env" ] && { set -a; . "$DIR/.env"; set +a; }
exec ./.venv/bin/python -m src.live.notify_discord
