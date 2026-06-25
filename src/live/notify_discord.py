"""日次Discord通知。資産スナップショットから各口座の評価額と
1日/1週/2週の変化率を算出し、Discord Webhook に投稿する。

cron で毎日 18:00 JST に実行する想定。
Webhook URL は環境変数 DISCORD_WEBHOOK_URL から読む（コードに書かない）。
--dry-run で投稿せず本文を表示。
"""
from __future__ import annotations

import argparse
import time

from src.live import discord
from src.live.paper import _cfg
from src.live.store import PaperStore

_DAY_MS = 86_400_000
WINDOWS = [("1日", 1 * _DAY_MS), ("1週", 7 * _DAY_MS), ("2週", 14 * _DAY_MS)]


def _pct(now: float, past: float | None) -> str:
    if not past:
        return "—"
    v = (now / past - 1.0) * 100
    return f"{v:+.2f}%"


def build_message(store: PaperStore) -> str:
    now_ts = int(time.time() * 1000)
    lines = ["**📈 ペーパートレード 日次レポート**"]
    total_now = 0.0
    total_base = 0.0
    for name in store.accounts():
        eq = store.latest_equity(name)
        if eq is None:
            continue
        base = store.base_jpy(name)
        total_now += eq
        total_base += base
        lines.append(f"\n**【{name}】** 現在評価額 **¥{eq:,.0f}**（ベース比 {_pct(eq, base)}）")
        for label, ms in WINDOWS:
            past = store.equity_at_or_before(name, now_ts - ms)
            lines.append(f"・{label}前比: {_pct(eq, past)}")
        fs = store.fillstat(name)
        if fs.get("placed", 0) > 0:  # maker口座のみ約定率を表示
            rate = fs["filled"] / fs["placed"] * 100
            lines.append(f"・指値約定率: {rate:.0f}%（約定{fs['filled']}/発注{fs['placed']}）")
    if total_base > 0:
        lines.append(f"\n**合計** 評価額 **¥{total_now:,.0f}**"
                     f"（ベース¥{total_base:,.0f} 比 {_pct(total_now, total_base)}）")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db_path, _, _ = _cfg()
    store = PaperStore(db_path)
    msg = build_message(store)

    if args.dry_run:
        print(msg)
        return
    print("posted to Discord" if discord.post(msg) else "通知できませんでした")


if __name__ == "__main__":
    main()
