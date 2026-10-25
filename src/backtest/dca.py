"""毎月定額を入金して自動取引に回す「積立 × 自動取引」シミュレーション。

ランダムな N か月窓を複数本引き、各窓で
  - cash   : 入金のみ（元本）
  - dca_bh : 毎月の入金でその時点の価格を買い持ち（taker コスト・時価評価）
  - 戦略   : 入金を現金に積み、simulator.run(deposits=...) で取引
の最終資産を比べる。窓は全戦略・全ペアで共通（compare と同じ考え方）。

使い方:
  python -m src.backtest.dca --pairs btc_jpy eth_jpy --months 6 --monthly 10000 --runs 100
  python -m src.backtest.dca --pairs btc_jpy --latest          # 直近 N か月を 1 本だけ
"""
from __future__ import annotations

import argparse
import warnings
import json
import multiprocessing as mp
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import compare as C
from src.backtest import simulator as S
from src.backtest import strategies as ST
from src.config import ROOT
from src.data import resample as R

DEFAULT_SPEC = ("sma_cross", {"fast": 20, "slow": 60, "atr_stop_mult": 5.0,
                              "risk_per_trade": 0.01})


@dataclass(frozen=True)
class Job:
    pair: str
    strategy: str
    params: str          # JSON（Pool で渡すため）
    timeframe: str
    window_id: int
    t0: pd.Timestamp
    t1: pd.Timestamp
    monthly: float
    months: int


def deposit_schedule(t0: pd.Timestamp, months: int, monthly: float) -> pd.Series:
    """t0, t0+1M, ..., t0+(months-1)M に monthly ずつ入金。"""
    times = [t0 + pd.DateOffset(months=k) for k in range(months)]
    return pd.Series([float(monthly)] * months, index=pd.DatetimeIndex(times))


def sample_month_windows(start: pd.Timestamp, end: pd.Timestamp, months: int,
                         n: int, seed: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[start, end] 内に長さ months か月の窓を n 本引く。t0 は日境界(UTC 0時)。"""
    start = start.ceil("D")
    last_t0 = end - pd.DateOffset(months=months)
    room_days = int((last_t0 - start) / pd.Timedelta(days=1))
    if room_days < 0:
        raise ValueError(f"データ期間が {months} か月より短い")
    rng = np.random.default_rng(seed)
    out = []
    for k in rng.integers(0, room_days + 1, size=n):
        t0 = start + pd.Timedelta(days=int(k))
        out.append((t0, t0 + pd.DateOffset(months=months)))
    return out


def dca_buy_and_hold(close_1m: pd.Series, deposits: pd.Series, t1: pd.Timestamp,
                     cost: float) -> float:
    """各入金時刻の 1 分足終値で買い（片側コスト込み）、t1 直前の終値で時価評価。"""
    units = 0.0
    for t, amt in deposits.items():
        j = int(close_1m.index.searchsorted(t))
        if j >= len(close_1m):
            continue
        units += float(amt) / (float(close_1m.iloc[j]) * (1.0 + cost))
    j1 = int(close_1m.index.searchsorted(t1))
    return units * float(close_1m.iloc[max(0, j1 - 1)])


def _eval_job(job: Job) -> dict[str, Any]:
    params = {**json.loads(job.params), "initial_equity": 0.0}
    rdf = C._FRAMES[(job.pair, job.timeframe)]
    pre = max(2 * ST.warmup_bars(job.strategy, {**S.DEFAULT_PARAMS, **params}),
              C.MIN_PREFIX_BARS)
    blk, warm_short = C.slice_window(rdf, job.t0, job.t1, pre)
    deps = deposit_schedule(job.t0, job.months, job.monthly)
    eq, m = S.run(blk, params, R.bars_per_year(job.timeframe), job.strategy,
                  no_entry_before=job.t0, deposits=deps)
    p = {**S.DEFAULT_PARAMS, **params}
    cost = (p["fee_maker"] + p["maker_slippage"]) if p.get("order_type") == "maker" \
        else (p["fee"] + p["slippage"])
    close_1m = C._FRAMES[(job.pair, "1m")]["close"]
    invested = float(deps.sum())
    final = float(eq.iloc[-1])
    bh = dca_buy_and_hold(close_1m, deps, job.t1, cost)
    # 入金を除いた損益カーブで最大 DD（入金で回復したように見えるのを防ぐ）
    cum_dep = pd.Series(0.0, index=eq.index)
    for t, amt in deps.items():
        j = int(eq.index.searchsorted(t))
        if j < len(eq):
            cum_dep.iloc[j:] += float(amt)
    pnl_curve = (eq - cum_dep).loc[job.t0:]
    peak = pnl_curve.cummax()
    worst = float((pnl_curve - peak).min()) if len(pnl_curve) else 0.0
    return {
        "pair": job.pair, "strategy": job.strategy, "timeframe": job.timeframe,
        "params": job.params, "window_id": job.window_id, "t0": job.t0, "t1": job.t1,
        "invested": invested, "final": final, "pnl": final - invested,
        "ret": final / invested - 1.0 if invested else 0.0,
        "bh_final": bh, "bh_pnl": bh - invested,
        "bh_ret": bh / invested - 1.0 if invested else 0.0,
        "worst_pnl_dd": worst, "n_trades": m.n_trades, "warm_short": warm_short,
    }


def build_jobs(pairs, specs, timeframes, windows, monthly, months) -> list[Job]:
    return [Job(pair, name, json.dumps(p, sort_keys=True), tf, i, t0, t1, monthly, months)
            for pair in pairs for name, p in specs for tf in timeframes
            for i, (t0, t1) in enumerate(windows)]


def run_dca(frames, pairs, specs, timeframes, windows, monthly, months,
            workers) -> pd.DataFrame:
    jobs = build_jobs(pairs, specs, timeframes, windows, monthly, months)
    if workers <= 1:
        C._init(frames)
        rows = [_eval_job(j) for j in jobs]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(workers, initializer=C._init, initargs=(frames,)) as pool:
            rows = list(pool.imap_unordered(_eval_job, jobs, chunksize=4))
    return (pd.DataFrame(rows)
            .sort_values(["pair", "strategy", "params", "timeframe", "window_id"])
            .reset_index(drop=True))


def _dist_row(pair: str, st: str, tf: str, params: str, g: pd.DataFrame,
              final: pd.Series) -> dict[str, Any]:
    inv = g["invested"]
    return {
        "pair": pair, "strategy": st, "timeframe": tf, "params": params,
        "invested": inv.median(),
        "win_rate": (final > inv).mean(),
        "beat_dca_bh": (final > g["bh_final"]).mean(),
        "median_final": final.median(), "p5_final": final.quantile(0.05),
        "p95_final": final.quantile(0.95),
        "median_ret": (final / inv - 1.0).median(),
        "p5_ret": (final / inv - 1.0).quantile(0.05),
        "worst_pnl_dd": g["worst_pnl_dd"].min(), "med_trades": g["n_trades"].median(),
    }


def summarize(rows: pd.DataFrame, blends: list[float] | None = None) -> pd.DataFrame:
    """窓ごとの最終資産の分布。末尾にペアごとの dca_buy_and_hold 行。

    blends=[k, ...] を渡すと、各戦略について「(1-k) × 積立買い持ち + k × 戦略」の
    合成行（strategy='blend(k)'）を追加する。最終資産は現金に線形なので窓ごとに
    厳密に合成できる（最小注文数量は未モデル化）。worst_pnl_dd / med_trades は
    戦略単体の値をそのまま載せる。"""
    out = []
    for (pair, st, tf, params), g in rows.groupby(
            ["pair", "strategy", "timeframe", "params"], sort=False):
        out.append(_dist_row(pair, st, tf, params, g, g["final"]))
        for k in blends or []:
            mixed = (1.0 - k) * g["bh_final"] + k * g["final"]
            out.append(_dist_row(pair, f"blend({k:g})", tf, params, g, mixed)
                       | {"params": json.dumps({**json.loads(params), "_base": st},
                                               sort_keys=True)})
    for pair, g in rows.drop_duplicates(["pair", "window_id"]).groupby("pair", sort=False):
        out.append({
            "pair": pair, "strategy": "dca_buy_and_hold", "timeframe": "-", "params": "{}",
            "invested": g["invested"].median(),
            "win_rate": (g["bh_pnl"] > 0).mean(), "beat_dca_bh": np.nan,
            "median_final": g["bh_final"].median(), "p5_final": g["bh_final"].quantile(0.05),
            "p95_final": g["bh_final"].quantile(0.95),
            "median_ret": g["bh_ret"].median(), "p5_ret": g["bh_ret"].quantile(0.05),
            "worst_pnl_dd": np.nan, "med_trades": np.nan,
        })
    return pd.DataFrame(out)


def main() -> None:
    warnings.filterwarnings("ignore", category=RuntimeWarning)  # 資産 0 区間の sharpe 計算
    ap = argparse.ArgumentParser(description="毎月定額積立 × 自動取引のシミュレーション")
    ap.add_argument("--db", default="data/candles.sqlite")
    ap.add_argument("--pairs", nargs="+", default=["btc_jpy", "eth_jpy"])
    ap.add_argument("--strategies", nargs="+", default=[],
                    help="戦略名（既定 params）。省略時は 4h sma_cross 20/60 ATR×5")
    ap.add_argument("--spec", action="append", type=C._parse_spec, default=[],
                    help="NAME:k=v,... 形式で params 付き戦略を追加")
    ap.add_argument("--timeframes", nargs="+", default=["4h"], choices=list(R.TIMEFRAMES))
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--monthly", type=float, default=10_000)
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--latest", action="store_true", help="直近 N か月を 1 本だけ走らせる")
    ap.add_argument("--blend", nargs="+", type=float, default=[],
                    help="積立買い持ちと戦略の配分 k（0..1、戦略側の比率）。合成行を要約に追加")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1))
    ap.add_argument("--out", default=None)
    ap.add_argument("--out-rows", default=None)
    args = ap.parse_args()

    specs: list[tuple[str, dict[str, Any]]] = [(s, {}) for s in args.strategies] + args.spec
    if not specs:
        specs = [DEFAULT_SPEC]
    tag = args.pairs[0] if len(args.pairs) == 1 else "all"
    suffix = "latest" if args.latest else str(args.seed)
    out_rows = Path(args.out_rows) if args.out_rows else \
        ROOT / "state" / f"dca_rows_{tag}_{suffix}.csv"
    out_sum = Path(args.out) if args.out else ROOT / "state" / f"dca_{tag}_{suffix}.csv"

    t_start = time.perf_counter()
    frames = C.load_frames(args.db, args.pairs, args.timeframes)
    start, end = C.data_range(frames, args.pairs)
    if args.latest:
        t1 = end.floor("D")
        windows = [(t1 - pd.DateOffset(months=args.months), t1)]
    else:
        windows = sample_month_windows(start, end, args.months, args.runs, args.seed)
    print(f"data {start} .. {end}; pairs={args.pairs} specs={len(specs)} "
          f"tf={args.timeframes} windows={len(windows)}x{args.months}M "
          f"monthly={args.monthly:.0f} workers={args.workers}")
    rows = run_dca(frames, args.pairs, specs, args.timeframes, windows,
                   args.monthly, args.months, args.workers)
    table = summarize(rows, args.blend)
    out_rows.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out_rows, index=False)
    table.to_csv(out_sum, index=False)
    with pd.option_context("display.width", 200, "display.max_columns", 40,
                           "display.float_format", "{:,.3f}".format):
        print(f"\n=== 積立 {args.monthly:,.0f}円 × {args.months}か月 "
              f"(元本 {args.monthly * args.months:,.0f}円) ===")
        show = table.copy()
        show["params"] = show["params"].map(
            lambda j: ",".join(f"{k}={v}" for k, v in json.loads(j).items()))
        print(show.to_string(index=False))
    print(f"\nrows -> {out_rows}\nsummary -> {out_sum}")
    print(f"elapsed {time.perf_counter() - t_start:.1f}s")


if __name__ == "__main__":
    main()
