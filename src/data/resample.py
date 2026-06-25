"""1分足を上位時間足に集約する。過剰取引（コスト負け）対策の最重要レバー。"""
from __future__ import annotations

import pandas as pd

# timeframe -> (pandas resample rule, 1本あたりの分数)
TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1m": ("1min", 1),
    "5m": ("5min", 5),
    "15m": ("15min", 15),
    "30m": ("30min", 30),
    "1h": ("60min", 60),
    "4h": ("240min", 240),
}

# 期間プリセット（分）
PERIOD_MIN: dict[str, int] = {
    "1d": 24 * 60,
    "3d": 3 * 24 * 60,
    "1w": 7 * 24 * 60,
    "2w": 14 * 24 * 60,
    "1m": 30 * 24 * 60,
}

_YEAR_MIN = 365 * 24 * 60


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """OHLCV を上位時間足へ集約。timeframe='1m' は素通し。"""
    if timeframe == "1m":
        return df
    rule, _ = TIMEFRAMES[timeframe]
    out = pd.DataFrame({
        "open": df["open"].resample(rule).first(),
        "high": df["high"].resample(rule).max(),
        "low": df["low"].resample(rule).min(),
        "close": df["close"].resample(rule).last(),
        "volume": df["volume"].resample(rule).sum(),
    }).dropna()
    return out


def bars_per_year(timeframe: str) -> float:
    return _YEAR_MIN / TIMEFRAMES[timeframe][1]


def bars_for(timeframe: str, period: str) -> int:
    """指定期間が、その時間足で何本になるか。"""
    return PERIOD_MIN[period] // TIMEFRAMES[timeframe][1]
