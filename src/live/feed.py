"""リアルタイム足フィード。bitbank公開APIで当日分を増分取得し、
DBから直近をロードして指定時間足にリサンプルして返す。実発注はしない。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.backtest.simulator import load_candles
from src.data import fetch as FETCH
from src.data import resample as R

# 時間足ごとのウォームアップに必要なおおよその日数（指標窓を賄う）
_LOOKBACK_DAYS = {"1m": 3, "5m": 3, "15m": 5, "30m": 8, "1h": 14, "4h": 40}


def get_recent_bars(db_path: str, pair: str, timeframe: str,
                    refresh: bool = True) -> pd.DataFrame:
    """直近の確定足を返す（最後の行は最新の確定足）。"""
    if refresh:
        today = datetime.now(timezone.utc).date()
        try:
            FETCH.run(pair, db_path, today, today, pause=0.0)
        except Exception as e:  # 取得失敗時は既存DBで継続
            print(f"[feed] {pair}: refresh WARN {e}", flush=True)

    lookback = _LOOKBACK_DAYS.get(timeframe, 14)
    start_ms = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback)).timestamp() * 1000)
    data = load_candles(db_path, pair, start_ms=start_ms)
    if data.empty:
        return data
    rdf = R.resample_ohlcv(data, timeframe)
    # 形成中の最新足を落として「確定足」のみにする
    return rdf.iloc[:-1] if len(rdf) > 1 else rdf
