"""[5] ウォークフォワード再評価ループ。

過剰最適化を検出するための要。手順:
  1. 学習窓(直近 train_days 日)でモンテカルロ最適化 → 最良(時間足×パラメータ)を選ぶ
  2. その設定を 検証窓(次の test_days 日) で **1回だけ** バックテスト（未知データ=OOS）
  3. 窓を test_days だけ前進させて反復

学習窓で選んだ設定が検証窓でも通用するか（=本物の優位性か、ただの過剰適合か）を
OOSリターンの系列で評価する。OOSを複利でつなげば「実運用に近い資産推移」になる。

これがユーザー構想の「週次・月次で再評価する仕組み」の実体。
test_days=7 で週次、test_days=30 で月次の再評価に対応。
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import optimize as OPT
from src.backtest import simulator as S
from src.data import resample as R


# optimize 結果のうちパラメータでない列（残りは全てパラメータとして扱う）
_META_COLS = {"strategy", "timeframe", "score", "win_rate", "median_ret",
              "p5_ret", "worst_dd", "med_trades", "robust"}


def _params_from_row(row: pd.Series, strategy: str) -> dict[str, Any]:
    """メタ列以外をパラメータとして抽出（任意グリッド/order_type等に追従）。"""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in _META_COLS or pd.isna(v):
            continue
        out[k] = v.item() if hasattr(v, "item") else v  # numpy -> native(JSON化可能)
    return out


def walk_forward(
    df: pd.DataFrame,
    strategy: str = "sma_cross",
    timeframes: list[str] | None = None,
    train_days: int = 180,
    test_days: int = 30,
    opt_period: str = "1m",
    n_runs: int = 150,
    seed: int = 42,
    grid: dict[str, list[Any]] | None = None,
) -> pd.DataFrame:
    timeframes = timeframes or OPT.DEFAULT_TIMEFRAMES
    start, end = df.index[0], df.index[-1]
    train_td = pd.Timedelta(days=train_days)
    test_td = pd.Timedelta(days=test_days)

    total_steps = max(int((end - (start + train_td)) / test_td), 0)
    steps: list[dict[str, Any]] = []
    cur = start + train_td
    i = 0
    while cur + test_td <= end:
        i += 1
        train = df.loc[cur - train_td: cur]
        test = df.loc[cur: cur + test_td]
        print(f"[walkforward] step {i}/{total_steps} "
              f"train≤{cur.date()} test→{(cur + test_td).date()}", flush=True)
        # データ欠損で窓が空/過少なら無人運用でも落ちないようスキップ
        if test.empty or len(train) < 2:
            cur += test_td
            continue
        table = OPT.optimize(train, strategy, timeframes, grid=grid, period=opt_period,
                             n_runs=n_runs, seed=seed, verbose=False)
        if table.empty:
            cur += test_td
            continue
        best = table.iloc[0]
        tf = best["timeframe"]
        params = _params_from_row(best, strategy)

        # --- OOS: 検証窓で1回だけ実行（最適化に未使用のデータ）---
        rtest = R.resample_ohlcv(test, tf)
        if rtest.empty:
            cur += test_td
            continue
        bpy = R.bars_per_year(tf)
        _, m = S.run(rtest, params, bpy, strategy)

        steps.append({
            "test_start": test.index[0].date(),
            "test_end": test.index[-1].date(),
            "timeframe": tf,
            "params": params,
            "is_score": float(best["score"]),
            "is_median_ret": float(best["median_ret"]),  # 学習窓での期待(楽観側)
            "oos_return": float(m.total_return),          # 検証窓の実績(本命)
            "oos_dd": float(m.max_drawdown),
            "oos_trades": int(m.n_trades),
        })
        cur += test_td
    return pd.DataFrame(steps)


def summarize(steps: pd.DataFrame) -> dict[str, Any]:
    if steps.empty:
        return {"n_steps": 0}
    oos = steps["oos_return"]
    equity = float((1.0 + oos).prod())  # OOSを複利でつないだ最終倍率
    # パラメータ安定性: 採用された時間足が何回切り替わったか
    tf_switches = int((steps["timeframe"] != steps["timeframe"].shift()).sum() - 1)
    return {
        "n_steps": int(len(steps)),
        "oos_win_rate": float((oos > 0).mean()),
        "oos_mean_return": float(oos.mean()),
        "oos_median_return": float(oos.median()),
        "oos_worst": float(oos.min()),
        "oos_worst_dd": float(steps["oos_dd"].max()),
        "compounded_equity_x": equity,            # 例: 1.35 = +35%
        # 過剰最適化ギャップ: 学習窓の期待 - 検証窓の実績（大きいほど過適合疑い）
        "overfit_gap": float(steps["is_median_ret"].mean() - oos.mean()),
        "timeframe_switches": tf_switches,
    }


def main() -> None:
    from src.config import load

    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default=d["pair"])
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--strategy", default="sma_cross", choices=list(OPT.STRATEGY_GRIDS))
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30, help="30=月次, 7=週次")
    ap.add_argument("--runs", type=int, default=150)
    args = ap.parse_args()

    data = S.load_candles(args.db, args.pair)
    print(f"loaded {len(data):,} bars; walk-forward strategy={args.strategy} "
          f"train={args.train_days}d test={args.test_days}d ...")
    steps = walk_forward(data, args.strategy, train_days=args.train_days,
                         test_days=args.test_days, n_runs=args.runs)
    if steps.empty:
        print("有効なステップなし（データ/期間設定を見直し）。")
        return

    print("\n=== 各検証窓(OOS)の実績 ===")
    with pd.option_context("display.width", 200, "display.max_columns", None,
                           "display.float_format", lambda v: f"{v:,.4f}"):
        show = steps.copy()
        show["params"] = show["params"].apply(
            lambda p: ",".join(f"{k}={int(v) if float(v).is_integer() else v}"
                               for k, v in p.items()))
        print(show.to_string(index=False))

    print("\n=== OOS 総括 ===")
    for k, v in summarize(steps).items():
        print(f"  {k:20s}: {v:,.4f}" if isinstance(v, float) else f"  {k:20s}: {v}")


if __name__ == "__main__":
    main()
