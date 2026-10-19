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


def _ema(close: pd.Series, span: int) -> pd.Series:
    """EMA。窓未満は NaN（ウォームアップ中に誤シグナルを出さない）。"""
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder の ADX。分母0は NaN → 0。"""
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    alpha = 1 / period
    atr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return adx.fillna(0.0)


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


def ema_cross(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り: 短期EMA > 長期EMA でロング。"""
    close = df["close"]
    fast = _ema(close, int(p.get("fast", 20)))
    slow = _ema(close, int(p.get("slow", 60)))
    return (fast > slow).astype(float).shift(1).fillna(0.0)


def donchian(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り: 直前N本の高値更新で買い、直前M本の安値割れで手仕舞い。"""
    close = df["close"]
    upper = df["high"].rolling(int(p.get("dc_entry", 20))).max().shift(1)
    lower = df["low"].rolling(int(p.get("dc_exit", 10))).min().shift(1)
    sig = _state_signal(close > upper, close < lower, df.index)
    return sig.shift(1).fillna(0.0)


def macd(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り: MACD線 > シグナル線 かつ MACD > 0 でロング。"""
    close = df["close"]
    line = (_ema(close, int(p.get("macd_fast", 12)))
            - _ema(close, int(p.get("macd_slow", 26))))
    n_sig = int(p.get("macd_signal", 9))
    sig_line = line.ewm(span=n_sig, adjust=False, min_periods=n_sig).mean()
    long = (line > sig_line) & (line > 0)
    return long.astype(float).shift(1).fillna(0.0)


def sma_adx(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り: SMAクロス + ADX>しきい値 のときだけ入る。出口はSMA反転のみ。"""
    close = df["close"]
    fast = close.rolling(int(p.get("fast", 20))).mean()
    slow = close.rolling(int(p.get("slow", 60))).mean()
    adx = _adx(df, int(p.get("adx_period", 14)))
    enter = (fast > slow) & (adx > float(p.get("adx_min", 20.0)))
    exit_ = fast < slow
    sig = _state_signal(enter, exit_, df.index)
    return sig.shift(1).fillna(0.0)


def roc(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """順張り(時系列モメンタム): N本前比の騰落率 > entry で買い、< exit で手仕舞い。"""
    close = df["close"]
    r = close / close.shift(int(p.get("roc_period", 24))) - 1
    sig = _state_signal(r > float(p.get("roc_entry", 0.0)),
                        r < float(p.get("roc_exit", -0.01)), df.index)
    return sig.shift(1).fillna(0.0)


STRATEGIES: dict[str, Callable[[pd.DataFrame, dict[str, Any]], pd.Series]] = {
    "sma_cross": sma_cross,
    "rsi": rsi_reversion,
    "bollinger": bollinger_reversion,
    "ema_cross": ema_cross,
    "donchian": donchian,
    "macd": macd,
    "sma_adx": sma_adx,
    "roc": roc,
}

# 各戦略のウォームアップに効く窓パラメータ（最小必要バー数の見積り用）
WARMUP_KEYS: dict[str, tuple[str, ...]] = {
    "sma_cross": ("fast", "slow"),
    "rsi": ("rsi_period",),
    "bollinger": ("bb_period",),
    "ema_cross": ("fast", "slow"),
    "donchian": ("dc_entry", "dc_exit"),
    "macd": ("macd_slow", "macd_signal"),
    "sma_adx": ("fast", "slow", "adx_period"),
    "roc": ("roc_period",),
}


def warmup_bars(strategy: str, p: dict[str, Any]) -> int:
    keys = WARMUP_KEYS.get(strategy, ())
    windows = [int(p.get(k, 0)) for k in keys] + [int(p.get("atr_period", 30))]
    return max(windows) if windows else 30
