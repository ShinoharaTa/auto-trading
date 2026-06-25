"""ペア別チューナ。各ペアでウォークフォワードを回し、推奨設定を決めて
settings_store に書き込む。OOSで信頼できないペアは enabled=False にする。

これを定期実行すれば「ペアごとに設定が更新され続ける」運用になる。
週次なら test_days=7、月次なら test_days=30。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

import pandas as pd

from src import settings_store as STORE
from src.backtest import simulator as S
from src.backtest import walkforward as WF


def recommend_setting(steps: pd.DataFrame, strategy: str) -> dict[str, Any]:
    """OOS各窓で選ばれた (timeframe, params) の最頻値を推奨設定とする。
    同点なら直近の窓の選択を優先（実運用で次に使う設定に近い）。"""
    keys = [(row["timeframe"], json.dumps(row["params"], sort_keys=True))
            for _, row in steps.iterrows()]
    counts = Counter(keys)
    top = counts.most_common(1)[0][1]
    # 最頻のうち最も新しい窓で出たものを採用
    for tf, pjson in reversed(keys):
        if counts[(tf, pjson)] == top:
            best_tf, best_pjson = tf, pjson
            break
    return {
        "strategy": strategy,
        "timeframe": best_tf,
        "params": json.loads(best_pjson),
        "selection_freq": top / len(keys),  # この設定が選ばれた窓の割合（安定性）
    }


def reliability_gate(summary: dict[str, Any]) -> bool:
    """OOSで実運用に値するか。中央値プラス・平均プラス・勝率5割・DD許容内。"""
    return (summary.get("oos_mean_return", 0) > 0
            and summary.get("oos_median_return", 0) > 0
            and summary.get("oos_win_rate", 0) >= 0.50
            and summary.get("oos_worst_dd", 1) <= 0.15)


def tune_pair(db: str, pair: str, strategy: str = "sma_cross",
              train_days: int = 180, test_days: int = 30,
              n_runs: int = 120) -> dict[str, Any]:
    data = S.load_candles(db, pair)
    steps = WF.walk_forward(data, strategy, train_days=train_days,
                            test_days=test_days, n_runs=n_runs)
    if steps.empty:
        record = {"strategy": strategy, "enabled": False, "reason": "no_steps"}
        STORE.upsert(pair, record)
        return record

    summary = WF.summarize(steps)
    rec = recommend_setting(steps, strategy)
    enabled = reliability_gate(summary)
    record = {
        **rec,
        "enabled": enabled,
        "reason": "passed_oos" if enabled else "failed_oos",
        "oos": {
            "win_rate": round(summary["oos_win_rate"], 4),
            "mean_return": round(summary["oos_mean_return"], 4),
            "median_return": round(summary["oos_median_return"], 4),
            "worst": round(summary["oos_worst"], 4),
            "worst_dd": round(summary["oos_worst_dd"], 4),
            "compounded_x": round(summary["compounded_equity_x"], 4),
            "overfit_gap": round(summary["overfit_gap"], 4),
            "n_steps": summary["n_steps"],
        },
    }
    STORE.upsert(pair, record)
    return record


def main() -> None:
    from src.config import load

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

    for pair in args.pairs:
        print(f"\n=== tuning {pair} (strategy={args.strategy}) ===", flush=True)
        rec = tune_pair(args.db, pair, args.strategy, args.train_days,
                        args.test_days, args.runs)
        flag = "ENABLED" if rec.get("enabled") else "disabled"
        oos = rec.get("oos", {})
        print(f"  -> {flag} | {rec.get('timeframe','-')} {rec.get('params',{})}")
        if oos:
            print(f"     OOS win={oos['win_rate']:.0%} mean={oos['mean_return']:+.4f}/期 "
                  f"median={oos['median_return']:+.4f} dd={oos['worst_dd']:.4f} "
                  f"x{oos['compounded_x']}")

    print(f"\n=== 売買対象(enabled) === {STORE.enabled_pairs()}")
    print(f"設定ストア: {STORE.STORE}")


if __name__ == "__main__":
    main()
