"""Portfolio-level metrics for shared offline analysis."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict

from .sim_engine import TradeResult


@dataclass
class PortfolioMetrics:
    total_trades: int
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl_usd: float
    avg_win_usd: float
    avg_loss_usd: float
    profit_factor: float
    max_drawdown_pct: float
    max_win_usd: float
    max_loss_usd: float
    avg_pnl_usd: float
    by_exit_reason: Dict[str, int]


def compute_metrics(trades: list[TradeResult]) -> PortfolioMetrics:
    closed = [trade for trade in trades if trade.pnl_usd is not None and trade.exit_reason != "open"]
    wins = [trade for trade in closed if (trade.pnl_usd or 0.0) > 0]
    losses = [trade for trade in closed if (trade.pnl_usd or 0.0) <= 0]
    gross_win = sum(trade.pnl_usd or 0.0 for trade in wins)
    gross_loss = abs(sum(trade.pnl_usd or 0.0 for trade in losses))
    total_pnl = sum(trade.pnl_usd or 0.0 for trade in closed)

    return PortfolioMetrics(
        total_trades=len(trades),
        closed_trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=(len(wins) / len(closed) * 100.0) if closed else 0.0,
        total_pnl_usd=total_pnl,
        avg_win_usd=(gross_win / len(wins)) if wins else 0.0,
        avg_loss_usd=(sum(trade.pnl_usd or 0.0 for trade in losses) / len(losses)) if losses else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        max_drawdown_pct=min((trade.max_drawdown_pct for trade in trades), default=0.0),
        max_win_usd=max((trade.pnl_usd or 0.0 for trade in wins), default=0.0),
        max_loss_usd=min((trade.pnl_usd or 0.0 for trade in losses), default=0.0),
        avg_pnl_usd=(total_pnl / len(closed)) if closed else 0.0,
        by_exit_reason=dict(Counter(trade.exit_reason for trade in trades)),
    )
