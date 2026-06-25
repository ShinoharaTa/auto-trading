"""戦略（シグナル生成）。シミュレータから分離。

各戦略は df(OHLCV) と params を受け取り、「ロングでいたい状態」を表す
long_signal(0/1) の Series を返す。先読み防止のため最後に .shift(1) する
（確定足で判定し、約定は次バー）。リスク管理(損切り/サイジング)は simulator 側。
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder の RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _state_signal(enter: pd.Series, exit_: pd.Series, index: pd.Index) -> pd.Series:
    """enter で1、exit で0、その間は直前値を保持する状態シグナル。"""
    raw = pd.Series(np.nan, index=index)
    raw[enter] = 1.0
    raw[exit_] = 0.0
    return raw.ffill().fillna(0.0)


def sma_cross(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り: 短期SMA > 長期SMA でロング。"""
    close = df["close"]
    fast = close.rolling(int(p.get("fast", 20))).mean()
    slow = close.rolling(int(p.get("slow", 60))).mean()
    return (fast > slow).astype(float).shift(1).fillna(0.0)


def rsi_reversion(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """逆張り: RSIが売られすぎ(entry以下)で買い、中立(exit以上)回復で手仕舞い。"""
    close = df["close"]
    r = _rsi(close, int(p.get("rsi_period", 14)))
    entry = float(p.get("rsi_entry", 30))
    exit_l = float(p.get("rsi_exit", 55))
    sig = _state_signal(r < entry, r > exit_l, df.index)
    return sig.shift(1).fillna(0.0)


def bollinger_reversion(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """逆張り: 下バンド割れで買い、中央線回帰で手仕舞い。"""
    close = df["close"]
    period = int(p.get("bb_period", 20))
    k = float(p.get("bb_k", 2.0))
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    lower = ma - k * sd
    sig = _state_signal(close < lower, close > ma, df.index)
    return sig.shift(1).fillna(0.0)


STRATEGIES: dict[str, Callable[[pd.DataFrame, dict[str, Any]], pd.Series]] = {
    "sma_cross": sma_cross,
    "rsi": rsi_reversion,
    "bollinger": bollinger_reversion,
}

# 各戦略のウォームアップに効く窓パラメータ（最小必要バー数の見積り用）
WARMUP_KEYS: dict[str, tuple[str, ...]] = {
    "sma_cross": ("fast", "slow"),
    "rsi": ("rsi_period",),
    "bollinger": ("bb_period",),
}


def warmup_bars(strategy: str, p: dict[str, Any]) -> int:
    keys = WARMUP_KEYS.get(strategy, ())
    windows = [int(p.get(k, 0)) for k in keys] + [int(p.get("atr_period", 30))]
    return max(windows) if windows else 30
