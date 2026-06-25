"""資産曲線・トレード履歴からリスク調整後リターン指標を計算する。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    final_equity: float
    total_return: float        # 倍率-1
    max_drawdown: float        # 0〜1（正の値で深さ）
    sharpe: float              # 年率近似
    profit_factor: float       # 総利益 / 総損失
    expectancy: float          # 1トレード平均損益
    win_rate: float
    n_trades: int

    def as_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(-dd.min()) if len(dd) else 0.0


def sharpe(equity: pd.Series, bars_per_year: float) -> float:
    """バー単位リターンから年率シャープを近似（無リスク金利0想定）。"""
    rets = equity.pct_change().dropna()
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * np.sqrt(bars_per_year))


def from_run(
    equity: pd.Series, trade_pnls: list[float], bars_per_year: float = 525_600.0
) -> Metrics:
    """bars_per_year デフォルトは 1分足の年間本数(365*24*60)。"""
    pnls = np.asarray(trade_pnls, dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    start = float(equity.iloc[0]) if len(equity) else 0.0
    final = float(equity.iloc[-1]) if len(equity) else start
    return Metrics(
        final_equity=final,
        total_return=(final / start - 1.0) if start else 0.0,
        max_drawdown=max_drawdown(equity),
        sharpe=sharpe(equity, bars_per_year),
        profit_factor=float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy=float(pnls.mean()) if len(pnls) else 0.0,
        win_rate=float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        n_trades=int(len(pnls)),
    )
