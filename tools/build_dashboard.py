"""
Build a local HTML dashboard for wallet, portfolio, and analysis visibility.

Usage:
    python tools/build_dashboard.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
RECENT_DAYS = 7

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyze_logs import run_scenario  # noqa: E402
from trading.safety import SafetyCircuitBreaker  # noqa: E402


BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
OUTPUT_FILE = REPORTS_DIR / "dashboard.html"


CSS = """
:root {
  --bg: #f5efe1;
  --paper: #fffaf0;
  --ink: #1e1a16;
  --muted: #6f665d;
  --line: #d7c7b4;
  --accent: #005f73;
  --accent-2: #ca6702;
  --good: #2a9d8f;
  --bad: #ae2012;
  --card: #fffdf8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 28px;
  background:
    radial-gradient(circle at top right, rgba(202,103,2,0.10), transparent 30%),
    linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
  color: var(--ink);
  font-family: Georgia, "Times New Roman", serif;
}
.wrap {
  max-width: 1400px;
  margin: 0 auto;
}
.hero {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 18px;
  margin-bottom: 20px;
}
.panel {
  background: rgba(255, 253, 248, 0.95);
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 18px 20px;
  box-shadow: 0 18px 40px rgba(52, 35, 15, 0.08);
}
.eyebrow {
  color: var(--accent);
  letter-spacing: 0.18em;
  font-size: 12px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
h1, h2, h3 { margin: 0; }
h1 { font-size: 38px; line-height: 1.05; }
.subtitle {
  color: var(--muted);
  margin-top: 10px;
  font-size: 15px;
}
.stamp {
  display: inline-block;
  margin-top: 14px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 13px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.metric {
  min-height: 132px;
}
.metric .label {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
}
.metric .value {
  margin-top: 10px;
  font-size: 30px;
  font-weight: 700;
}
.metric .hint {
  margin-top: 10px;
  color: var(--muted);
  font-size: 13px;
}
.section {
  margin-top: 18px;
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: end;
  margin-bottom: 12px;
}
.section-head p {
  margin: 0;
  color: var(--muted);
  font-size: 14px;
}
.two-col {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 18px;
}
table {
  width: 100%;
  border-collapse: collapse;
  overflow: hidden;
  border-radius: 14px;
  background: var(--card);
  border: 1px solid var(--line);
}
th, td {
  padding: 11px 12px;
  border-bottom: 1px solid rgba(215,199,180,0.7);
  text-align: left;
  font-size: 14px;
  vertical-align: top;
}
th {
  background: #f1e7d8;
  color: #4c433c;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.10em;
}
tr:last-child td { border-bottom: none; }
.pos { color: var(--good); font-weight: 700; }
.neg { color: var(--bad); font-weight: 700; }
.muted { color: var(--muted); }
.wallet-card {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.pill {
  display: inline-block;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--line);
}
.ok { color: var(--good); border-color: rgba(42,157,143,0.35); background: rgba(42,157,143,0.08); }
.err { color: var(--bad); border-color: rgba(174,32,18,0.35); background: rgba(174,32,18,0.08); }
.bar {
  display: block;
  height: 10px;
  background: rgba(0,95,115,0.12);
  border-radius: 999px;
  overflow: hidden;
  margin-top: 8px;
}
.bar > span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
}
code {
  font-family: "Consolas", "Courier New", monospace;
  font-size: 12px;
  background: #f1e7d8;
  padding: 2px 6px;
  border-radius: 6px;
}
@media (max-width: 1080px) {
  .hero, .two-col, .grid, .wallet-card { grid-template-columns: 1fr; }
}
"""


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")
        return

    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    cls = "pos" if value > 0 else "neg" if value < 0 else "muted"
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}${value:,.2f}</span>'


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    cls = "pos" if value > 0 else "neg" if value < 0 else "muted"
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}{value:.2f}%</span>'


def _short_addr(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 14:
        return value
    return f"{value[:6]}...{value[-4:]}"


def load_positions() -> tuple[list[dict], list[dict]]:
    path = DATA_DIR / "positions.jsonl"
    if not path.exists():
        return [], []

    open_positions: dict[tuple[str, str], dict] = {}
    closed_positions: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            key = (record.get("chain"), record.get("contract_address"))
            if record.get("is_closed"):
                open_positions.pop(key, None)
                closed_positions.append(record)
            else:
                open_positions[key] = record
    closed_positions.sort(key=lambda item: item.get("exit_timestamp") or "", reverse=True)
    return list(open_positions.values()), closed_positions


def build_portfolio_summary(open_positions: list[dict], closed_positions: list[dict]) -> dict:
    unrealized_pnl_usd = 0.0
    committed_usd = 0.0
    for pos in open_positions:
        size_usd = float(pos.get("size_usd") or 0.0)
        entry_price = float(pos.get("entry_price") or 0.0)
        current_price = float(pos.get("current_price") or 0.0)
        committed_usd += size_usd
        if entry_price > 0:
            unrealized_pnl_usd += size_usd * ((current_price / entry_price) - 1.0)

    realized_pnl_usd = sum(float(pos.get("realized_pnl_usd") or 0.0) for pos in closed_positions)
    # 타임존 버그 수정: exit_timestamp 는 UTC, today 는 KST 였음(9h 어긋남).
    # exit 를 KST 로 변환 후 KST '오늘'과 비교. 최근 N일 실적도 같이 계산
    # (헤드라인 realized 는 lifetime 이라 동결버그 시대에 묻혀 현재 상태가 안 보였음).
    now_kst = datetime.now(KST)
    today = now_kst.date()
    recent_cutoff = now_kst - timedelta(days=RECENT_DAYS)
    realized_today_usd = 0.0
    realized_recent_usd = 0.0
    recent_count = 0
    for pos in closed_positions:
        exit_timestamp = pos.get("exit_timestamp")
        if not exit_timestamp:
            continue
        try:
            exit_dt = datetime.fromisoformat(str(exit_timestamp).replace("Z", "+00:00"))
        except Exception:
            continue
        if exit_dt.tzinfo is None:
            exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        exit_kst = exit_dt.astimezone(KST)
        amount = float(pos.get("realized_pnl_usd") or 0.0)
        if exit_kst.date() == today:
            realized_today_usd += amount
        if exit_kst >= recent_cutoff:
            realized_recent_usd += amount
            recent_count += 1

    return {
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "committed_usd": committed_usd,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "realized_pnl_usd": realized_pnl_usd,
        "realized_today_usd": realized_today_usd,
        "realized_recent_usd": realized_recent_usd,
        "recent_count": recent_count,
    }


def build_chain_breakdown(open_positions: list[dict], closed_positions: list[dict]) -> list[dict]:
    bucket: dict[str, dict] = defaultdict(lambda: {
        "open_positions": 0,
        "closed_positions": 0,
        "committed_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
    })

    for pos in open_positions:
        chain = (pos.get("chain") or "?").lower()
        entry_price = float(pos.get("entry_price") or 0.0)
        current_price = float(pos.get("current_price") or 0.0)
        size_usd = float(pos.get("size_usd") or 0.0)
        bucket[chain]["open_positions"] += 1
        bucket[chain]["committed_usd"] += size_usd
        if entry_price > 0:
            bucket[chain]["unrealized_pnl_usd"] += size_usd * ((current_price / entry_price) - 1.0)

    for pos in closed_positions:
        chain = (pos.get("chain") or "?").lower()
        bucket[chain]["closed_positions"] += 1
        bucket[chain]["realized_pnl_usd"] += float(pos.get("realized_pnl_usd") or 0.0)

    rows = []
    for chain, stats in bucket.items():
        rows.append({"chain": chain, **stats})
    rows.sort(key=lambda row: row["chain"])
    return rows


def load_safety_state() -> dict:
    path = DATA_DIR / "trading_safety_state.json"
    if not path.exists():
        circuit = SafetyCircuitBreaker(path)
        return circuit.get_status()
    return json.loads(path.read_text(encoding="utf-8"))


def load_wallet_statuses() -> dict:
    statuses = {}
    try:
        from trading.wallet_bsc import BSCWallet

        statuses["bsc"] = {"ok": True, "data": BSCWallet().get_status()}
    except Exception as exc:
        statuses["bsc"] = {"ok": False, "error": str(exc)}

    try:
        from trading.wallet_solana import SolanaWallet

        statuses["solana"] = {"ok": True, "data": SolanaWallet().get_status()}
    except Exception as exc:
        statuses["solana"] = {"ok": False, "error": str(exc)}
    return statuses


def load_analysis_results() -> dict:
    results = {}
    for scenario in ("ideal", "realistic", "conservative", "pessimistic"):
        rows, metrics = run_scenario(scenario, profit_lock_enabled=True)
        results[scenario] = {
            "metrics": asdict(metrics),
            "rows": rows,
        }
    return results


def render_wallet_panel(wallet_statuses: dict) -> str:
    blocks = []
    for chain, title in (("bsc", "BSC / Rabby"), ("solana", "Solana / Phantom")):
        item = wallet_statuses[chain]
        if item["ok"]:
            data = item["data"]
            status_badge = '<span class="pill ok">Connected</span>'
            if chain == "bsc":
                body = (
                    f"<div class='hint'>Address <code>{_short_addr(data['address'])}</code></div>"
                    f"<div class='value'>{data['bnb_balance']:.5f} BNB</div>"
                    f"<div class='hint'>USDT {data['usdt_balance']:.4f} | Gas {data['gas_price_gwei']:.2f} Gwei</div>"
                    f"<div class='hint'>Block {data['block_number']:,}</div>"
                )
            else:
                body = (
                    f"<div class='hint'>Address <code>{_short_addr(data['address'])}</code></div>"
                    f"<div class='value'>{data['sol_balance']:.5f} SOL</div>"
                    f"<div class='hint'>USDC {data['usdc_balance']:.4f}</div>"
                    f"<div class='hint'>{data['rpc']}</div>"
                )
        else:
            status_badge = '<span class="pill err">Unavailable</span>'
            body = f"<div class='hint'>{item['error']}</div>"
        blocks.append(
            f"<div class='panel metric'><div class='label'>{title}</div>{status_badge}{body}</div>"
        )
    return "<div class='wallet-card'>" + "".join(blocks) + "</div>"


def render_dashboard() -> str:
    _load_env()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    wallet_statuses = load_wallet_statuses()
    open_positions, closed_positions = load_positions()
    portfolio = build_portfolio_summary(open_positions, closed_positions)
    chain_rows = build_chain_breakdown(open_positions, closed_positions)
    safety = load_safety_state()
    analysis = load_analysis_results()

    metrics = analysis["ideal"]["metrics"]
    closed_recent = closed_positions[:10]
    open_rows = sorted(
        open_positions,
        key=lambda row: float(row.get("size_usd") or 0.0),
        reverse=True,
    )

    max_realized_chain = max([abs(row["realized_pnl_usd"]) for row in chain_rows] + [1.0])

    scenario_rows = []
    for name in ("ideal", "realistic", "conservative", "pessimistic"):
        row = analysis[name]["metrics"]
        scenario_rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{row['total_trades']}</td>"
            f"<td>{row['closed_trades']}</td>"
            f"<td>{row['win_rate_pct']:.1f}%</td>"
            f"<td>{_fmt_usd(row['total_pnl_usd'])}</td>"
            f"<td>{_fmt_usd(row['total_pnl_usd'] / row['closed_trades'] if row['closed_trades'] else 0.0)}</td>"
            f"<td>{row['profit_factor']:.2f}</td>"
            "</tr>"
        )

    chain_table_rows = []
    for row in chain_rows:
        bar_pct = min(100.0, abs(row["realized_pnl_usd"]) / max_realized_chain * 100.0)
        chain_table_rows.append(
            "<tr>"
            f"<td>{row['chain'].upper()}</td>"
            f"<td>{row['open_positions']}</td>"
            f"<td>{row['closed_positions']}</td>"
            f"<td>{_fmt_usd(row['committed_usd'])}</td>"
            f"<td>{_fmt_usd(row['unrealized_pnl_usd'])}</td>"
            f"<td>{_fmt_usd(row['realized_pnl_usd'])}<span class='bar'><span style='width:{bar_pct:.1f}%'></span></span></td>"
            "</tr>"
        )

    open_table_rows = []
    for pos in open_rows:
        entry_price = float(pos.get("entry_price") or 0.0)
        current_price = float(pos.get("current_price") or 0.0)
        size_usd = float(pos.get("size_usd") or 0.0)
        pnl_pct = ((current_price / entry_price) - 1.0) * 100.0 if entry_price else 0.0
        pnl_usd = size_usd * (pnl_pct / 100.0)
        open_table_rows.append(
            "<tr>"
            f"<td>{(pos.get('chain') or '').upper()}</td>"
            f"<td>{pos.get('symbol') or '-'}</td>"
            f"<td>${entry_price:.6g}</td>"
            f"<td>${current_price:.6g}</td>"
            f"<td>{_fmt_pct(pnl_pct)}</td>"
            f"<td>{_fmt_usd(pnl_usd)}</td>"
            f"<td>{_fmt_usd(size_usd)}</td>"
            f"<td>{pos.get('last_update') or '-'}</td>"
            "</tr>"
        )
    if not open_table_rows:
        open_table_rows.append("<tr><td colspan='8' class='muted'>No open positions.</td></tr>")

    closed_table_rows = []
    for pos in closed_recent:
        closed_table_rows.append(
            "<tr>"
            f"<td>{(pos.get('chain') or '').upper()}</td>"
            f"<td>{pos.get('symbol') or '-'}</td>"
            f"<td>{pos.get('exit_reason') or '-'}</td>"
            f"<td>{_fmt_pct(float(pos.get('realized_pnl_pct')) if pos.get('realized_pnl_pct') is not None else None)}</td>"
            f"<td>{_fmt_usd(float(pos.get('realized_pnl_usd')) if pos.get('realized_pnl_usd') is not None else None)}</td>"
            f"<td>{pos.get('exit_timestamp') or '-'}</td>"
            "</tr>"
        )
    if not closed_table_rows:
        closed_table_rows.append("<tr><td colspan='6' class='muted'>No closed positions yet.</td></tr>")

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jegubot Performance Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="panel">
        <div class="eyebrow">Jegubot Dashboard</div>
        <h1>Wallet, PnL, Risk 를 한 번에 보는 운용 보드</h1>
        <div class="subtitle">
          현재 지갑 상태, 실제 포지션 장부, 안전장치, 그리고 오프라인 분석 시나리오를
          한 화면에 모았습니다. Phase 2 진입 전에 운영 체력과 자금 상태를 빠르게 점검할 수 있습니다.
        </div>
        <div class="stamp">Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
      </div>
      <div class="panel">
        <div class="eyebrow">Quick Status</div>
        <div class="hint">Open positions {portfolio['open_count']} / Closed {portfolio['closed_count']}</div>
        <div class="value">{_fmt_usd(portfolio['realized_recent_usd'])}</div>
        <div class="hint">Realized last {RECENT_DAYS}d ({portfolio['recent_count']} trades) &mdash; 현재 상태</div>
        <div class="hint" style="margin-top:12px;">Lifetime {_fmt_usd(portfolio['realized_pnl_usd'])} | Today(KST) {_fmt_usd(portfolio['realized_today_usd'])} | Unrealized {_fmt_usd(portfolio['unrealized_pnl_usd'])}</div>
        <div class="hint">Safety halt: {safety.get('halt_reason') or 'off'}</div>
      </div>
    </div>

    <div class="section">
      <div class="section-head">
        <div>
          <div class="eyebrow">Wallets</div>
          <h2>Connected Wallet Snapshot</h2>
        </div>
        <p>잔액 조회 전용. 서명/스왑 없음.</p>
      </div>
      {render_wallet_panel(wallet_statuses)}
    </div>

    <div class="grid section">
      <div class="panel metric">
        <div class="label">Committed Capital</div>
        <div class="value">{portfolio['committed_usd']:.0f} USD</div>
        <div class="hint">현재 오픈 포지션 기준 투입 자본</div>
      </div>
      <div class="panel metric">
        <div class="label">Unrealized PnL</div>
        <div class="value">{_fmt_usd(portfolio['unrealized_pnl_usd'])}</div>
        <div class="hint">오픈 포지션의 현재 평가손익</div>
      </div>
      <div class="panel metric">
        <div class="label">Daily Trades</div>
        <div class="value">{int(safety.get('daily_trades', 0))}</div>
        <div class="hint">오늘 집행된 거래 수</div>
      </div>
      <div class="panel metric">
        <div class="label">Consecutive Losses</div>
        <div class="value">{int(safety.get('consecutive_losses', 0))}</div>
        <div class="hint">Circuit breaker 감시 지표</div>
      </div>
    </div>

    <div class="two-col section">
      <div class="panel">
        <div class="section-head">
          <div>
            <div class="eyebrow">Ledger</div>
            <h2>Open Positions</h2>
          </div>
          <p>현재 장부에서 살아 있는 포지션</p>
        </div>
        <table>
          <tr><th>Chain</th><th>Symbol</th><th>Entry</th><th>Current</th><th>PnL %</th><th>PnL USD</th><th>Size</th><th>Updated</th></tr>
          {''.join(open_table_rows)}
        </table>
      </div>

      <div class="panel">
        <div class="section-head">
          <div>
            <div class="eyebrow">Safety</div>
            <h2>Circuit Breaker State</h2>
          </div>
          <p>자동매매 중단 조건 요약</p>
        </div>
        <table>
          <tr><th>Field</th><th>Value</th></tr>
          <tr><td>Daily PnL</td><td>{_fmt_usd(float(safety.get('daily_pnl_usd', 0.0)))}</td></tr>
          <tr><td>Daily trades</td><td>{int(safety.get('daily_trades', 0))}</td></tr>
          <tr><td>Consecutive losses</td><td>{int(safety.get('consecutive_losses', 0))}</td></tr>
          <tr><td>Halt until</td><td>{safety.get('halt_until') or '<span class="muted">-</span>'}</td></tr>
          <tr><td>Halt reason</td><td>{safety.get('halt_reason') or '<span class="muted">off</span>'}</td></tr>
          <tr><td>Last reset</td><td>{safety.get('last_reset_date') or '-'}</td></tr>
        </table>
      </div>
    </div>

    <div class="section">
      <div class="section-head">
        <div>
          <div class="eyebrow">Chain View</div>
          <h2>Chain Breakdown</h2>
        </div>
        <p>실제 장부 기준 체인별 노출과 손익</p>
      </div>
      <table>
        <tr><th>Chain</th><th>Open</th><th>Closed</th><th>Committed</th><th>Unrealized</th><th>Realized</th></tr>
        {''.join(chain_table_rows) if chain_table_rows else "<tr><td colspan='6' class='muted'>No chain stats yet.</td></tr>"}
      </table>
    </div>

    <div class="two-col section">
      <div class="panel">
        <div class="section-head">
          <div>
            <div class="eyebrow">Postmortem</div>
            <h2>Scenario Comparison</h2>
          </div>
          <p>⚠ 시뮬레이션(예측) &mdash; 실제 실적 아님. 위 Quick Status(realized)가 실제 장부.</p>
        </div>
        <table>
          <tr><th>Scenario</th><th>Trades</th><th>Closed</th><th>Win %</th><th>Total PnL</th><th>Avg / Trade</th><th>PF</th></tr>
          {''.join(scenario_rows)}
        </table>
      </div>

      <div class="panel">
        <div class="section-head">
          <div>
            <div class="eyebrow">Reference</div>
            <h2>Ideal Scenario Snapshot</h2>
          </div>
          <p>현재 기준선</p>
        </div>
        <table>
          <tr><th>Metric</th><th>Value</th></tr>
          <tr><td>Total trades</td><td>{metrics['total_trades']}</td></tr>
          <tr><td>Closed trades</td><td>{metrics['closed_trades']}</td></tr>
          <tr><td>Wins / Losses</td><td>{metrics['wins']} / {metrics['losses']}</td></tr>
          <tr><td>Win rate</td><td>{metrics['win_rate_pct']:.1f}%</td></tr>
          <tr><td>Total PnL</td><td>{_fmt_usd(metrics['total_pnl_usd'])}</td></tr>
          <tr><td>Profit factor</td><td>{metrics['profit_factor']:.2f}</td></tr>
          <tr><td>Max drawdown</td><td>{_fmt_pct(metrics['max_drawdown_pct'])}</td></tr>
          <tr><td>Best trade</td><td>{_fmt_usd(metrics['max_win_usd'])}</td></tr>
          <tr><td>Worst trade</td><td>{_fmt_usd(metrics['max_loss_usd'])}</td></tr>
        </table>
      </div>
    </div>

    <div class="section">
      <div class="section-head">
        <div>
          <div class="eyebrow">History</div>
          <h2>Recent Closed Trades</h2>
        </div>
        <p>최근 청산된 거래 10건</p>
      </div>
      <table>
        <tr><th>Chain</th><th>Symbol</th><th>Reason</th><th>PnL %</th><th>PnL USD</th><th>Exit Timestamp</th></tr>
        {''.join(closed_table_rows)}
      </table>
    </div>
  </div>
</body>
</html>"""
    return html


def main() -> None:
    html = render_dashboard()
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
