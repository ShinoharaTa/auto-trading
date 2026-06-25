"""クロスペア × ランダム期間で「設定値の信頼性」を検証する。

考え方:
  ある設定(戦略×時間足×パラメータ)が本物の優位性なら、学習に使っていない
  別ペアでも、ランダム期間でもそこそこ通用するはず。1ペアでしか効かないなら
  それは過剰適合。→ 設定を固定し、各ペアでブロック・ブートストラップ評価して
  指標の分布を比べ、ペア間で揃って良いか(=信頼できるか)を判定する。

信頼性スコアの考え方:
  - 全ペアで利益区間の割合が高い(consistency)
  - 最悪5%リターンが浅い(downside)
  - ペア間のばらつきが小さい(stability)
  これらが揃うほど「この設定は信頼できる」。
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import montecarlo as MC
from src.backtest import simulator as S
from src.data import resample as R

# walk-forward[5] で最も多く選ばれた候補を既定の検証対象にする
DEFAULT_CANDIDATE: dict[str, Any] = {
    "strategy": "sma_cross",
    "timeframe": "1h",
    "params": {"fast": 20, "slow": 50, "atr_stop_mult": 2.5, "risk_per_trade": 0.01},
}


def validate_pair(db: str, pair: str, strategy: str, timeframe: str,
                  params: dict[str, Any], period: str, n_runs: int,
                  seed: int) -> dict[str, Any] | None:
    data = S.load_candles(db, pair)
    if data.empty:
        return None
    rdf = R.resample_ohlcv(data, timeframe)
    bpy = R.bars_per_year(timeframe)
    block = R.bars_for(timeframe, period)
    if len(rdf) <= block:
        return None
    res = MC.run_montecarlo(rdf, params, n_runs, block, seed, bpy, strategy)
    v = MC.verdict(res)
    return {
        "pair": pair,
        "win_rate": v["win_period_rate"],
        "median_ret": v["median_return"],
        "mean_ret": float(res["total_return"].mean()),
        "p5_ret": v["p5_return"],
        "worst_dd": v["worst_drawdown"],
        "med_trades": float(res["n_trades"].median()),
        "robust": v["robust"],
    }


def cross_pair_validate(db: str, pairs: list[str],
                        candidate: dict[str, Any] = DEFAULT_CANDIDATE,
                        period: str = "1m", n_runs: int = 300,
                        seed: int = 42) -> pd.DataFrame:
    strategy = candidate["strategy"]
    timeframe = candidate["timeframe"]
    params = candidate["params"]
    rows = [r for pair in pairs
            if (r := validate_pair(db, pair, strategy, timeframe, params,
                                   period, n_runs, seed)) is not None]
    return pd.DataFrame(rows)


def reliability(table: pd.DataFrame) -> dict[str, Any]:
    """ペア横断の信頼性サマリ。"""
    if table.empty:
        return {"n_pairs": 0}
    wr = table["win_rate"]
    med = table["median_ret"]
    return {
        "n_pairs": int(len(table)),
        "min_win_rate": float(wr.min()),         # 最も弱いペアの勝率(ここが肝)
        "mean_win_rate": float(wr.mean()),
        "all_pairs_positive_median": bool((med > 0).all()),  # 全ペアで中央値プラス?
        "median_ret_spread": float(med.max() - med.min()),   # ペア間ばらつき(小さいほど安定)
        "worst_p5": float(table["p5_ret"].min()),            # 全ペア通じた最悪5%
        "deepest_dd": float(table["worst_dd"].max()),
        # 信頼できる目安: 全ペアで利益区間≥55% かつ 中央値プラス かつ 最悪5%≥-12%
        "reliable": bool((wr >= 0.55).all() and (med > 0).all()
                         and table["p5_ret"].min() >= -0.12),
    }


def main() -> None:
    from src.config import load

    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--pairs", nargs="+",
                    default=["btc_jpy", "eth_jpy", "xrp_jpy", "ltc_jpy"])
    ap.add_argument("--strategy", default=DEFAULT_CANDIDATE["strategy"])
    ap.add_argument("--timeframe", default=DEFAULT_CANDIDATE["timeframe"])
    ap.add_argument("--period", default="1m", choices=list(R.PERIOD_MIN))
    ap.add_argument("--runs", type=int, default=300)
    args = ap.parse_args()

    candidate = {
        "strategy": args.strategy,
        "timeframe": args.timeframe,
        "params": DEFAULT_CANDIDATE["params"],
    }
    print(f"validating {candidate['strategy']} @ {candidate['timeframe']} "
          f"{candidate['params']}\n  pairs={args.pairs} period={args.period} runs={args.runs}")
    table = cross_pair_validate(args.db, args.pairs, candidate, args.period, args.runs)
    if table.empty:
        print("データのあるペアがありません。fetch してください。")
        return

    print("\n=== ペア別（ランダム期間の分布から）===")
    with pd.option_context("display.width", 200, "display.float_format",
                           lambda v: f"{v:,.4f}"):
        print(table.to_string(index=False))

    print("\n=== 信頼性サマリ ===")
    rel = reliability(table)
    for k, v in rel.items():
        print(f"  {k:26s}: {v:,.4f}" if isinstance(v, float) else f"  {k:26s}: {v}")
    print("\n  reliable=True の条件: 全ペアで利益区間≥55% かつ 中央値プラス かつ 最悪5%≥-12%")


if __name__ == "__main__":
    main()
