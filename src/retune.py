"""定期再チューニングのオーケストレータ（LLM不要・完全機械的）。

  1. 各ペアの最新ローソク足を増分取得（既存日はスキップ）
  2. 各ペアでウォークフォワード→ゲート判定→ settings_store 更新
  3. 前回設定との差分をログ。異常時は review_needed=True を立てて通知に回す

注文は一切出さない。設定(state/pair_settings.json)を更新するだけなので無人運用でも安全。
cron から scripts/retune.sh 経由で叩く想定。

review_needed が立つ条件（人間/LLMの確認推奨）:
  - enabled だったペアが全て disabled に落ちた（地合い変化の疑い）
  - いずれかのペアで overfit_gap が大きく開いた（過剰最適化の兆候）
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import settings_store as STORE
from src.backtest import tune as TUNE
from src.config import ROOT, load
from src.data import fetch as FETCH

LOG = ROOT / "state" / "retune_log.jsonl"
OVERFIT_GAP_ALERT = 0.03  # 学習窓とOOSの乖離がこれを超えたら確認推奨


def _incremental_fetch(pair: str, db_path: str, lookback_days: int) -> int:
    """直近 lookback_days 分を取得（fetch側で取得済み過去日はスキップ）。"""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)
    return FETCH.run(pair, db_path, start, end)


def retune(db_path: str, pairs: list[str], strategy: str = "sma_cross",
           train_days: int = 180, test_days: int = 30, runs: int = 120,
           fetch_lookback: int = 45) -> dict[str, Any]:
    prev = STORE.load_all()
    results: dict[str, Any] = {}
    changes: list[str] = []

    for pair in pairs:
        try:
            n = _incremental_fetch(pair, db_path, fetch_lookback)
            print(f"[fetch] {pair}: +{n} bars", flush=True)
        except Exception as e:  # 取得失敗でも既存データで再チューニングは続行
            print(f"[fetch] {pair}: WARN {e}", flush=True)

        rec = TUNE.tune_pair(db_path, pair, strategy, train_days, test_days, runs)
        results[pair] = rec

        was = prev.get(pair, {})
        if was.get("enabled") != rec.get("enabled"):
            changes.append(f"{pair}: enabled {was.get('enabled')} -> {rec.get('enabled')}")
        elif was.get("params") != rec.get("params") or was.get("timeframe") != rec.get("timeframe"):
            changes.append(f"{pair}: setting changed -> {rec.get('timeframe')} {rec.get('params')}")
        print(f"[tune] {pair}: {'ENABLED' if rec.get('enabled') else 'disabled'} "
              f"{rec.get('timeframe','-')} {rec.get('params',{})}", flush=True)

    enabled_now = STORE.enabled_pairs()
    enabled_before = [p for p, r in prev.items() if r.get("enabled")]
    max_gap = max((abs(r.get("oos", {}).get("overfit_gap", 0)) for r in results.values()),
                  default=0.0)
    review_needed = (
        (len(enabled_before) > 0 and len(enabled_now) == 0)  # 全ペア無効化
        or max_gap > OVERFIT_GAP_ALERT                       # 過剰最適化の兆候
    )

    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs": pairs,
        "enabled_before": enabled_before,
        "enabled_now": enabled_now,
        "changes": changes,
        "max_overfit_gap": round(max_gap, 4),
        "review_needed": review_needed,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def main() -> None:
    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--pairs", nargs="+",
                    default=d.get("pairs", ["btc_jpy", "eth_jpy", "xrp_jpy", "ltc_jpy"]))
    ap.add_argument("--strategy", default="sma_cross")
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30, help="30=月次, 7=週次")
    ap.add_argument("--runs", type=int, default=120)
    args = ap.parse_args()

    print(f"=== retune {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z "
          f"pairs={args.pairs} ===", flush=True)
    s = retune(args.db, args.pairs, args.strategy, args.train_days,
               args.test_days, args.runs)
    print("\n=== summary ===")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if s["review_needed"]:
        print("\n⚠ REVIEW NEEDED: 結果が異常です。人間/LLMで確認してください。")


if __name__ == "__main__":
    main()
