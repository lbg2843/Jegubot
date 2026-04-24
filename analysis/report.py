"""Markdown reporting helpers for unified analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .metrics import PortfolioMetrics
from .sim_engine import TradeResult


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}${value:.2f}"


def build_markdown_report(trades: list[TradeResult], metrics: PortfolioMetrics) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    by_chain: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        by_chain[trade.chain].append(trade)

    lines: list[str] = [
        "# Jegubot Analysis Postmortem",
        "",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total trades | {metrics.total_trades} |",
        f"| Closed trades | {metrics.closed_trades} |",
        f"| Wins / Losses | {metrics.wins} / {metrics.losses} |",
        f"| Win rate | {metrics.win_rate_pct:.1f}% |",
        f"| Total PnL | {_fmt_usd(metrics.total_pnl_usd)} |",
        f"| Avg win | {_fmt_usd(metrics.avg_win_usd)} |",
        f"| Avg loss | {_fmt_usd(metrics.avg_loss_usd)} |",
        f"| Avg PnL / trade | {_fmt_usd(metrics.avg_pnl_usd)} |",
        f"| Profit factor | {metrics.profit_factor:.2f} |",
        f"| Worst drawdown | {_fmt_pct(metrics.max_drawdown_pct)} |",
        "",
        "## By Chain",
        "",
        "| Chain | Trades | Closed | Wins | Total PnL | Avg PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for chain in ("bsc", "solana", "base"):
        chain_trades = by_chain.get(chain, [])
        chain_closed = [trade for trade in chain_trades if trade.exit_reason != "open" and trade.pnl_usd is not None]
        chain_wins = [trade for trade in chain_closed if (trade.pnl_usd or 0.0) > 0]
        chain_total = sum(trade.pnl_usd or 0.0 for trade in chain_closed)
        chain_avg = chain_total / len(chain_closed) if chain_closed else 0.0
        lines.append(
            f"| {chain.upper()} | {len(chain_trades)} | {len(chain_closed)} | {len(chain_wins)} "
            f"| {_fmt_usd(chain_total)} | {_fmt_usd(chain_avg)} |"
        )

    lines.extend(
        [
            "",
            "## Exit Reasons",
            "",
            "| Exit reason | Count |",
            "|---|---:|",
        ]
    )
    for reason, count in sorted(metrics.by_exit_reason.items()):
        lines.append(f"| {reason} | {count} |")

    lines.extend(
        [
            "",
            "## Trade Detail",
            "",
            "| Chain | Symbol | Entry | Exit reason | Exit | PnL % | PnL USD | Peak | Max DD | Hold h |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for trade in trades:
        lines.append(
            f"| {trade.chain.upper()} | {trade.symbol} | ${trade.entry_price:.6g} | {trade.exit_reason} "
            f"| {'-' if trade.exit_price is None else f'${trade.exit_price:.6g}'} "
            f"| {_fmt_pct(trade.pnl_pct)} | {_fmt_usd(trade.pnl_usd)} "
            f"| ${trade.peak_price:.6g} | {_fmt_pct(trade.max_drawdown_pct)} "
            f"| {'-' if trade.hold_hours is None else f'{trade.hold_hours:.1f}'} |"
        )

    return "\n".join(lines)
