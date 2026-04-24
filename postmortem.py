"""Postmortem report built on the unified simulation engine."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from analysis.loader import (
    load_all_snapshots,
    load_candidates_from_log,
    series_from_entry,
)
from analysis.metrics import compute_metrics
from analysis.report import build_markdown_report
from analysis.sim_engine import params_for_chain, position_size_usd_for_chain, simulate_trade


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "reflexivity.log"
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def build_results() -> list:
    snapshots = load_all_snapshots(DATA_DIR)
    candidates = load_candidates_from_log(LOG_FILE)
    trades = []

    for candidate in candidates:
        series = snapshots.get(candidate["chain"], {}).get(candidate["symbol"], [])
        entry_snapshot = next((row for row in series if row["_ts"] >= candidate["ts"]), None)
        if not entry_snapshot:
            continue
        entry_snapshot = dict(entry_snapshot)
        entry_snapshot["_position_size_usd"] = position_size_usd_for_chain(candidate["chain"])
        trade = simulate_trade(
            entry_snapshot=entry_snapshot,
            future_snapshots=series_from_entry(series, candidate["ts"]),
            params=params_for_chain(candidate["chain"]),
        )
        trades.append(trade)

    return trades


def main() -> None:
    print("=== Jegubot postmortem ===")

    trades = build_results()
    metrics = compute_metrics(trades)

    for trade in trades:
        pnl_pct = "-" if trade.pnl_pct is None else f"{trade.pnl_pct:+.2f}%"
        pnl_usd = "-" if trade.pnl_usd is None else f"${trade.pnl_usd:+.2f}"
        hold = "-" if trade.hold_hours is None else f"{trade.hold_hours:.1f}h"
        print(
            f"[{trade.chain.upper():6}] {trade.symbol:16} "
            f"{trade.exit_reason:14} {pnl_pct:>10} {pnl_usd:>12} hold={hold}"
        )

    print("")
    print(f"Trades: {metrics.total_trades}")
    print(f"Closed trades: {metrics.closed_trades}")
    print(f"Wins/Losses: {metrics.wins}/{metrics.losses}")
    print(f"Win rate: {metrics.win_rate_pct:.1f}%")
    print(f"Total PnL: ${metrics.total_pnl_usd:+.2f}")
    print(f"Average PnL per closed trade: ${metrics.avg_pnl_usd:+.2f}")
    print(f"Profit factor: {metrics.profit_factor:.2f}")

    report_path = REPORT_DIR / "analysis_summary.md"
    report_path.write_text(build_markdown_report(trades, metrics), encoding="utf-8")
    print(f"Markdown report: {report_path}")


if __name__ == "__main__":
    main()
