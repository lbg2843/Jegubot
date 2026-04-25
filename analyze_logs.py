"""HTML-focused offline analysis using the shared simulation engine."""

from __future__ import annotations

import html
import io
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from analysis.loader import (
    first_price_at_or_after,
    load_all_snapshots,
    load_candidates_from_log,
    parse_log_diagnostics,
    series_from_entry,
)
from analysis.metrics import compute_metrics
from analysis.sim_engine import params_for_chain, position_size_usd_for_chain, simulate_trade


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "reflexivity.log"
REPORT_FILE = BASE_DIR / "analysis_report.html"


CSS = """
body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }
h1, h2 { margin: 0 0 16px; }
h1 { color: #93c5fd; }
h2 { color: #fcd34d; margin-top: 32px; }
.grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
.card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 16px; }
.label { color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
.value { color: #f8fafc; font-size: 26px; font-weight: 700; margin-top: 8px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 24px; background: #111827; border-radius: 12px; overflow: hidden; }
th, td { padding: 10px 12px; border-bottom: 1px solid #1f2937; text-align: left; font-size: 13px; }
th { color: #cbd5e1; background: #0b1220; }
td { color: #e2e8f0; }
.pos { color: #4ade80; }
.neg { color: #f87171; }
.muted { color: #94a3b8; }
"""


def summarize_errors(errors: list[tuple[str, str]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for level, message in errors:
        normalized = re.sub(r"0x[0-9a-fA-F]+|\$[\d.]+|byte 0x\w+", "?", message)
        counts[f"[{level}] {normalized[:120]}"] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]


def analyze_gaps(data_dir: Path, chain: str) -> dict:
    path = data_dir / f"{chain}_snapshots.jsonl"
    timestamps: list[datetime] = []
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                timestamps.append(datetime.fromisoformat(__import__("json").loads(line)["timestamp"]))
            except Exception:
                continue
    if len(timestamps) < 2:
        return {}
    timestamps.sort()
    gaps = [
        (timestamps[index + 1] - timestamps[index]).total_seconds() / 60.0
        for index in range(len(timestamps) - 1)
    ]
    abnormal = [gap for gap in gaps if gap >= 15]
    return {
        "records": len(timestamps),
        "span_h": round((timestamps[-1] - timestamps[0]).total_seconds() / 3600.0, 1),
        "first": timestamps[0].strftime("%m/%d %H:%M"),
        "last": timestamps[-1].strftime("%m/%d %H:%M"),
        "normal_pct": round(sum(1 for gap in gaps if gap < 15) / len(gaps) * 100.0, 1),
        "abnormal_cnt": len(abnormal),
        "max_gap_min": round(max(abnormal), 1) if abnormal else 0.0,
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    cls = "pos" if value > 0 else "neg" if value < 0 else "muted"
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}{value:.2f}%</span>'


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    cls = "pos" if value > 0 else "neg" if value < 0 else "muted"
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}${value:.2f}</span>'


def _candidate_rows(
    candidates: list[dict],
    snapshots: dict[str, dict[str, list[dict]]],
    *,
    profit_lock_enabled: bool,
) -> list[dict]:
    rows: list[dict] = []
    for candidate in candidates:
        series = snapshots.get(candidate["chain"], {}).get(candidate["symbol"], [])
        entry_snapshot = next((row for row in series if row["_ts"] >= candidate["ts"]), None)
        if not entry_snapshot:
            continue
        entry_snapshot = dict(entry_snapshot)
        entry_snapshot["_position_size_usd"] = position_size_usd_for_chain(candidate["chain"])
        params = params_for_chain(candidate["chain"])
        params.profit_lock_enabled = profit_lock_enabled
        trade = simulate_trade(entry_snapshot, series_from_entry(series, candidate["ts"]), params)
        rows.append(
            {
                "candidate": candidate,
                "trade": trade,
                "r1h": None if not series else first_price_at_or_after(series, candidate["ts"] + timedelta(hours=1)),
                "r4h": None if not series else first_price_at_or_after(series, candidate["ts"] + timedelta(hours=4)),
                "r24h": None if not series else first_price_at_or_after(series, candidate["ts"] + timedelta(hours=24)),
            }
        )
    return rows


def build_html_report(candidate_rows: list[dict], metrics, gap_stats: dict, cycles: list[datetime], errors: list[tuple[str, str]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    uptime_pct = 0.0
    expected_cycles = 0
    if cycles:
        span_min = (cycles[-1] - cycles[0]).total_seconds() / 60.0
        expected_cycles = int(span_min / 10.0) if span_min > 0 else len(cycles)
        uptime_pct = len(cycles) / max(1, expected_cycles) * 100.0

    cards = [
        ("Candidates", str(metrics.total_trades)),
        ("Closed Trades", str(metrics.closed_trades)),
        ("Win Rate", f"{metrics.win_rate_pct:.1f}%"),
        ("Total PnL", f"${metrics.total_pnl_usd:+.2f}"),
    ]

    parts = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        f"<title>Jegubot Analysis Report - {html.escape(now)}</title>",
        f"<style>{CSS}</style></head><body>",
        f"<h1>Jegubot Analysis Report</h1><p class='muted'>Generated {html.escape(now)}</p>",
        "<div class='grid'>",
    ]
    for label, value in cards:
        parts.append(f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div></div>")
    parts.append("</div>")

    parts.append("<h2>Runtime Diagnostics</h2><table><tr><th>Metric</th><th>Value</th></tr>")
    parts.append(f"<tr><td>Observed cycles</td><td>{len(cycles)}</td></tr>")
    parts.append(f"<tr><td>Expected cycles</td><td>{expected_cycles}</td></tr>")
    parts.append(f"<tr><td>Estimated uptime</td><td>{uptime_pct:.1f}%</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Snapshot Coverage</h2><table><tr><th>Chain</th><th>Records</th><th>Span (h)</th><th>Normal %</th><th>Abnormal gaps</th><th>Max gap (min)</th></tr>")
    for chain in ("bsc", "base", "solana"):
        stats = gap_stats.get(chain) or {}
        parts.append(
            f"<tr><td>{chain.upper()}</td><td>{stats.get('records', 0):,}</td><td>{stats.get('span_h', 0)}</td>"
            f"<td>{stats.get('normal_pct', 0)}%</td><td>{stats.get('abnormal_cnt', 0)}</td><td>{stats.get('max_gap_min', 0)}</td></tr>"
        )
    parts.append("</table>")

    parts.append("<h2>Top Errors</h2><table><tr><th>Count</th><th>Message</th></tr>")
    for message, count in summarize_errors(errors):
        parts.append(f"<tr><td>{count}</td><td>{html.escape(message)}</td></tr>")
    if not errors:
        parts.append("<tr><td colspan='2' class='muted'>No warnings or errors found.</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Trade Outcomes</h2><table><tr><th>Chain</th><th>Symbol</th><th>Entry</th><th>Exit Reason</th><th>PnL %</th><th>PnL USD</th><th>Peak</th><th>Max DD</th><th>Hold h</th></tr>")
    for row in candidate_rows:
        trade = row["trade"]
        parts.append(
            f"<tr><td>{trade.chain.upper()}</td><td>{html.escape(trade.symbol)}</td><td>${trade.entry_price:.6g}</td>"
            f"<td>{html.escape(trade.exit_reason)}</td><td>{_fmt_pct(trade.pnl_pct)}</td><td>{_fmt_usd(trade.pnl_usd)}</td>"
            f"<td>${trade.peak_price:.6g}</td><td>{_fmt_pct(trade.max_drawdown_pct)}</td>"
            f"<td>{'-' if trade.hold_hours is None else f'{trade.hold_hours:.1f}'}</td></tr>"
        )
    parts.append("</table></body></html>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-profit-lock", action="store_true", help="Use original immediate take-profit behavior.")
    args = parser.parse_args()

    print("=== Jegubot unified analysis ===")

    snapshots = load_all_snapshots(DATA_DIR)
    candidates = load_candidates_from_log(LOG_FILE)
    cycles, errors = parse_log_diagnostics(LOG_FILE)

    candidate_rows = _candidate_rows(
        candidates,
        snapshots,
        profit_lock_enabled=not args.no_profit_lock,
    )
    trades = [row["trade"] for row in candidate_rows]
    metrics = compute_metrics(trades)

    gap_stats = {chain: analyze_gaps(DATA_DIR, chain) for chain in ("bsc", "base", "solana")}
    REPORT_FILE.write_text(build_html_report(candidate_rows, metrics, gap_stats, cycles, errors), encoding="utf-8")

    print(f"Candidates: {metrics.total_trades}")
    print(f"Closed trades: {metrics.closed_trades}")
    print(f"Wins: {metrics.wins}")
    print(f"Total PnL: ${metrics.total_pnl_usd:+.2f}")
    print(f"Average PnL per closed trade: ${metrics.avg_pnl_usd:+.2f}")
    print(f"Profit lock enabled: {not args.no_profit_lock}")
    print(f"Exit reasons: {metrics.by_exit_reason}")
    print(f"HTML report: {REPORT_FILE}")


if __name__ == "__main__":
    main()
