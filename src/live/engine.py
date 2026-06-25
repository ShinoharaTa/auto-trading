"""ペーパートレード執行エンジン（口座×ペア、1ステップ）。

確定した新しい足ごとに:
  1. 前足で出した maker 指値の約定判定（その足の安値が指値を貫けば約定、でなければ見送り=約定率を実測）
  2. 保有中なら 損切り(安値が逆指値到達)/シグナル消失 で成行クローズ
  3. 無ポジでシグナル点灯なら、taker=即時成行 / maker=指値を出して次足判定に回す

入りは maker(指値)、出口は常に成行(taker) ＝ バックテスト(両側maker)より保守的。
リスク管理(1%リスク・最大ポジ比率・現金制約)は backtest と同じ思想。
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from src.backtest import strategies as ST
from src.backtest.simulator import DEFAULT_PARAMS, _atr
from src.live.store import PaperStore

BarsProvider = Callable[[str, str], pd.DataFrame]


def _entry_cost(p: dict[str, Any], order_type: str) -> float:
    if order_type == "maker":
        return p["fee_maker"] + p["maker_slippage"]
    return p["fee"] + p["slippage"]


def _exit_cost(p: dict[str, Any]) -> float:
    return p["fee"] + p["slippage"]  # 出口は確実に逃げるため常に成行(taker)


def _size(equity: float, cash: float, atr: float, p: dict[str, Any],
          fill_price: float) -> tuple[float, float]:
    stop_dist = p["atr_stop_mult"] * atr
    if stop_dist <= 0:
        return 0.0, 0.0
    size = (equity * p["risk_per_trade"]) / stop_dist
    size = min(size, p["max_position_frac"] * equity / fill_price, cash / fill_price)
    return max(size, 0.0), stop_dist


def _account_equity(store: PaperStore, account: str, price_map: dict[str, float]) -> float:
    cash = store.get_cash(account)
    inv = 0.0
    for pos in store.all_positions(account):
        px = price_map.get(pos["pair"], pos["entry_price"])
        inv += pos["units"] * px
    return cash + inv


def step_account(store: PaperStore, acct: dict[str, Any], now_ts: int,
                 bars_provider: BarsProvider) -> dict[str, float]:
    """1口座を1ステップ進め、口座評価額(JPY)のスナップショットを記録して返す。"""
    name = acct["name"]
    strategy = acct["strategy"]
    order_type = acct["order_type"]
    timeframe = acct["timeframe"]
    p = {**DEFAULT_PARAMS, **acct.get("params", {}), "order_type": order_type}
    store.ensure_account(name, acct.get("base_jpy", 100_000.0), now_ts)

    price_map: dict[str, float] = {}
    for pair in acct["pairs"]:
        bars = bars_provider(pair, timeframe)
        warm = ST.warmup_bars(strategy, p) + 2
        if bars is None or len(bars) < warm:
            continue
        last = bars.iloc[-1]
        price_map[pair] = float(last["close"])
        bar_ts = int(bars.index[-1].value // 1_000_000)  # ns->ms
        if bar_ts <= store.last_bar_ts(name, pair):
            continue  # まだ新しい確定足が来ていない
        store.set_last_bar_ts(name, pair, bar_ts)
        _step_pair(store, name, pair, bars, p, strategy, order_type, now_ts, price_map)

    equity = _account_equity(store, name, price_map)
    store.add_snapshot(name, now_ts, equity)
    return {"equity": equity}


def _step_pair(store: PaperStore, name: str, pair: str, bars: pd.DataFrame,
               p: dict[str, Any], strategy: str, order_type: str,
               now_ts: int, price_map: dict[str, float]) -> None:
    last = bars.iloc[-1]
    close = float(last["close"])
    low = float(last["low"])
    atr = float(_atr(bars, p["atr_period"]).iloc[-1])
    sig = int(ST.STRATEGIES[strategy](bars, p).iloc[-1])

    pos = store.get_position(name, pair)
    pend = store.get_pending(name, pair)

    # 1) 前足で出した maker 買い指値の約定判定（安値が指値を貫けば約定）
    if pend is not None and pos is None:
        if low <= pend["limit_price"]:
            fill = pend["limit_price"] * (1 + _entry_cost(p, "maker"))
            equity = _account_equity(store, name, price_map)
            cash = store.get_cash(name)
            size, stop_dist = _size(equity, cash, atr, p, fill)
            if size > 0:
                store.set_cash(name, cash - size * fill)
                store.set_position(name, pair, size, fill, pend["limit_price"] - stop_dist)
                store.add_trade(name, pair, "buy", "maker_fill", fill, size, now_ts)
                store.bump_fillstat(name, "filled")
        else:
            store.bump_fillstat(name, "missed")  # 指値に届かず=約定せず
        store.clear_pending(name, pair)
        pos = store.get_position(name, pair)

    # 2) 保有中: 損切り or シグナル消失で成行クローズ
    if pos is not None:
        units = pos["units"]
        if low <= pos["stop_price"]:
            fill = pos["stop_price"] * (1 - _exit_cost(p))
            store.set_cash(name, store.get_cash(name) + units * fill)
            store.add_trade(name, pair, "sell", "stop", fill, units, now_ts)
            store.clear_position(name, pair)
            return
        if sig == 0:
            fill = close * (1 - _exit_cost(p))
            store.set_cash(name, store.get_cash(name) + units * fill)
            store.add_trade(name, pair, "sell", "signal", fill, units, now_ts)
            store.clear_position(name, pair)
            return
        return  # 継続保有

    # 3) 無ポジ & 指値なし & シグナル点灯 → エントリー
    if pend is None and sig == 1 and not np.isnan(atr) and atr > 0:
        if order_type == "maker":
            store.set_pending(name, pair, "buy", close, now_ts)  # 次足で約定判定
            store.bump_fillstat(name, "placed")
        else:
            fill = close * (1 + _entry_cost(p, "taker"))
            equity = _account_equity(store, name, price_map)
            cash = store.get_cash(name)
            size, stop_dist = _size(equity, cash, atr, p, fill)
            if size > 0:
                store.set_cash(name, cash - size * fill)
                store.set_position(name, pair, size, fill, close - stop_dist)
                store.add_trade(name, pair, "buy", "taker_fill", fill, size, now_ts)
