"""ペーパートレード常駐プロセス。実発注はしない。

各ポール（既定60秒）で:
  - 対象ペアの当日足を1回だけ増分取得（refresh）
  - 各口座を1ステップ進め、評価額スナップショットを記録
サーバーで systemd 常駐させる想定。--once でワンショット実行（テスト用）。
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from src.config import load
from src.data import fetch as FETCH
from src.live import engine, feed
from src.live.store import PaperStore

# config に [paper] が無い場合の既定（A=成行トレンド / B=指値intraday、各¥100k）
DEFAULT_ACCOUNTS = [
    {"name": "A_trend", "strategy": "sma_cross", "order_type": "taker",
     "timeframe": "1h", "pairs": ["btc_jpy", "eth_jpy"], "base_jpy": 100_000,
     "params": {"fast": 40, "slow": 50, "atr_stop_mult": 2.5, "risk_per_trade": 0.01}},
    {"name": "B_intraday", "strategy": "rsi", "order_type": "maker",
     "timeframe": "5m", "pairs": ["btc_jpy"], "base_jpy": 100_000,
     "params": {"rsi_period": 14, "rsi_entry": 30, "rsi_exit": 55,
                "atr_stop_mult": 2.5, "risk_per_trade": 0.01}},
]


def _cfg() -> tuple[str, list[dict], int]:
    cfg = load()
    paper = cfg.get("paper", {})
    db_path = paper.get("db_path", "state/paper.sqlite")
    accounts = paper.get("accounts") or DEFAULT_ACCOUNTS
    poll = int(paper.get("poll_seconds", 60))
    return db_path, accounts, poll


def run_once(store: PaperStore, db_path: str, accounts: list[dict]) -> None:
    now_ts = int(time.time() * 1000)
    pairs = {pp for a in accounts for pp in a["pairs"]}
    for pair in pairs:                       # 当日足を1ペア1回だけ取得
        try:
            FETCH.run(pair, db_path, datetime.now(timezone.utc).date(),
                      datetime.now(timezone.utc).date(), pause=0.0)
        except Exception as e:
            print(f"[paper] fetch {pair} WARN {e}", flush=True)

    bars_cache: dict[tuple, object] = {}

    def provider(pair: str, tf: str):
        key = (pair, tf)
        if key not in bars_cache:
            bars_cache[key] = feed.get_recent_bars(db_path, pair, tf, refresh=False)
        return bars_cache[key]

    for acct in accounts:
        try:
            r = engine.step_account(store, acct, now_ts, provider)
            print(f"[paper] {datetime.now(timezone.utc):%H:%M}Z {acct['name']}: "
                  f"equity=¥{r['equity']:,.0f}", flush=True)
        except Exception as e:
            print(f"[paper] step {acct['name']} ERROR {e}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    args = ap.parse_args()
    db_path, accounts, poll = _cfg()
    store = PaperStore(db_path)
    print(f"=== paper trading start: accounts={[a['name'] for a in accounts]} "
          f"poll={poll}s db={db_path} ===", flush=True)
    if args.once:
        run_once(store, db_path, accounts)
        return
    while True:
        try:
            run_once(store, db_path, accounts)
        except Exception as e:
            print(f"[paper] loop ERROR {e}", flush=True)
        time.sleep(poll)


if __name__ == "__main__":
    main()
