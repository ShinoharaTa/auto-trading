#!/usr/bin/env bash
# systemd ユニット(常駐 + 日次通知タイマー)をまとめて配置・有効化する。
# 使い方（プロジェクト直下で）:  sudo ./scripts/install-systemd.sh
#
# - /path/to/auto-trading を実パスに自動置換
# - 古い systemd(<252) では OnCalendar のTZ指定が使えないため、
#   TZ を Asia/Tokyo に設定して時刻のみ指定に自動フォールバック
# - 何度実行しても安全（冪等）
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "sudo で実行してください: sudo $0"; exit 1; }

UNITS="auto-trading-paper.service auto-trading-notify.service auto-trading-notify.timer"
for f in $UNITS; do
  sed "s#/path/to/auto-trading#${DIR}#g" "$DIR/scripts/$f" > "/etc/systemd/system/$f"
  echo "installed  /etc/systemd/system/$f"
done

# systemd のバージョンで OnCalendar のTZ指定可否が分かれる
SDVER="$(systemctl --version | awk 'NR==1{print $2}')"
if [ "${SDVER:-0}" -lt 252 ]; then
  echo "systemd ${SDVER}: OnCalendar のTZ指定に非対応 → TZ=Asia/Tokyo に設定し時刻のみ指定へ"
  timedatectl set-timezone Asia/Tokyo || true
  sed -i 's#^OnCalendar=.*#OnCalendar=*-*-* 18:00:00#' /etc/systemd/system/auto-trading-notify.timer
fi

systemctl daemon-reload
systemctl enable --now auto-trading-paper.service    # 既に稼働中なら何もしない
systemctl enable --now auto-trading-notify.timer

echo
echo "=== paper daemon ==="
systemctl --no-pager --lines=3 status auto-trading-paper.service || true
echo "=== notify timer (NEXT に 18:00 が出れば成功) ==="
systemctl list-timers auto-trading-notify.timer --no-pager

echo
echo "▶ 今すぐ1回テスト送信するなら:"
echo "    sudo systemctl start auto-trading-notify.service"
