"""同一ランダム窓での戦略横並び比較（60日窓・並列）。

montecarlo は戦略ごとに別々の区間を引くため、戦略同士を同条件で比べられない。
ここでは「カレンダー時刻」で窓を N 本引き、全ペア・全戦略・全時間足を
同じ窓集合で評価する。ジョブは multiprocessing(fork) で並列に回す。

- 窓 [t0, t1) の前に max(2×warmup_bars, 200) 本のプレフィックスを足して切り出し、
  指標をウォームアップさせる。simulator.run の no_entry_before=t0 で窓開始前の
  新規エントリーを抑止するので、total_return / max_drawdown / n_trades は
  窓本体の成績と一致する。そのため montecarlo（プレフィックスなし）とは数値が一致しない。
- 窓の開始 t0 は 4h 境界に丸める。全時間足で窓のカレンダー範囲が一致し、B&H とも揃う。
- 窓は互いに重なる（180日に60日窓は非重複で3本しか取れない）。p5 や勝ち区間率は
  見かけより粗い。用途は戦略同士の相対比較に限る。
- 子プロセスで SQLite を開かない。親でロード→リサンプル→fork し、子は CoW で共有。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import simulator as S
from src.backtest import strategies as ST
from src.config import ROOT
from src.data import resample as R

# 既定の比較時間足（5m は重いので --timeframes で明示したときのみ）
DEFAULT_TIMEFRAMES = ["15m", "30m", "1h", "4h"]

# 窓開始の丸め単位（分）。最上位の時間足 4h に揃える
WINDOW_ALIGN_MIN = 240
# プレフィックスの下限本数。params={} だと warmup_bars が戦略関数側の既定値を
# 見ないため、下限で指標のウォームアップを確保する（取引は no_entry_before で抑止）
MIN_PREFIX_BARS = 200

# (pair, timeframe) -> リサンプル済み DataFrame。(pair, "1m") は B&H 計算用の1分足。
# fork 前に親で埋め、子は読むだけ。
_FRAMES: dict[tuple[str, str], pd.DataFrame] = {}


@dataclass(frozen=True)
class Job:
    pair: str
    strategy: str
    timeframe: str
    params: str          # json 文字列（sort_keys）
    window_id: int
    t0: pd.Timestamp
    t1: pd.Timestamp


def sample_windows(start: pd.Timestamp, end: pd.Timestamp, days: int, n: int,
                   seed: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[start, end] 内に長さ days の窓を n 本引く。t0 は 4h 境界に丸める。"""
    span = pd.Timedelta(days=days)
    start = start.ceil(f"{WINDOW_ALIGN_MIN}min")   # 丸めの基準を 4h 境界にする
    room_min = int((end - start - span) / pd.Timedelta(minutes=1))
    if room_min < 0:
        raise ValueError(f"データ期間 {end - start} が窓長 {days}日 より短い")
    rng = np.random.default_rng(seed)
    offs = rng.integers(0, room_min + 1, size=n)
    out = []
    for k in offs:
        t0 = start + pd.Timedelta(minutes=int(k) // WINDOW_ALIGN_MIN * WINDOW_ALIGN_MIN)
        out.append((t0, t0 + span))
    return out


def slice_window(rdf: pd.DataFrame, t0: pd.Timestamp, t1: pd.Timestamp,
                 prefix_bars: int) -> tuple[pd.DataFrame, bool]:
    """窓 [t0, t1) の前に prefix_bars 本を足して切り出す。戻りの bool = プレフィックス不足。"""
    i0 = int(rdf.index.searchsorted(t0))
    i1 = int(rdf.index.searchsorted(t1))
    warm_short = i0 - prefix_bars < 0
    return rdf.iloc[max(0, i0 - prefix_bars):i1], warm_short


def build_jobs(pairs: list[str], specs: list[tuple[str, dict[str, Any]]],
               timeframes: list[str],
               windows: list[tuple[pd.Timestamp, pd.Timestamp]]) -> list[Job]:
    jobs = []
    for pair in pairs:
        for strategy, params in specs:
            pj = json.dumps(params, sort_keys=True)
            for tf in timeframes:
                for wid, (t0, t1) in enumerate(windows):
                    jobs.append(Job(pair, strategy, tf, pj, wid, t0, t1))
    return jobs


def _init(frames: dict[tuple[str, str], pd.DataFrame]) -> None:
    global _FRAMES
    _FRAMES = frames


def _bh_return(pair: str, t0: pd.Timestamp, t1: pd.Timestamp) -> float:
    """窓本体のみの買い持ちリターン。1分足で取るので時間足によらず同じ値になる。"""
    close = _FRAMES[(pair, "1m")]["close"]
    j0 = int(close.index.searchsorted(t0))
    j1 = int(close.index.searchsorted(t1))
    if j1 - j0 < 1:
        return float("nan")
    return float(close.iloc[j1 - 1] / close.iloc[j0] - 1.0)


def _eval_job(job: Job) -> dict[str, Any]:
    params = json.loads(job.params)
    rdf = _FRAMES[(job.pair, job.timeframe)]
    pre = max(2 * ST.warmup_bars(job.strategy, {**S.DEFAULT_PARAMS, **params}),
              MIN_PREFIX_BARS)
    blk, warm_short = slice_window(rdf, job.t0, job.t1, pre)
    _, m = S.run(blk, params, R.bars_per_year(job.timeframe), job.strategy,
                 no_entry_before=job.t0)
    return {
        "pair": job.pair,
        "strategy": job.strategy,
        "timeframe": job.timeframe,
        "params": job.params,
        "window_id": job.window_id,
        "t0": job.t0,
        "t1": job.t1,
        "total_return": m.total_return,
        "max_drawdown": m.max_drawdown,
        "sharpe": m.sharpe,
        "profit_factor": m.profit_factor,
        "win_rate": m.win_rate,
        "n_trades": m.n_trades,
        "bh_return": _bh_return(job.pair, job.t0, job.t1),
        "warm_short": warm_short,
    }


def load_frames(db: str, pairs: list[str],
                timeframes: list[str]) -> dict[tuple[str, str], pd.DataFrame]:
    """親プロセスでロード→リサンプル。子では SQLite を開かない。"""
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for pair in pairs:
        df1m = S.load_candles(db, pair)
        frames[(pair, "1m")] = df1m
        for tf in timeframes:
            frames[(pair, tf)] = R.resample_ohlcv(df1m, tf)
    return frames


def data_range(frames: dict[tuple[str, str], pd.DataFrame],
               pairs: list[str]) -> tuple[pd.Timestamp, pd.Timestamp]:
    """全ペアで共通に存在する期間（1分足基準）。"""
    starts = [frames[(p, "1m")].index[0] for p in pairs]
    ends = [frames[(p, "1m")].index[-1] + pd.Timedelta(minutes=1) for p in pairs]
    return max(starts), min(ends)


def run_compare(db: str, pairs: list[str], specs: list[tuple[str, dict[str, Any]]],
                timeframes: list[str],
                windows: list[tuple[pd.Timestamp, pd.Timestamp]],
                workers: int, extra_params: dict[str, Any],
                frames: dict[tuple[str, str], pd.DataFrame] | None = None) -> pd.DataFrame:
    """窓別の行（1ジョブ1行）を返す。workers=1 は Pool を使わず逐次。"""
    if frames is None:
        frames = load_frames(db, pairs, timeframes)
    merged = [(name, {**p, **extra_params}) for name, p in specs]
    jobs = build_jobs(pairs, merged, timeframes, windows)
    if workers <= 1:
        _init(frames)
        rows = [_eval_job(j) for j in jobs]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(workers, initializer=_init, initargs=(frames,)) as pool:
            rows = list(pool.imap_unordered(_eval_job, jobs, chunksize=8))
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return (out.sort_values(["pair", "strategy", "params", "timeframe", "window_id"])
            .reset_index(drop=True))


def label_bh(bh_return: pd.Series, label_th: float = 0.05) -> pd.Series:
    """窓の局面ラベル: B&H が +th 超なら up / -th 未満なら down / それ以外 flat。"""
    lab = np.where(bh_return > label_th, "up",
                   np.where(bh_return < -label_th, "down", "flat"))
    return pd.Series(lab, index=bh_return.index)


def _agg(g: pd.DataFrame, ret: pd.Series, labels: pd.Series,
         is_bh: bool = False) -> dict[str, Any]:
    p5 = float(ret.quantile(0.05))
    med = float(ret.median())
    return {
        "n_win": int(len(g)),
        "win_rate": float((ret > 0).mean()),
        "median_ret": med,
        "p5_ret": p5,
        "worst_dd": float(g["max_drawdown"].max()) if "max_drawdown" in g else float("nan"),
        "med_trades": float(g["n_trades"].median()) if "n_trades" in g else float("nan"),
        "bh_median": float(g["bh_return"].median()),
        "beat_bh": float("nan") if is_bh else float((ret > g["bh_return"]).mean()),
        "up_median": float(ret[labels == "up"].median()),
        "flat_median": float(ret[labels == "flat"].median()),
        "down_median": float(ret[labels == "down"].median()),
        "score": med + p5,
    }


def summarize(rows: pd.DataFrame, label_th: float = 0.05) -> pd.DataFrame:
    """戦略×時間足で集計（score 降順）。末尾にペアごとの buy_and_hold 行を付ける。"""
    labels = label_bh(rows["bh_return"], label_th)
    out = []
    for (pair, strategy, tf, params), g in rows.groupby(
            ["pair", "strategy", "timeframe", "params"], sort=False):
        out.append({"pair": pair, "strategy": strategy, "timeframe": tf, "params": params,
                    **_agg(g, g["total_return"], labels.loc[g.index])})
    table = pd.DataFrame(out).sort_values("score", ascending=False)

    # B&H: 窓ごとに1回だけ数える（bh_return は戦略・時間足によらず同じ値）
    bh_rows = []
    uniq = rows.drop_duplicates(["pair", "window_id"])
    for pair, g in uniq.groupby("pair", sort=False):
        g = g[["bh_return"]]
        bh_rows.append({"pair": pair, "strategy": "buy_and_hold", "timeframe": "-",
                        "params": "{}",
                        **_agg(g, g["bh_return"], labels.loc[g.index], is_bh=True)})
    return pd.concat([table, pd.DataFrame(bh_rows)], ignore_index=True)


def _convert(v: str) -> Any:
    """数値に変換できれば int/float、できなければ文字列のまま。"""
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f


def _parse_kv(s: str) -> tuple[str, Any]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"KEY=VALUE 形式ではない: {s!r}")
    k, v = s.split("=", 1)
    return k.strip(), _convert(v.strip())


def _parse_spec(s: str) -> tuple[str, dict[str, Any]]:
    """NAME:k=v,k=v -> (NAME, {k: v})"""
    name, _, rest = s.partition(":")
    name = name.strip()
    if name not in ST.STRATEGIES:
        raise argparse.ArgumentTypeError(f"未登録の戦略: {name!r}")
    params = dict(_parse_kv(kv) for kv in rest.split(",") if kv.strip())
    return name, params


def main() -> None:
    from src.backtest import optimize as O
    from src.config import load

    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser(
        description="同一ランダム窓で複数戦略を横並び比較する。窓は互いに重なるため "
                    "p5/勝ち区間率は見かけより粗い。戦略同士の相対比較に使うこと。")
    ap.add_argument("--pairs", nargs="+", default=[d["pair"]])
    ap.add_argument("--db", default=d["db_path"])
    ap.add_argument("--strategies", nargs="+", default=None,
                    help="戦略名 or all（ST.STRATEGIES の全キー）。--spec のみ指定時は省略可")
    ap.add_argument("--spec", action="append", type=_parse_spec, default=[],
                    help="NAME:k=v,k=v 形式で params 付きの戦略を追加（繰返し可）")
    ap.add_argument("--timeframes", nargs="+", default=DEFAULT_TIMEFRAMES,
                    choices=list(R.TIMEFRAMES))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1))
    ap.add_argument("--param", action="append", type=_parse_kv, default=[],
                    help="KEY=VALUE を全戦略の params に上書き（繰返し可）")
    ap.add_argument("--grid", action="store_true",
                    help="optimize.STRATEGY_GRIDS を展開（既定は各戦略の既定 params 1 組）")
    ap.add_argument("--label-th", type=float, default=0.05)
    ap.add_argument("--out", default=None)
    ap.add_argument("--out-rows", default=None)
    args = ap.parse_args()

    names: list[str] = []
    if args.strategies is None and not args.spec:
        args.strategies = ["all"]
    for s in args.strategies or []:
        if s == "all":
            names.extend(ST.STRATEGIES)
        elif s in ST.STRATEGIES:
            names.append(s)
        else:
            ap.error(f"未登録の戦略: {s!r}（候補: all, {', '.join(ST.STRATEGIES)}）")
    specs: list[tuple[str, dict[str, Any]]] = []
    for name in dict.fromkeys(names):
        if args.grid and name in O.STRATEGY_GRIDS:
            specs.extend((name, p) for p in O._param_combos(O.STRATEGY_GRIDS[name]))
        else:
            specs.append((name, {}))
    specs.extend(args.spec)
    extra = dict(args.param)

    tag = args.pairs[0] if len(args.pairs) == 1 else "all"
    out_rows = Path(args.out_rows) if args.out_rows else \
        ROOT / "state" / f"compare_rows_{tag}_{args.seed}.csv"
    out_sum = Path(args.out) if args.out else ROOT / "state" / f"compare_{tag}_{args.seed}.csv"

    t_start = time.perf_counter()
    frames = load_frames(args.db, args.pairs, args.timeframes)
    start, end = data_range(frames, args.pairs)
    windows = sample_windows(start, end, args.days, args.runs, args.seed)
    n_jobs = len(args.pairs) * len(specs) * len(args.timeframes) * len(windows)
    print(f"data {start} .. {end}; pairs={args.pairs} specs={len(specs)} "
          f"tf={args.timeframes} windows={len(windows)}x{args.days}d "
          f"jobs={n_jobs} workers={args.workers}")

    rows = run_compare(args.db, args.pairs, specs, args.timeframes, windows,
                       args.workers, extra, frames=frames)
    rows["bh_label"] = label_bh(rows["bh_return"], args.label_th)
    table = summarize(rows, args.label_th)

    out_rows.parent.mkdir(parents=True, exist_ok=True)
    out_sum.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out_rows, index=False)
    table.to_csv(out_sum, index=False)

    print("\n=== 同一窓比較（score=median_ret+p5_ret 降順、末尾は買い持ち）===")
    with pd.option_context("display.width", 250, "display.max_columns", None,
                           "display.max_colwidth", 60,
                           "display.float_format", lambda v: f"{v:,.4f}"):
        print(table.to_string(index=False))
    print(f"\nrows -> {out_rows}\nsummary -> {out_sum}")
    print(f"elapsed {time.perf_counter() - t_start:.1f}s")


if __name__ == "__main__":
    main()
