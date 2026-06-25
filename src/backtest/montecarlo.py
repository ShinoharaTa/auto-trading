"""[3] モンテカルロ評価（ブロック・ブートストラップ）。

分足を1本ずつシャッフルするとトレンド/自己相関が壊れるため、
「ランダムな開始位置から連続 block_bars 本」の区間を N 本サンプリングし、
各区間で独立にバックテストして指標の分布を得る。

平均だけでなく分布（特に最悪5%・最大DD）を見ることで、
「たまたま良い相場で勝てただけ」を弾き、パラメータの頑健性を測る。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import simulator as S

# 区間長プリセット（1分足の本数）
BLOCK_PRESETS = {
    "1d": 24 * 60,
    "3d": 3 * 24 * 60,
    "1w": 7 * 24 * 60,
    "2w": 14 * 24 * 60,
    "1m": 30 * 24 * 60,
}

# 分布として見たい指標（max_drawdown は小さいほど良い向き）
_METRIC_KEYS = ("total_return", "max_drawdown", "sharpe", "profit_factor",
                "expectancy", "win_rate", "n_trades")


def sample_block(df: pd.DataFrame, block_bars: int, rng: np.random.Generator) -> pd.DataFrame:
    if len(df) <= block_bars:
        return df
    start = int(rng.integers(0, len(df) - block_bars))
    return df.iloc[start:start + block_bars]


def run_montecarlo(
    df: pd.DataFrame,
    params: dict[str, Any] | None = None,
    n_runs: int = 500,
    block_bars: int = BLOCK_PRESETS["1w"],
    seed: int | None = 42,
    bars_per_year: float = 525_600.0,
    strategy: str = "sma_cross",
) -> pd.DataFrame:
    """各ランの指標を 1行ずつ持つ DataFrame を返す。"""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, float]] = []
    for _ in range(n_runs):
        block = sample_block(df, block_bars, rng)
        _, m = S.run(block, params, bars_per_year, strategy)
        rows.append(asdict(m))
    return pd.DataFrame(rows)


def summarize(results: pd.DataFrame) -> pd.DataFrame:
    """主要指標について 平均/中央値/p5/p25/p75/p95 を表にする。"""
    cols = [c for c in _METRIC_KEYS if c in results.columns]
    # profit_factor の inf は要約を壊すので除外して扱う
    clean = results[cols].replace([np.inf, -np.inf], np.nan)
    q = clean.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    summary = pd.DataFrame({
        "mean": clean.mean(),
        "std": clean.std(ddof=1),
        "p5": q.loc[0.05],
        "p25": q.loc[0.25],
        "median": q.loc[0.5],
        "p75": q.loc[0.75],
        "p95": q.loc[0.95],
    })
    return summary


def verdict(results: pd.DataFrame) -> dict[str, Any]:
    """頑健性のざっくり判定。リスク調整後の観点で合否の目安を出す。"""
    r = results.replace([np.inf, -np.inf], np.nan)
    ret = r["total_return"]
    return {
        "n_runs": int(len(r)),
        "win_period_rate": float((ret > 0).mean()),     # 利益で終えた区間の割合
        "median_return": float(ret.median()),
        "p5_return": float(ret.quantile(0.05)),          # 最悪5%の損益
        "worst_return": float(ret.min()),
        "worst_drawdown": float(r["max_drawdown"].max()),
        "median_sharpe": float(r["sharpe"].median()),
        # 目安: 7割の区間で勝ち、最悪5%でも -10% 以内なら頑健寄り
        "robust": bool((ret > 0).mean() >= 0.7 and ret.quantile(0.05) >= -0.10),
    }


def main() -> None:
    from src.config import load

    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default=d["pair"])
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--runs", type=int, default=500)
    ap.add_argument("--block", default="1w", choices=list(BLOCK_PRESETS))
    args = ap.parse_args()

    data = S.load_candles(args.db, args.pair)
    block_bars = BLOCK_PRESETS[args.block]
    print(f"loaded {len(data):,} bars; block={args.block} ({block_bars:,} bars); runs={args.runs}")
    if len(data) <= block_bars:
        print("[warn] データが区間長より短いです。fetch でもっと貯めてください。")

    results = run_montecarlo(data, n_runs=args.runs, block_bars=block_bars)
    print("\n=== 指標の分布 ===")
    with pd.option_context("display.float_format", lambda v: f"{v:,.4f}"):
        print(summarize(results))
    print("\n=== 頑健性 ===")
    for k, v in verdict(results).items():
        print(f"  {k:18s}: {v}")


if __name__ == "__main__":
    main()
