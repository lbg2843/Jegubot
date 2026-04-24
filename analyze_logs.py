"""
analyze_logs.py — Jegubot Reflexivity 성과 분석기
후보 토큰 추출 → 이후 가격 변화 추적 → 가상 수익률 → HTML 리포트
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ─── 설정 ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "reflexivity.log"
REPORT_FILE = BASE_DIR / "analysis_report.html"

# 체인별 파라미터 (orchestrator와 동일하게)
CHAIN_CONFIG = {
    "bsc":    {"alloc": 0.25, "fee": 0.0025, "slippage": 0.003, "hold_min": 60,  "target_pct": 15, "stop_pct": -8},
    "base":   {"alloc": 0.55, "fee": 0.003,  "slippage": 0.005, "hold_min": 240, "target_pct": 20, "stop_pct": -10},
    "solana": {"alloc": 0.20, "fee": 0.001,  "slippage": 0.002, "hold_min": 30,  "target_pct": 12, "stop_pct": -6},
}
TOTAL_CAPITAL_USD = 10_000
POSITION_SIZE_PCT = 0.05  # 포지션당 5%


# ─── 1. 스냅샷 로드 ──────────────────────────────────────────────────────────

def load_snapshots(chain: str) -> dict[str, list[dict]]:
    """symbol → [{timestamp, price_usd, ...}, ...] 정렬된 시계열"""
    path = DATA_DIR / f"{chain}_snapshots.jsonl"
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                sym = d.get("symbol", "?").upper()
                d["_ts"] = datetime.fromisoformat(d["timestamp"])
                by_symbol[sym].append(d)
            except Exception:
                pass
    for sym in by_symbol:
        by_symbol[sym].sort(key=lambda x: x["_ts"])
    return dict(by_symbol)


# ─── 2. 로그에서 후보 감지 이벤트 파싱 ─────────────────────────────────────

CAND_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[DRY-RUN\] 진입 후보: \[(\w+)\] (\S+) price=\$([0-9.]+)"
)
CYCLE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?사이클 시작")
ERR_RE   = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[(ERROR|WARNING)\].*?:\s*(.*)")


def parse_log() -> tuple[list[dict], list[datetime], list[tuple]]:
    candidates = []
    cycles = []
    errors = []
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            if m := CAND_RE.search(line):
                candidates.append({
                    "ts": datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                    "chain": m.group(2).lower(),
                    "symbol": m.group(3).upper(),
                    "entry_price": float(m.group(4)),
                })
            if m := CYCLE_RE.search(line):
                cycles.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            if m := ERR_RE.search(line):
                errors.append((m.group(2), m.group(3).strip()))
    return candidates, cycles, errors


# ─── 3. 이후 가격 조회 ──────────────────────────────────────────────────────

def price_after(ts_list: list[dict], after_ts: datetime) -> float | None:
    """ts_list에서 after_ts 이후 첫 번째 레코드의 가격 반환"""
    for rec in ts_list:
        if rec["_ts"] >= after_ts:
            return rec.get("price_usd")
    return None


def track_candidate(cand: dict, snapshots: dict[str, list[dict]]) -> dict:
    chain = cand["chain"]
    sym   = cand["symbol"]
    entry_ts    = cand["ts"]
    entry_price = cand["entry_price"]
    cfg   = CHAIN_CONFIG.get(chain, {})

    ts_list = snapshots.get(chain, {}).get(sym, [])

    p1h  = price_after(ts_list, entry_ts + timedelta(hours=1))
    p4h  = price_after(ts_list, entry_ts + timedelta(hours=4))
    p24h = price_after(ts_list, entry_ts + timedelta(hours=24))

    def pct(p):
        if p is None or entry_price == 0:
            return None
        return (p / entry_price - 1) * 100

    r1h, r4h, r24h = pct(p1h), pct(p4h), pct(p24h)

    # 가상 수익률 계산
    position_usd = TOTAL_CAPITAL_USD * POSITION_SIZE_PCT
    fee_in  = position_usd * cfg.get("fee", 0.003)
    slippage_in = position_usd * cfg.get("slippage", 0.005)
    gross_cost = position_usd + fee_in + slippage_in

    target = cfg.get("target_pct", 15) / 100
    stop   = cfg.get("stop_pct", -8) / 100
    hold_h = cfg.get("hold_min", 60) / 60

    # hold_h 이후 가격으로 실제 수익률 결정
    p_exit = price_after(ts_list, entry_ts + timedelta(hours=hold_h))
    actual_pct = pct(p_exit)

    if actual_pct is None:
        sim_pnl = None
    else:
        # 목표/손절 중 먼저 도달했는지 근사 (스냅샷으로 단순 확인)
        # 1h 이내에 목표 또는 손절 터치 여부
        exit_pct = actual_pct / 100
        capped = max(stop, min(target, exit_pct))
        proceeds = position_usd * (1 + capped)
        fee_out = proceeds * cfg.get("fee", 0.003)
        slip_out = proceeds * cfg.get("slippage", 0.005)
        sim_pnl = proceeds - fee_out - slip_out - gross_cost

    return {
        **cand,
        "r1h": r1h, "r4h": r4h, "r24h": r24h,
        "actual_pct": actual_pct,
        "sim_pnl": sim_pnl,
        "position_usd": position_usd,
    }


# ─── 4. 진단 지표 ────────────────────────────────────────────────────────────

def analyze_gaps(chain: str) -> dict:
    path = DATA_DIR / f"{chain}_snapshots.jsonl"
    if not path.exists():
        return {}
    ts_list = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                ts_list.append(datetime.fromisoformat(d["timestamp"]))
            except Exception:
                pass
    if len(ts_list) < 2:
        return {}
    ts_list.sort()
    gaps = [(ts_list[i+1]-ts_list[i]).total_seconds()/60 for i in range(len(ts_list)-1)]
    normal = sum(1 for g in gaps if g < 15)
    abnormal = [g for g in gaps if g >= 15]
    span_h = (ts_list[-1]-ts_list[0]).total_seconds()/3600
    return {
        "records": len(ts_list),
        "span_h": round(span_h, 1),
        "first": ts_list[0].strftime("%m/%d %H:%M"),
        "last": ts_list[-1].strftime("%m/%d %H:%M"),
        "normal_pct": round(100*normal/len(gaps), 1),
        "abnormal_cnt": len(abnormal),
        "max_gap_min": round(max(abnormal), 0) if abnormal else 0,
    }


# ─── 5. 에러 집계 ────────────────────────────────────────────────────────────

def summarize_errors(errors: list[tuple]) -> list[tuple]:
    cnt: dict[str, int] = defaultdict(int)
    for level, msg in errors:
        key = re.sub(r"0x[0-9a-fA-F]+|position \d+|\$[\d.]+|byte 0x\w+", "…", msg)[:120]
        cnt[f"[{level}] {key}"] += 1
    return sorted(cnt.items(), key=lambda x: -x[1])[:10]


# ─── 6. HTML 리포트 생성 ─────────────────────────────────────────────────────

CSS = """
body{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;margin:0;padding:20px}
h1{color:#7ee8fa;border-bottom:2px solid #333;padding-bottom:8px}
h2{color:#f9ca24;margin-top:30px}
table{border-collapse:collapse;width:100%;margin-bottom:20px;font-size:13px}
th{background:#1e2235;color:#aaa;padding:8px 12px;text-align:left;border-bottom:2px solid #333}
td{padding:6px 12px;border-bottom:1px solid #222}
tr:hover td{background:#1a1f2e}
.pos{color:#2ecc71}.neg{color:#e74c3c}.neutral{color:#888}
.badge{padding:2px 8px;border-radius:10px;font-size:11px}
.bsc{background:#f0b90b22;color:#f0b90b}
.base{background:#0052ff22;color:#60a5fa}
.solana{background:#9945ff22;color:#c084fc}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat-box{background:#1e2235;border-radius:8px;padding:16px;text-align:center}
.stat-val{font-size:24px;font-weight:bold;color:#7ee8fa}
.stat-lbl{font-size:11px;color:#888;margin-top:4px}
"""

def fmt_pct(v, decimals=1):
    if v is None: return '<span class="neutral">—</span>'
    cls = "pos" if v > 0 else "neg" if v < 0 else "neutral"
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}{v:.{decimals}f}%</span>'

def fmt_usd(v):
    if v is None: return '<span class="neutral">—</span>'
    cls = "pos" if v > 0 else "neg" if v < 0 else "neutral"
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}${v:.2f}</span>'

def build_report(candidates_tracked, gap_stats, cycles, errors):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 사이클 통계
    if cycles:
        span_min = (cycles[-1]-cycles[0]).total_seconds()/60
        expected = int(span_min/10)
        actual_cyc = len(cycles)
        uptime_pct = round(100*actual_cyc/max(1,expected), 1)
    else:
        expected = actual_cyc = uptime_pct = 0

    # ── 후보 통계 (고유 심볼 기준 첫 감지만)
    seen = set()
    unique_cands = []
    for c in sorted(candidates_tracked, key=lambda x: x["ts"]):
        key = (c["chain"], c["symbol"])
        if key not in seen:
            seen.add(key)
            unique_cands.append(c)

    total_cands = len(unique_cands)
    chain_cnt = defaultdict(int)
    for c in unique_cands: chain_cnt[c["chain"]] += 1

    tracked = [c for c in unique_cands if c["r1h"] is not None]
    winners_1h = [c for c in tracked if (c["r1h"] or 0) > 0]
    avg_r1h = sum(c["r1h"] for c in tracked)/len(tracked) if tracked else None
    avg_r4h = sum(c["r4h"] for c in tracked if c["r4h"] is not None) / max(1, sum(1 for c in tracked if c["r4h"] is not None)) if tracked else None

    total_sim_pnl = sum(c["sim_pnl"] for c in unique_cands if c["sim_pnl"] is not None)
    win_trades = sum(1 for c in unique_cands if (c["sim_pnl"] or 0) > 0)
    total_trades = sum(1 for c in unique_cands if c["sim_pnl"] is not None)

    # ── 에러 요약
    err_summary = summarize_errors(errors)

    # ─── HTML 조립 ──────────────────────────────────────────────────────────
    parts = [f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Jegubot 분석 리포트 — {now}</title>
<style>{CSS}</style></head><body>
<h1>Jegubot Reflexivity — 성과 분석 리포트</h1>
<p style="color:#888">생성: {now} | 기간: {gap_stats.get('bsc',{}).get('first','?')} ~ {gap_stats.get('bsc',{}).get('last','?')}</p>

<h2>📊 핵심 지표</h2>
<div class="stat-grid">
  <div class="stat-box"><div class="stat-val">{actual_cyc}</div><div class="stat-lbl">총 사이클 (예상 {expected})</div></div>
  <div class="stat-box"><div class="stat-val">{uptime_pct}%</div><div class="stat-lbl">가동률</div></div>
  <div class="stat-box"><div class="stat-val">{total_cands}</div><div class="stat-lbl">고유 후보 토큰</div></div>
  <div class="stat-box"><div class="stat-val" style="color:{'#2ecc71' if total_sim_pnl>0 else '#e74c3c'}">${total_sim_pnl:+.2f}</div><div class="stat-lbl">가상 누적 PnL</div></div>
</div>
"""]

    # ── 진단: 데이터 연속성
    parts.append("<h2>🔍 진단 1-3: 데이터 파일 상태</h2>")
    parts.append("""<table><tr><th>체인</th><th>레코드 수</th><th>기간 (시간)</th>
<th>첫 기록</th><th>마지막 기록</th><th>정상 간격 %</th><th>비정상 횟수</th><th>최대 GAP (분)</th></tr>""")
    for chain in ["bsc", "base", "solana"]:
        g = gap_stats.get(chain, {})
        if not g:
            parts.append(f"<tr><td>{chain.upper()}</td><td colspan='7' class='neutral'>파일 없음</td></tr>")
            continue
        norm_cls = "pos" if g['normal_pct'] > 90 else "neg"
        parts.append(f"""<tr>
  <td><span class="badge {chain}">{chain.upper()}</span></td>
  <td>{g['records']:,}</td>
  <td>{g['span_h']}</td>
  <td>{g['first']}</td>
  <td>{g['last']}</td>
  <td class="{norm_cls}">{g['normal_pct']}%</td>
  <td class="{'neg' if g['abnormal_cnt']>10 else 'neutral'}">{g['abnormal_cnt']}</td>
  <td class="{'neg' if g['max_gap_min']>60 else 'neutral'}">{g['max_gap_min']:.0f}</td>
</tr>""")
    parts.append("</table>")

    # ── 진단: 사이클
    parts.append(f"""<h2>🔄 진단 4: 사이클 통계</h2>
<table><tr><th>지표</th><th>값</th></tr>
<tr><td>실제 사이클 수</td><td>{actual_cyc}</td></tr>
<tr><td>예상 사이클 수 (10분 간격)</td><td>{expected}</td></tr>
<tr><td>가동률</td><td class="{'pos' if uptime_pct>80 else 'neg'}">{uptime_pct}%</td></tr>
</table>""")

    # ── 진단: 후보 감지 빈도
    parts.append("<h2>🎯 진단 5: 후보 감지 (고유 토큰 기준)</h2>")
    parts.append("""<table><tr><th>체인</th><th>고유 후보 수</th><th>1h 추적 가능</th>
<th>평균 +1h</th><th>평균 +4h</th><th>승률 (+1h > 0)</th></tr>""")
    for chain in ["bsc", "base", "solana"]:
        cc = [c for c in unique_cands if c["chain"] == chain]
        tr = [c for c in cc if c["r1h"] is not None]
        a1 = sum(c["r1h"] for c in tr)/len(tr) if tr else None
        a4 = sum(c["r4h"] for c in tr if c["r4h"] is not None)/max(1,sum(1 for c in tr if c["r4h"] is not None)) if tr else None
        wr = sum(1 for c in tr if (c["r1h"] or 0)>0)/len(tr)*100 if tr else None
        parts.append(f"""<tr>
  <td><span class="badge {chain}">{chain.upper()}</span></td>
  <td>{len(cc)}</td>
  <td>{len(tr)}</td>
  <td>{fmt_pct(a1)}</td>
  <td>{fmt_pct(a4)}</td>
  <td>{fmt_pct(wr)}</td>
</tr>""")
    parts.append("</table>")

    # ── 진단: 에러
    parts.append("<h2>⚠️ 진단 6: 에러 / 경고 Top 10</h2>")
    parts.append("<table><tr><th>횟수</th><th>메시지</th></tr>")
    if err_summary:
        for msg, cnt in err_summary:
            parts.append(f"<tr><td style='color:#e74c3c'>{cnt}</td><td style='font-size:12px'>{msg}</td></tr>")
    else:
        parts.append("<tr><td colspan='2' class='pos'>에러 없음</td></tr>")
    parts.append("</table>")

    # ── 후보 토큰 상세 테이블
    parts.append("<h2>📈 후보 토큰 상세 (고유 첫 감지 기준)</h2>")
    parts.append("""<table><tr>
  <th>감지 시각</th><th>체인</th><th>심볼</th><th>진입가</th>
  <th>+1h</th><th>+4h</th><th>+24h</th><th>가상 PnL</th>
</tr>""")
    for c in sorted(unique_cands, key=lambda x: x["ts"], reverse=True):
        parts.append(f"""<tr>
  <td style="font-size:11px;color:#888">{c['ts'].strftime('%m/%d %H:%M')}</td>
  <td><span class="badge {c['chain']}">{c['chain'].upper()}</span></td>
  <td style="font-weight:bold">{c['symbol']}</td>
  <td style="font-size:11px">${c['entry_price']:.6g}</td>
  <td>{fmt_pct(c['r1h'])}</td>
  <td>{fmt_pct(c['r4h'])}</td>
  <td>{fmt_pct(c['r24h'])}</td>
  <td>{fmt_usd(c['sim_pnl'])}</td>
</tr>""")
    parts.append("</table>")

    # ── 체인별 성과 비교
    parts.append("<h2>⚖️ 체인별 가상 성과 비교</h2>")
    parts.append("""<table><tr>
  <th>체인</th><th>포지션수</th><th>승리</th><th>승률</th>
  <th>총 PnL</th><th>평균 PnL/trade</th>
</tr>""")
    for chain in ["bsc", "base", "solana"]:
        cc = [c for c in unique_cands if c["chain"] == chain and c["sim_pnl"] is not None]
        if not cc:
            parts.append(f"<tr><td><span class='badge {chain}'>{chain.upper()}</span></td><td colspan='5' class='neutral'>데이터 부족</td></tr>")
            continue
        w = sum(1 for c in cc if c["sim_pnl"] > 0)
        wr_c = w/len(cc)*100
        tot = sum(c["sim_pnl"] for c in cc)
        avg = tot/len(cc)
        parts.append(f"""<tr>
  <td><span class="badge {chain}">{chain.upper()}</span></td>
  <td>{len(cc)}</td>
  <td>{w}</td>
  <td>{fmt_pct(wr_c)}</td>
  <td>{fmt_usd(tot)}</td>
  <td>{fmt_usd(avg)}</td>
</tr>""")
    parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("=== Jegubot Reflexivity 분석 시작 ===\n")

    # 스냅샷 로드
    print("[1] 스냅샷 로딩...")
    snapshots = {}
    for chain in ["bsc", "base", "solana"]:
        snapshots[chain] = load_snapshots(chain)
        total = sum(len(v) for v in snapshots[chain].values())
        print(f"  {chain.upper()}: {len(snapshots[chain])} 심볼, {total:,} 레코드")

    # 로그 파싱
    print("\n[2] 로그 파싱...")
    candidates, cycles, errors = parse_log()
    print(f"  후보 감지 이벤트: {len(candidates)}건")
    print(f"  사이클: {len(cycles)}회")
    print(f"  에러/경고: {len(errors)}건")

    # 후보 추적
    print("\n[3] 후보 가격 추적...")
    tracked = []
    for c in candidates:
        result = track_candidate(c, snapshots)
        tracked.append(result)

    # 갭 분석
    print("\n[4] 데이터 연속성 분석...")
    gap_stats = {}
    for chain in ["bsc", "base", "solana"]:
        g = analyze_gaps(chain)
        gap_stats[chain] = g
        if g:
            print(f"  {chain.upper()}: {g['records']} records, {g['normal_pct']}% normal, max gap {g['max_gap_min']:.0f}min")

    # 에러 요약 출력
    print("\n[5] 에러 Top 5:")
    for msg, cnt in summarize_errors(errors)[:5]:
        print(f"  {cnt:4d}x  {msg[:80]}")

    # 후보 요약 출력
    seen = set()
    unique = []
    for c in sorted(tracked, key=lambda x: x["ts"]):
        k = (c["chain"], c["symbol"])
        if k not in seen:
            seen.add(k)
            unique.append(c)

    print(f"\n[6] 고유 후보 토큰: {len(unique)}개")
    trackable = [c for c in unique if c["r1h"] is not None]
    if trackable:
        avg1 = sum(c["r1h"] for c in trackable)/len(trackable)
        win1 = sum(1 for c in trackable if c["r1h"] > 0)
        print(f"  추적 가능: {len(trackable)}개")
        print(f"  평균 +1h: {avg1:+.2f}%")
        print(f"  승률 (+1h): {100*win1/len(trackable):.1f}%")

    sim_pnls = [c["sim_pnl"] for c in unique if c["sim_pnl"] is not None]
    if sim_pnls:
        print(f"  가상 총 PnL: ${sum(sim_pnls):+.2f}")

    # 리포트 생성
    print(f"\n[7] HTML 리포트 생성: {REPORT_FILE}")
    html = build_report(tracked, gap_stats, cycles, errors)
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"완료! 브라우저에서 열어보세요: {REPORT_FILE}")


if __name__ == "__main__":
    main()
