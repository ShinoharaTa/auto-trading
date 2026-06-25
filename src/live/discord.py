"""Discord Webhook 投稿の共通ヘルパ。

失敗してもプロセスを止めない（通知不達で売買ロジックを巻き込まない）。
Webhook URL は環境変数 DISCORD_WEBHOOK_URL から読む。
"""
from __future__ import annotations

import os

import requests


def post(content: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[discord] DISCORD_WEBHOOK_URL 未設定のため通知スキップ", flush=True)
        return False
    try:
        r = requests.post(url, json={"content": content}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:  # ネットワーク/Discord障害でも常駐を止めない
        print(f"[discord] 通知失敗: {e}", flush=True)
        return False
