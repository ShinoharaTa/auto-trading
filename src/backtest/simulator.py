"""最小バックテスタ。

設計方針:
- 戦略(シグナル)とリスク管理(サイズ/損切り)を分離する。シグナルは
  strategies.py の戦略関数が生成し、ここは固定割合サイジング + ATR損切りに専念。
- ロングのみ。手数料/スリッページを考慮。

戻り値: (equity: pd.Series, metrics: Metrics)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import metrics as M
from src.backtest import strategies as ST
from src.config import ROOT

DEFAULT_PARAMS: dict[str, Any] = {
    "atr_period": 30,     # ATR算出期間(本)
    "atr_stop_mult": 2.0, # 損切り幅 = ATR * mult
    "risk_per_trade": 0.01,  # 1トレードで資産の何割をリスクに晒すか
    "order_type": "taker",  # "taker"(成行) or "maker"(指値)。intraday採算の分岐点。
    "fee": 0.0012,        # taker 片側手数料(bitbank ≒ 0.12%)
    "slippage": 0.0005,   # taker 片側スリッページ
    "fee_maker": -0.0002, # maker 片側手数料(bitbank はリベート≒ -0.02%)
    "maker_slippage": 0.0002,  # maker の保守的コスト(約定しない/逆選択を粗くモデル化)
    "initial_equity": 1_000_000.0,
    # --- Risk Manager ---
    "max_position_frac": 0.25,  # 1建玉に充てる資産の上限割合。実質フルポジ防止の要。
    "daily_loss_limit": 0.05,   # その日の損失がこの割合を超えたら新規停止(翌日リセット)。None で無効。
}


def load_candles(db_path: str, pair: str, start_ms: int | None = None,
                 end_ms: int | None = None) -> pd.DataFrame:
    p = Path(db_path)
    if not p.is_absolute():
        p = ROOT / p
    q = "SELECT ts, open, high, low, close, volume FROM candles WHERE pair=?"
    args: list[Any] = [pair]
    if start_ms is not None:
        q += " AND ts>=?"; args.append(start_ms)
    if end_ms is not None:
        q += " AND ts<?"; args.append(end_ms)
    q += " ORDER BY ts"
    with sqlite3.connect(p) as conn:
        df = pd.read_sql_query(q, conn, params=args)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("dt")


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def run(df: pd.DataFrame, params: dict[str, Any] | None = None,
        bars_per_year: float = 525_600.0,
        strategy: str = "sma_cross") -> tuple[pd.Series, M.Metrics]:
    p = {**DEFAULT_PARAMS, **(params or {})}
    if len(df) < ST.warmup_bars(strategy, p) + 2:
        eq = pd.Series([p["initial_equity"]], index=df.index[:1] if len(df) else None)
        return eq, M.from_run(eq, [], bars_per_year)

    close = df["close"]
    atr = _atr(df, p["atr_period"])
    # 戦略がシグナルを生成（戦略側で先読み防止の shift 済み）
    long_signal = ST.STRATEGIES[strategy](df, p)

    equity = p["initial_equity"]
    cash = equity
    units = 0.0
    entry_price = 0.0
    stop_price = 0.0
    trade_entry_equity = 0.0
    # 執行コスト(片側)。maker は手数料リベート込みでtakerよりはるかに小さい。
    if p.get("order_type") == "maker":
        cost = p["fee_maker"] + p["maker_slippage"]
    else:
        cost = p["fee"] + p["slippage"]

    eq_curve: list[float] = []
    trade_pnls: list[float] = []
    idx = df.index
    days = idx.normalize()                 # 各バーの所属日(UTC)。日次サーキットブレーカ用。
    max_frac = float(p["max_position_frac"])
    daily_limit = p.get("daily_loss_limit")
    cur_day = None
    day_start_equity = equity

    for i in range(len(df)):
        price = float(close.iloc[i])
        sig = int(long_signal.iloc[i])
        a = atr.iloc[i]

        # 日替わりでその日の基準資産を更新（日次損失の起点）
        day = days[i]
        if day != cur_day:
            cur_day = day
            day_start_equity = equity

        # --- 保有中: 損切り or シグナル消失でクローズ ---
        if units > 0:
            exit_now = price <= stop_price or sig == 0
            if exit_now:
                fill = price * (1 - cost)
                cash += units * fill
                trade_pnls.append(cash - trade_entry_equity)
                units = 0.0

        # サーキットブレーカ: その日の損失が上限超なら新規エントリーを止める
        halted = (daily_limit is not None
                  and equity <= day_start_equity * (1.0 - float(daily_limit)))

        # --- 無ポジ: シグナル点灯 & ATR有効 & 非停止ならエントリー ---
        if units == 0 and sig == 1 and not halted and not np.isnan(a) and a > 0:
            stop_dist = p["atr_stop_mult"] * float(a)
            risk_cash = equity * p["risk_per_trade"]
            size = risk_cash / stop_dist           # 損切りまでの距離で数量を決める
            fill = price * (1 + cost)
            # サイズ上限: ①1%リスク基準 ②最大ポジ比率 ③現金(レバ無し) の最小
            size = min(size, max_frac * equity / fill, cash / fill)
            if size > 0:
                units = size
                entry_price = fill
                stop_price = price - stop_dist
                cash -= units * fill
                trade_entry_equity = equity

        equity = cash + units * price
        eq_curve.append(equity)

    eq = pd.Series(eq_curve, index=idx)
    return eq, M.from_run(eq, trade_pnls, bars_per_year)


if __name__ == "__main__":
    from src.config import load

    cfg = load()
    d = cfg["data"]
    data = load_candles(d["db_path"], d["pair"])
    print(f"loaded {len(data)} bars for {d['pair']}")
    eq, m = run(data)
    for k, v in m.as_dict().items():
        print(f"  {k:14s}: {v:,.4f}" if isinstance(v, float) else f"  {k:14s}: {v}")
