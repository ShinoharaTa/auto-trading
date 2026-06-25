"""[4] パラメータ最適化。

時間足 × 戦略パラメータ を総当たりし、各候補をモンテカルロ（ブロック・ブートストラップ）
で評価。「利益と損失のバランス」を 1 つの合成スコアにして順位付けする。

balanced_score = median_return + p5_return
  - median_return : 区間リターンの中央値（期待される儲け）
  - p5_return     : 最悪5%区間のリターン（下振れリスク。通常マイナス）
  両者を足すことで「儲けは大きく、最悪も浅く」を同時に評価する。
  下振れが深い候補は p5 のマイナスで自動的に減点される。

注意: ここで選ぶのは候補の絞り込みまで。最終採用は walk-forward[5] の
アウトオブサンプル検証を通すこと（インサンプル最適化は過剰最適化を生む）。
"""
from __future__ import annotations

import argparse
import itertools
from typing import Any, Iterable

import pandas as pd

from src.backtest import montecarlo as MC
from src.backtest import simulator as S
from src.backtest import strategies as ST
from src.data import resample as R

# 既定の探索時間足
DEFAULT_TIMEFRAMES = ["15m", "30m", "1h", "4h"]

# 戦略ごとの探索グリッド（控えめ。広げるのは呼び出し側で）
STRATEGY_GRIDS: dict[str, dict[str, list[Any]]] = {
    "sma_cross": {
        "fast": [10, 20, 40],
        "slow": [50, 100, 200],
        "atr_stop_mult": [1.5, 2.5],
        "risk_per_trade": [0.01],
    },
    "rsi": {
        "rsi_period": [7, 14, 21],
        "rsi_entry": [20, 30],
        "rsi_exit": [50, 60],
        "atr_stop_mult": [1.5, 2.5],
        "risk_per_trade": [0.01],
    },
    "bollinger": {
        "bb_period": [20, 40],
        "bb_k": [2.0, 2.5],
        "atr_stop_mult": [1.5, 2.5],
        "risk_per_trade": [0.01],
    },
}


def _param_combos(grid: dict[str, list[Any]]) -> Iterable[dict[str, Any]]:
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values))
        if params.get("fast", 0) >= params.get("slow", 1):
            continue  # fast >= slow は無意味
        yield params


def balanced_score(verdict: dict[str, Any]) -> float:
    return verdict["median_return"] + verdict["p5_return"]


def optimize(
    df: pd.DataFrame,
    strategy: str = "sma_cross",
    timeframes: list[str] = DEFAULT_TIMEFRAMES,
    grid: dict[str, list[Any]] | None = None,
    period: str = "1w",
    n_runs: int = 200,
    seed: int = 42,
    verbose: bool = True,
) -> pd.DataFrame:
    grid = grid or STRATEGY_GRIDS[strategy]
    rows: list[dict[str, Any]] = []
    for tf in timeframes:
        rdf = R.resample_ohlcv(df, tf)
        bpy = R.bars_per_year(tf)
        block = R.bars_for(tf, period)
        if len(rdf) <= block:
            if verbose:
                print(f"[skip] {tf}: データ不足 (bars={len(rdf)} <= block={block})")
            continue
        for params in _param_combos(grid):
            # 指標の最長窓に対しブロックが短すぎると約定機会がなく結果が0になる。
            # 実際に売買できる余地（窓の2倍以上）が無い組合せはスキップ。
            longest = ST.warmup_bars(strategy, params)
            if block < longest * 2:
                continue
            res = MC.run_montecarlo(rdf, params, n_runs, block, seed, bpy, strategy)
            med_trades = float(res["n_trades"].median())
            if med_trades < 1:
                continue  # ノートレード候補は評価対象外（0が負の候補より上に来る穴を防ぐ）
            v = MC.verdict(res)
            rows.append({
                "strategy": strategy,
                "timeframe": tf,
                **params,
                "score": balanced_score(v),
                "win_rate": v["win_period_rate"],
                "median_ret": v["median_return"],
                "p5_ret": v["p5_return"],
                "worst_dd": v["worst_drawdown"],
                "med_trades": med_trades,
                "robust": v["robust"],
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("score", ascending=False).reset_index(drop=True)


def main() -> None:
    from src.config import load

    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default=d["pair"])
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--period", default="1w", choices=list(R.PERIOD_MIN))
    ap.add_argument("--strategy", default="all",
                    choices=["all", *STRATEGY_GRIDS])
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    data = S.load_candles(args.db, args.pair)
    strats = list(STRATEGY_GRIDS) if args.strategy == "all" else [args.strategy]
    print(f"loaded {len(data):,} bars; optimizing {strats} "
          f"(period={args.period}, runs={args.runs})...")
    tables = []
    for st in strats:
        t = optimize(data, strategy=st, period=args.period, n_runs=args.runs)
        if not t.empty:
            tables.append(t)
    table = (pd.concat(tables, ignore_index=True)
             .sort_values("score", ascending=False).reset_index(drop=True)
             if tables else pd.DataFrame())
    if table.empty:
        print("\n有効な候補なし（全候補がノートレード/ブロック過短）。"
              "\n→ --period を伸ばす、グリッドの slow を小さくする、データを増やす。")
        return
    view_cols = ["strategy", "timeframe", "score", "win_rate", "median_ret",
                 "p5_ret", "worst_dd", "med_trades", "robust"]
    print(f"\n=== 上位{args.top}候補（score=median_ret+p5_ret 降順）===")
    with pd.option_context("display.width", 200, "display.max_columns", None,
                           "display.float_format", lambda v: f"{v:,.4f}"):
        print(table.head(args.top)[view_cols].to_string(index=False))
    best = table.iloc[0]
    print("\n=== 最良候補 ===")
    print(best.to_string())
    if not best["robust"]:
        print("\n[note] 最良候補も robust 基準（勝率≥70% かつ p5≥-10%）未達。"
              "\n       → 戦略自体の見直し（逆張り/グリッド追加）が必要なサイン。")


if __name__ == "__main__":
    main()
