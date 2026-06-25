#!/usr/bin/env bash
# ペーパートレード常駐プロセス起動（systemd から叩く）。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
[ -f "$DIR/.env" ] && { set -a; . "$DIR/.env"; set +a; }
exec ./.venv/bin/python -m src.live.paper
