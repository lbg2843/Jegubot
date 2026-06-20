"""
Jegubot 운용 대시보드 (간결판 + 비용반영).

필요한 것만: 총/순 PnL · 일/주/월 PnL + 그래프 · 승률 · 오픈 포지션(실시간) · 거래내역.
기준일(CUTOFF) 이후만 집계. net = 슬리피지+수수료+가스(왕복 추정) 반영 → 실거래
기준의 정직한 손익. gross 는 스냅샷가 기준(비용 0).

Usage:
    python tools/build_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))

# 베이스라인 기준일 — 지난 일요일(버그 수정 직후 클린 구간). 바꾸려면 이 줄만.
CUTOFF = date(2026, 6, 14)

# 왕복 거래비용 추정(%) = LP수수료 + 슬리피지 + 가스. 보수적. env 로 덮어쓸 수 있음.
# BSC Pancake 0.25%x2 + 슬리피지 + 가스($100기준) ~ 1.5% / BASE Uniswap 0.3%x2 + 가스저렴 ~ 1.2%
def _cost_for(chain: str) -> float:
    chain = (chain or "").lower()
    default = {"bsc": 1.5, "base": 1.2, "solana": 1.5}.get(chain, 1.5)
    try:
        return float(os.getenv(f"{chain.upper()}_ROUNDTRIP_COST_PCT", str(default)))
    except ValueError:
        return default


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

sys.path.insert(0, str(Path(__file__).parent.parent))

from trading.safety import SafetyCircuitBreaker  # noqa: E402

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
OUTPUT_FILE = REPORTS_DIR / "dashboard.html"


CSS = """
:root {
  --bg:#f5efe1; --paper:#fffaf0; --ink:#1e1a16; --muted:#6f665d; --line:#d7c7b4;
  --accent:#005f73; --accent-2:#ca6702; --good:#2a9d8f; --bad:#ae2012; --card:#fffdf8;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:28px; color:var(--ink); font-family:Georgia,"Times New Roman",serif;
  background: radial-gradient(circle at top right, rgba(202,103,2,0.10), transparent 30%),
              linear-gradient(180deg,#fbf7ef 0%, var(--bg) 100%);
}
.wrap { max-width:1180px; margin:0 auto; }
.panel { background:rgba(255,253,248,0.96); border:1px solid var(--line); border-radius:18px;
         padding:18px 20px; box-shadow:0 18px 40px rgba(52,35,15,0.08); }
.eyebrow { color:var(--accent); letter-spacing:0.18em; font-size:12px; text-transform:uppercase; margin-bottom:8px; }
h1,h2,h3 { margin:0; }
h1 { font-size:34px; line-height:1.05; }
.subtitle { color:var(--muted); margin-top:8px; font-size:14px; }
.stamp { display:inline-block; margin-top:12px; padding:7px 12px; border:1px solid var(--line);
         border-radius:999px; color:var(--muted); font-size:13px; }
.grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:18px 0; }
.metric { min-height:118px; }
.metric .label { font-size:12px; text-transform:uppercase; letter-spacing:0.12em; color:var(--muted); }
.metric .value { margin-top:10px; font-size:28px; font-weight:700; }
.metric .hint { margin-top:8px; color:var(--muted); font-size:13px; }
.section { margin-top:18px; }
.section-head { display:flex; justify-content:space-between; align-items:end; margin-bottom:12px; }
.section-head p { margin:0; color:var(--muted); font-size:14px; }
table { width:100%; border-collapse:collapse; overflow:hidden; border-radius:14px;
        background:var(--card); border:1px solid var(--line); }
th,td { padding:10px 12px; border-bottom:1px solid rgba(215,199,180,0.7); text-align:left; font-size:14px; }
th { background:#f1e7d8; color:#4c433c; font-size:12px; text-transform:uppercase; letter-spacing:0.10em; }
tr:last-child td { border-bottom:none; }
.pos { color:var(--good); font-weight:700; }
.neg { color:var(--bad); font-weight:700; }
.muted { color:var(--muted); }
.pill { display:inline-block; padding:4px 9px; border-radius:999px; font-size:12px; border:1px solid var(--line); }
.ok { color:var(--good); border-color:rgba(42,157,143,0.35); background:rgba(42,157,143,0.08); }
.err { color:var(--bad); border-color:rgba(174,32,18,0.35); background:rgba(174,32,18,0.08); }
@media (max-width:980px){ .grid{ grid-template-columns:repeat(2,1fr);} }
"""


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")


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


# ----------------------------------------------------------------------------
# 데이터 로드
# ----------------------------------------------------------------------------
def load_positions() -> tuple[list[dict], list[dict]]:
    path = DATA_DIR / "positions.jsonl"
    if not path.exists():
        return [], []
    open_positions: dict[tuple, dict] = {}
    closed_positions: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = (rec.get("chain"), rec.get("contract_address"))
            if rec.get("is_closed"):
                open_positions.pop(key, None)
                closed_positions.append(rec)
            else:
                open_positions[key] = rec
    return list(open_positions.values()), closed_positions


def load_latest_prices(open_positions: list[dict]) -> dict[tuple[str, str], tuple[float, str | None]]:
    wanted: dict[str, set[str]] = {}
    for pos in open_positions:
        chain = (pos.get("chain") or "").lower()
        contract = (pos.get("contract_address") or "").lower()
        if chain and contract:
            wanted.setdefault(chain, set()).add(contract)
    prices: dict[tuple[str, str], tuple[float, str | None]] = {}
    for chain, contracts in wanted.items():
        path = DATA_DIR / f"{chain}_snapshots.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(sub in line for sub in contracts):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                contract = (rec.get("contract_address") or "").lower()
                if contract not in contracts:
                    continue
                price = rec.get("price_usd")
                if price:
                    prices[(chain, contract)] = (float(price), rec.get("timestamp"))
    return prices


def enrich_open_positions_with_live_price(open_positions: list[dict]) -> None:
    live = load_latest_prices(open_positions)
    for pos in open_positions:
        key = ((pos.get("chain") or "").lower(), (pos.get("contract_address") or "").lower())
        hit = live.get(key)
        if hit and hit[0] > 0:
            pos["current_price"] = hit[0]
            pos["_live_price_ts"] = hit[1]


def load_safety_state() -> dict:
    path = DATA_DIR / "trading_safety_state.json"
    if not path.exists():
        return SafetyCircuitBreaker(path).get_status()
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# 집계 (gross = 스냅샷가, net = 비용 반영)
# ----------------------------------------------------------------------------
def _exit_kst(pos: dict) -> datetime | None:
    ts = pos.get("exit_timestamp")
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(KST)


def _gross_usd(pos: dict) -> float:
    try:
        return float(pos.get("realized_pnl_usd") or 0.0)
    except Exception:
        return 0.0


def _net_usd(pos: dict) -> float:
    size = float(pos.get("size_usd") or 0.0)
    return _gross_usd(pos) - size * _cost_for(pos.get("chain")) / 100.0


def _gross_pct(pos: dict) -> float | None:
    try:
        return float(pos.get("realized_pnl_pct"))
    except Exception:
        return None


def _net_pct(pos: dict) -> float | None:
    g = _gross_pct(pos)
    return None if g is None else g - _cost_for(pos.get("chain"))


def _stat(rows: list[dict]) -> dict:
    n = len(rows)
    gross = sum(_gross_usd(p) for p in rows)
    net = sum(_net_usd(p) for p in rows)
    wins = sum(1 for p in rows if _gross_usd(p) > 0)
    netwins = sum(1 for p in rows if _net_usd(p) > 0)
    return {
        "n": n, "gross": gross, "net": net,
        "win": (wins / n * 100.0 if n else 0.0),
        "netwin": (netwins / n * 100.0 if n else 0.0),
    }


def daily_series(closed: list[dict], cutoff: date, today: date) -> list[dict]:
    by_day: dict[date, list[dict]] = {}
    for p in closed:
        k = _exit_kst(p)
        if k and k.date() >= cutoff:
            by_day.setdefault(k.date(), []).append(p)
    series, gcum, ncum, d = [], 0.0, 0.0, cutoff
    while d <= today:
        day = by_day.get(d, [])
        g = sum(_gross_usd(p) for p in day)
        nt = sum(_net_usd(p) for p in day)
        gcum += g
        ncum += nt
        series.append({"date": d, "net": nt, "gcum": gcum, "ncum": ncum, "n": len(day)})
        d += timedelta(days=1)
    return series


def render_pnl_chart(series: list[dict]) -> str:
    """일별 순손익 막대 + 누적 곡선(net 굵게, gross 점선). 자체 SVG, 의존성 없음."""
    if not series or all(s["n"] == 0 for s in series):
        return "<p class='muted'>기준일 이후 청산 거래가 아직 없습니다.</p>"
    W, H = 820, 300
    pl, pr, pt, pb = 50, 16, 18, 42
    iw, ih = W - pl - pr, H - pt - pb
    n = len(series)
    vals = ([s["gcum"] for s in series] + [s["ncum"] for s in series]
            + [s["net"] for s in series] + [0.0])
    ymin, ymax = min(vals), max(vals)
    if ymax == ymin:
        ymax = ymin + 1

    def X(i: int) -> float:
        return pl + iw * (i + 0.5) / n

    def Y(v: float) -> float:
        return pt + ih * (1 - (v - ymin) / (ymax - ymin))

    zero = Y(0.0)
    bw = max(2.0, iw / n * 0.55)
    bars = []
    for i, s in enumerate(series):
        y = Y(s["net"])
        top = min(y, zero)
        h = max(abs(y - zero), 0.6)
        color = "#2a9d8f" if s["net"] >= 0 else "#ae2012"
        bars.append(f'<rect x="{X(i)-bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{color}" opacity="0.40"/>')
    gross_pts = " ".join(f"{X(i):.1f},{Y(s['gcum']):.1f}" for i, s in enumerate(series))
    net_pts = " ".join(f"{X(i):.1f},{Y(s['ncum']):.1f}" for i, s in enumerate(series))
    dots = "".join(f'<circle cx="{X(i):.1f}" cy="{Y(s["ncum"]):.1f}" r="2.6" fill="#005f73"/>' for i, s in enumerate(series))
    step = max(1, n // 8)
    xlabels = "".join(
        f'<text x="{X(i):.1f}" y="{H-pb+18:.1f}" font-size="11" fill="#6f665d" text-anchor="middle">{s["date"].strftime("%m/%d")}</text>'
        for i, s in enumerate(series) if i % step == 0 or i == n - 1
    )
    yvals = sorted({ymax, 0.0, ymin}, reverse=True)
    ylabels = "".join(
        f'<text x="{pl-8:.1f}" y="{Y(v)+3:.1f}" font-size="11" fill="#6f665d" text-anchor="end">${v:+.0f}</text>'
        for v in yvals
    )
    return (
        f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px">'
        f'<line x1="{pl}" y1="{zero:.1f}" x2="{W-pr}" y2="{zero:.1f}" stroke="#d7c7b4" stroke-dasharray="3 3"/>'
        f'{"".join(bars)}'
        f'<polyline points="{gross_pts}" fill="none" stroke="#b9a88f" stroke-width="1.5" stroke-dasharray="4 3"/>'
        f'<polyline points="{net_pts}" fill="none" stroke="#005f73" stroke-width="2.6"/>'
        f'{dots}{xlabels}{ylabels}</svg>'
        '<div class="hint" style="margin-top:8px">막대 = 일별 순손익 · '
        '<span style="color:#005f73">━ 누적 순(net)</span> · '
        '<span style="color:#b9a88f">┈ 누적 총(gross)</span></div>'
    )


# ----------------------------------------------------------------------------
# 렌더
# ----------------------------------------------------------------------------
def render_dashboard() -> str:
    _load_env()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    open_positions, closed = load_positions()
    enrich_open_positions_with_live_price(open_positions)
    safety = load_safety_state()

    now_kst = datetime.now(KST)
    today = now_kst.date()
    week_start = today - timedelta(days=today.weekday())

    def bucket(pred):
        return [p for p in closed if pred(p)]

    total = bucket(lambda p: (_exit_kst(p) or datetime.min.replace(tzinfo=KST)).date() >= CUTOFF)
    day_rows = bucket(lambda p: (_exit_kst(p) and _exit_kst(p).date() == today))
    week_rows = bucket(lambda p: (_exit_kst(p) and _exit_kst(p).date() >= week_start))
    month_rows = bucket(lambda p: (_exit_kst(p) and _exit_kst(p).year == today.year and _exit_kst(p).month == today.month))

    s_total, s_today, s_week, s_month = _stat(total), _stat(day_rows), _stat(week_rows), _stat(month_rows)
    s_life = _stat(closed)

    unreal, committed = 0.0, 0.0
    for p in open_positions:
        size = float(p.get("size_usd") or 0.0)
        entry = float(p.get("entry_price") or 0.0)
        cur = float(p.get("current_price") or 0.0)
        committed += size
        if entry > 0:
            unreal += size * ((cur / entry) - 1.0)

    series = daily_series(closed, CUTOFF, today)
    chart = render_pnl_chart(series)

    def prow(label, st):
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{_fmt_usd(st['net'])}</td>"
            f"<td class='muted'>{_fmt_usd(st['gross'])}</td>"
            f"<td>{st['n']}</td>"
            f"<td>{st['win']:.0f}%</td>"
            "</tr>"
        )
    period_rows = (
        prow("오늘", s_today) + prow("이번 주", s_week) + prow("이번 달", s_month)
        + prow(f"총 (기준일 {CUTOFF} 이후)", s_total)
    )

    open_sorted = sorted(open_positions, key=lambda r: float(r.get("size_usd") or 0.0), reverse=True)
    open_html = []
    for pos in open_sorted:
        entry = float(pos.get("entry_price") or 0.0)
        cur = float(pos.get("current_price") or 0.0)
        size = float(pos.get("size_usd") or 0.0)
        pnl_pct = ((cur / entry) - 1.0) * 100.0 if entry else 0.0
        live = " <span class='pill ok'>live</span>" if pos.get("_live_price_ts") else ""
        open_html.append(
            "<tr>"
            f"<td>{(pos.get('chain') or '').upper()}</td>"
            f"<td>{pos.get('symbol') or '-'}</td>"
            f"<td>${entry:.6g}</td>"
            f"<td>${cur:.6g}{live}</td>"
            f"<td>{_fmt_pct(pnl_pct)}</td>"
            f"<td>{_fmt_usd(size * pnl_pct / 100.0)}</td>"
            f"<td>{_fmt_usd(size)}</td>"
            "</tr>"
        )
    if not open_html:
        open_html.append("<tr><td colspan='7' class='muted'>오픈 포지션 없음</td></tr>")

    hist = sorted(total, key=lambda p: _exit_kst(p) or datetime.min.replace(tzinfo=KST), reverse=True)[:50]
    hist_html = []
    for p in hist:
        k = _exit_kst(p)
        hist_html.append(
            "<tr>"
            f"<td>{k.strftime('%m/%d %H:%M') if k else '-'}</td>"
            f"<td>{(p.get('chain') or '').upper()}</td>"
            f"<td>{p.get('symbol') or '-'}</td>"
            f"<td>{p.get('exit_reason') or '-'}</td>"
            f"<td>{_fmt_pct(_net_pct(p))}</td>"
            f"<td>{_fmt_usd(_net_usd(p))}</td>"
            f"<td class='muted'>{_fmt_usd(_gross_usd(p))}</td>"
            "</tr>"
        )
    if not hist_html:
        hist_html.append("<tr><td colspan='7' class='muted'>기준일 이후 청산 없음</td></tr>")

    halt = safety.get("halt_reason") or "off"
    halt_pill = "<span class='pill ok'>off</span>" if halt == "off" else f"<span class='pill err'>{halt}</span>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jegubot Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <div class="eyebrow">Jegubot · Paper</div>
      <h1>{_fmt_usd(s_total['net'])} <span style="font-size:16px;color:var(--muted)">순 PnL(비용반영) · gross {_fmt_usd(s_total['gross'])} · 승률 {s_total['win']:.0f}% · {s_total['n']}건</span></h1>
      <div class="subtitle">기준일 {CUTOFF} 이후. net = 왕복비용(BSC 1.5% / BASE 1.2%: 수수료+슬리피지+가스) 반영한 실거래 추정. Lifetime net {_fmt_usd(s_life['net'])} (gross {_fmt_usd(s_life['gross'])}, {s_life['n']}건).</div>
      <div class="stamp">Generated {now_kst.strftime("%Y-%m-%d %H:%M")} KST · Safety halt {halt_pill}</div>
    </div>

    <div class="grid">
      <div class="panel metric"><div class="label">오늘 (net)</div><div class="value">{_fmt_usd(s_today['net'])}</div><div class="hint">gross {_fmt_usd(s_today['gross'])} · {s_today['n']}건 · 승률 {s_today['win']:.0f}%</div></div>
      <div class="panel metric"><div class="label">이번 주 (net)</div><div class="value">{_fmt_usd(s_week['net'])}</div><div class="hint">gross {_fmt_usd(s_week['gross'])} · {s_week['n']}건 · 승률 {s_week['win']:.0f}%</div></div>
      <div class="panel metric"><div class="label">이번 달 (net)</div><div class="value">{_fmt_usd(s_month['net'])}</div><div class="hint">gross {_fmt_usd(s_month['gross'])} · {s_month['n']}건 · 승률 {s_month['win']:.0f}%</div></div>
      <div class="panel metric"><div class="label">오픈 평가손익</div><div class="value">{_fmt_usd(unreal)}</div><div class="hint">{len(open_positions)}개 · 투입 ${committed:.0f} (gross)</div></div>
    </div>

    <div class="section panel">
      <div class="section-head"><div><div class="eyebrow">Equity</div><h2>누적 손익 추이</h2></div><p>기준일 이후 · net vs gross</p></div>
      {chart}
    </div>

    <div class="section panel">
      <div class="section-head"><div><div class="eyebrow">Periods</div><h2>기간별 손익 · 승률</h2></div></div>
      <table>
        <tr><th>기간</th><th>순 PnL</th><th>gross</th><th>거래수</th><th>승률</th></tr>
        {period_rows}
      </table>
    </div>

    <div class="section panel">
      <div class="section-head"><div><div class="eyebrow">Ledger</div><h2>오픈 포지션 (실시간)</h2></div></div>
      <table>
        <tr><th>Chain</th><th>Symbol</th><th>진입가</th><th>현재가</th><th>PnL %</th><th>PnL $</th><th>Size</th></tr>
        {''.join(open_html)}
      </table>
    </div>

    <div class="section panel">
      <div class="section-head"><div><div class="eyebrow">History</div><h2>거래내역</h2></div><p>기준일 이후 청산 (최신 {len(hist)}건, PnL=net)</p></div>
      <table>
        <tr><th>시각(KST)</th><th>Chain</th><th>Symbol</th><th>청산사유</th><th>순 PnL %</th><th>순 $</th><th>gross $</th></tr>
        {''.join(hist_html)}
      </table>
    </div>
  </div>
</body>
</html>"""


def main() -> None:
    html = render_dashboard()
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
