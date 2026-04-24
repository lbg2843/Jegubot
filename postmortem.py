"""
postmortem.py - 41개 후보 토큰 사후 성과 분석
감지 이후 가격 추적 → 트레일링 스탑 시뮬 → Markdown 리포트
"""

import io, sys, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ─── 설정 ────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
LOG_FILE   = BASE_DIR / "reflexivity.log"
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

TOTAL_CAPITAL  = 10_000
POSITION_FRAC  = 0.05   # 포지션당 자본 5%

CHAIN_PARAMS = {
    "bsc": {
        "stop_pct":   -0.20,   # 스탑 -20%
        "trail_pct":   0.15,   # 트레일링 15%
        "target_pct":  0.80,   # 익절 +80%
        "max_hours":  72,
        "fee":         0.0025,
        "slippage":    0.003,
    },
    "solana": {
        "stop_pct":   -0.25,
        "trail_pct":   0.18,
        "target_pct":  1.00,
        "max_hours":  48,
        "fee":         0.001,
        "slippage":    0.002,
    },
    "base": {
        "stop_pct":   -0.15,
        "trail_pct":   0.12,
        "target_pct":  0.60,
        "max_hours":  96,
        "fee":         0.003,
        "slippage":    0.005,
    },
}


# ─── 1. 스냅샷 로드 ──────────────────────────────────────────────────────────

def load_all_snapshots() -> dict[str, dict[str, list[dict]]]:
    """chain -> symbol -> sorted list of records"""
    result = {}
    for chain in ["bsc", "base", "solana"]:
        by_sym: dict[str, list[dict]] = defaultdict(list)
        path = DATA_DIR / f"{chain}_snapshots.jsonl"
        if not path.exists():
            result[chain] = {}
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    sym = d.get("symbol", "").upper()
                    d["_ts"] = datetime.fromisoformat(d["timestamp"])
                    d["_price"] = float(d.get("price_usd") or 0)
                    by_sym[sym].append(d)
                except Exception:
                    pass
        for sym in by_sym:
            by_sym[sym].sort(key=lambda x: x["_ts"])
        result[chain] = dict(by_sym)
    return result


# ─── 2. 로그에서 고유 후보 파싱 ─────────────────────────────────────────────

LOG_CAND_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+"
    r".*?\[DRY-RUN\] 진입 후보: \[(\w+)\] (\S+) price=\$([0-9.]+)"
)

def parse_unique_candidates() -> list[dict]:
    seen: dict[tuple, dict] = {}
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_CAND_RE.search(line)
            if not m:
                continue
            ts    = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            chain = m.group(2).lower()
            sym   = m.group(3).upper()
            price = float(m.group(4))
            key   = (chain, sym)
            if key not in seen:
                seen[key] = {
                    "ts":          ts,
                    "chain":       chain,
                    "symbol":      sym,
                    "entry_price": price,
                }
    return sorted(seen.values(), key=lambda x: x["ts"])


# ─── 3. 가격 시계열 슬라이스 ────────────────────────────────────────────────

def get_series_after(ts_list: list[dict], from_ts: datetime) -> list[dict]:
    """from_ts 이후 레코드만 반환 (시간순)"""
    return [r for r in ts_list if r["_ts"] >= from_ts]


def price_at(ts_list: list[dict], target_ts: datetime) -> float | None:
    """target_ts 이후 첫 번째 유효 가격"""
    for r in ts_list:
        if r["_ts"] >= target_ts and r["_price"] > 0:
            return r["_price"]
    return None


# ─── 4. 트레일링 스탑 시뮬레이션 ────────────────────────────────────────────

def simulate_trade(cand: dict, series: list[dict]) -> dict:
    """
    series: 감지 시점 이후 가격 시계열 (시간순)
    반환: exit_ts, exit_price, exit_reason, pnl_pct, pnl_usd, max_high_pct, max_low_pct
    """
    chain  = cand["chain"]
    params = CHAIN_PARAMS.get(chain, CHAIN_PARAMS["bsc"])
    entry  = cand["entry_price"]
    entry_ts = cand["ts"]

    stop_pct   = params["stop_pct"]
    trail_pct  = params["trail_pct"]
    target_pct = params["target_pct"]
    max_ts     = entry_ts + timedelta(hours=params["max_hours"])

    if entry <= 0 or not series:
        return _no_data(cand)

    # 트레일링 스탑 초기값: 진입가 기준 고정 스탑
    trail_high  = entry          # 현재까지 최고점
    trail_stop  = entry * (1 + stop_pct)   # 트레일링 스탑 가격

    abs_max_price = entry
    abs_min_price = entry
    exit_ts     = None
    exit_price  = None
    exit_reason = "max_time"

    for rec in series:
        ts    = rec["_ts"]
        price = rec["_price"]
        if price <= 0:
            continue

        # 최대/최소 추적
        abs_max_price = max(abs_max_price, price)
        abs_min_price = min(abs_min_price, price)

        # 익절 체크
        if price >= entry * (1 + target_pct):
            exit_ts     = ts
            exit_price  = entry * (1 + target_pct)
            exit_reason = "target"
            break

        # 트레일링 고점 갱신 → 스탑 상향
        if price > trail_high:
            trail_high = price
            new_stop   = trail_high * (1 - trail_pct)
            trail_stop = max(trail_stop, new_stop)

        # 트레일링 스탑 터치
        if price <= trail_stop:
            exit_ts     = ts
            exit_price  = trail_stop
            exit_reason = "trail_stop"
            break

        # 시간 초과
        if ts >= max_ts:
            exit_ts     = ts
            exit_price  = price
            exit_reason = "max_time"
            break

    # 시계열 소진 (데이터 부족)
    if exit_price is None:
        last = series[-1]
        exit_ts     = last["_ts"]
        exit_price  = last["_price"]
        exit_reason = "data_end"

    pos_usd   = TOTAL_CAPITAL * POSITION_FRAC
    fee_in    = pos_usd * params["fee"]
    slip_in   = pos_usd * params["slippage"]
    cost      = pos_usd + fee_in + slip_in

    raw_pnl_pct = (exit_price / entry - 1) if entry > 0 else 0
    proceeds    = pos_usd * (1 + raw_pnl_pct)
    fee_out     = proceeds * params["fee"]
    slip_out    = proceeds * params["slippage"]
    net_usd     = proceeds - fee_out - slip_out - cost

    max_high_pct = (abs_max_price / entry - 1) * 100 if entry > 0 else None
    max_low_pct  = (abs_min_price / entry - 1) * 100 if entry > 0 else None

    return {
        **cand,
        "exit_ts":      exit_ts,
        "exit_price":   exit_price,
        "exit_reason":  exit_reason,
        "pnl_pct":      raw_pnl_pct * 100,
        "pnl_usd":      net_usd,
        "max_high_pct": max_high_pct,
        "max_low_pct":  max_low_pct,
        "p1h":          price_at(series, entry_ts + timedelta(hours=1)),
        "p4h":          price_at(series, entry_ts + timedelta(hours=4)),
        "p24h":         price_at(series, entry_ts + timedelta(hours=24)),
    }


def _no_data(cand: dict) -> dict:
    return {**cand, "exit_ts": None, "exit_price": None, "exit_reason": "no_data",
            "pnl_pct": None, "pnl_usd": None,
            "max_high_pct": None, "max_low_pct": None,
            "p1h": None, "p4h": None, "p24h": None}


def pct_str(v, entry):
    if v is None or entry is None or entry == 0:
        return "—"
    return f"{(v/entry - 1)*100:+.2f}%"

def pct_fmt(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


# ─── 5. Markdown 리포트 생성 ─────────────────────────────────────────────────

REASON_KO = {
    "target":     "익절",
    "trail_stop": "트레일스탑",
    "max_time":   "시간만료",
    "data_end":   "데이터부족",
    "no_data":    "데이터없음",
}

def build_markdown(results: list[dict]) -> str:
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    valid   = [r for r in results if r["pnl_pct"] is not None]
    winners = [r for r in valid if r["pnl_usd"] > 0]
    losers  = [r for r in valid if r["pnl_usd"] <= 0]

    total_pnl = sum(r["pnl_usd"] for r in valid)
    avg_win   = sum(r["pnl_usd"] for r in winners) / len(winners) if winners else 0
    avg_loss  = sum(r["pnl_usd"] for r in losers)  / len(losers)  if losers  else 0
    gross_win = sum(r["pnl_usd"] for r in winners)
    gross_loss= abs(sum(r["pnl_usd"] for r in losers))
    pf        = gross_win / gross_loss if gross_loss > 0 else float("inf")
    best      = max(valid, key=lambda x: x["pnl_usd"]) if valid else None
    worst     = min(valid, key=lambda x: x["pnl_usd"]) if valid else None

    lines = []
    lines.append(f"# Jegubot Reflexivity — 후보 토큰 사후 분석")
    lines.append(f"\n생성: {now}  |  분석 대상: {len(results)}개 고유 후보  |  유효 시뮬: {len(valid)}개\n")

    # ── 종합 요약
    lines.append("## 1. 종합 요약\n")
    lines.append("| 지표 | 값 |")
    lines.append("|------|-----|")
    lines.append(f"| 총 포지션 | {len(valid)}건 |")
    lines.append(f"| 승리 / 패배 | {len(winners)} / {len(losers)} |")
    lines.append(f"| 승률 | {100*len(winners)/len(valid):.1f}% |" if valid else "| 승률 | — |")
    lines.append(f"| 누적 PnL | ${total_pnl:+.2f} |")
    lines.append(f"| 평균 수익 (승) | ${avg_win:+.2f} |")
    lines.append(f"| 평균 손실 (패) | ${avg_loss:+.2f} |")
    lines.append(f"| Profit Factor | {pf:.2f} |")
    if best:
        lines.append(f"| 최고 승 | {best['symbol']} ({best['chain'].upper()}) {pct_fmt(best['pnl_pct'])} / ${best['pnl_usd']:+.2f} |")
    if worst:
        lines.append(f"| 최대 패 | {worst['symbol']} ({worst['chain'].upper()}) {pct_fmt(worst['pnl_pct'])} / ${worst['pnl_usd']:+.2f} |")
    lines.append("")

    # ── 체인별 성과
    lines.append("## 2. 체인별 성과\n")
    lines.append("| 체인 | 건수 | 승/패 | 승률 | 누적 PnL | 평균 PnL | Profit Factor |")
    lines.append("|------|------|-------|------|----------|----------|---------------|")
    for chain in ["bsc", "solana", "base"]:
        cc = [r for r in valid if r["chain"] == chain]
        if not cc:
            lines.append(f"| {chain.upper()} | 0 | — | — | — | — | — |")
            continue
        cw = [r for r in cc if r["pnl_usd"] > 0]
        cl = [r for r in cc if r["pnl_usd"] <= 0]
        cpnl = sum(r["pnl_usd"] for r in cc)
        cavg = cpnl / len(cc)
        cgw  = sum(r["pnl_usd"] for r in cw)
        cgl  = abs(sum(r["pnl_usd"] for r in cl))
        cpf  = cgw / cgl if cgl > 0 else float("inf")
        wr   = 100 * len(cw) / len(cc)
        lines.append(
            f"| {chain.upper()} | {len(cc)} | {len(cw)}/{len(cl)} | {wr:.1f}% "
            f"| ${cpnl:+.2f} | ${cavg:+.2f} | {cpf:.2f} |"
        )
    lines.append("")

    # ── 청산 사유별 분포
    lines.append("## 3. 청산 사유별 분포\n")
    reason_cnt: dict[str, list] = defaultdict(list)
    for r in valid:
        reason_cnt[r["exit_reason"]].append(r["pnl_usd"])
    lines.append("| 청산 사유 | 건수 | 합계 PnL | 평균 PnL |")
    lines.append("|-----------|------|----------|----------|")
    for reason in ["target", "trail_stop", "max_time", "data_end"]:
        vals = reason_cnt.get(reason, [])
        if not vals:
            continue
        tot = sum(vals)
        avg = tot / len(vals)
        lines.append(f"| {REASON_KO[reason]} | {len(vals)} | ${tot:+.2f} | ${avg:+.2f} |")
    lines.append("")

    # ── 개별 토큰 상세
    lines.append("## 4. 개별 토큰 상세\n")
    lines.append(
        "| # | 감지시각 | 체인 | 심볼 | 진입가 | +1h | +4h | +24h "
        "| 최대고점 | 최대저점 | 청산사유 | 청산시각 | PnL% | PnL USD |"
    )
    lines.append(
        "|---|----------|------|------|--------|-----|-----|------|"
        "----------|----------|----------|----------|------|---------|"
    )
    for i, r in enumerate(results, 1):
        entry = r["entry_price"]
        r1h   = pct_str(r["p1h"],   entry)
        r4h   = pct_str(r["p4h"],   entry)
        r24h  = pct_str(r["p24h"],  entry)
        hi    = pct_fmt(r["max_high_pct"]) if r["max_high_pct"] is not None else "—"
        lo    = pct_fmt(r["max_low_pct"])  if r["max_low_pct"]  is not None else "—"
        reason_ko = REASON_KO.get(r["exit_reason"], r["exit_reason"])
        exit_str  = r["exit_ts"].strftime("%m/%d %H:%M") if r["exit_ts"] else "—"
        pnl_p     = pct_fmt(r["pnl_pct"])  if r["pnl_pct"]  is not None else "—"
        pnl_u     = f"${r['pnl_usd']:+.2f}" if r["pnl_usd"] is not None else "—"
        lines.append(
            f"| {i} | {r['ts'].strftime('%m/%d %H:%M')} | {r['chain'].upper()} "
            f"| {r['symbol']} | ${entry:.5g} "
            f"| {r1h} | {r4h} | {r24h} "
            f"| {hi} | {lo} "
            f"| {reason_ko} | {exit_str} | {pnl_p} | {pnl_u} |"
        )
    lines.append("")

    # ── 체인별 파라미터 참조
    lines.append("## 5. 시뮬레이션 파라미터\n")
    lines.append("| 체인 | 스탑 | 트레일 | 익절 | 최대보유 | 수수료 | 슬리피지 |")
    lines.append("|------|------|--------|------|----------|--------|----------|")
    for chain, p in CHAIN_PARAMS.items():
        lines.append(
            f"| {chain.upper()} | {p['stop_pct']*100:.0f}% | {p['trail_pct']*100:.0f}% "
            f"| +{p['target_pct']*100:.0f}% | {p['max_hours']}h "
            f"| {p['fee']*100:.2f}% | {p['slippage']*100:.2f}% |"
        )
    lines.append("")
    lines.append(f"> 포지션 크기: ${TOTAL_CAPITAL:,} × {POSITION_FRAC*100:.0f}% = ${TOTAL_CAPITAL*POSITION_FRAC:.0f}/건\n")

    return "\n".join(lines)


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    print("=== 사후 성과 분석 시작 ===\n")

    print("[1] 스냅샷 로딩...")
    snapshots = load_all_snapshots()
    for chain, by_sym in snapshots.items():
        total = sum(len(v) for v in by_sym.values())
        print(f"    {chain.upper()}: {len(by_sym)} 심볼 / {total:,} 레코드")

    print("\n[2] 고유 후보 추출...")
    candidates = parse_unique_candidates()
    print(f"    고유 후보: {len(candidates)}개")

    print("\n[3] 시뮬레이션 실행...")
    results = []
    for cand in candidates:
        chain  = cand["chain"]
        sym    = cand["symbol"]
        ts_list = snapshots.get(chain, {}).get(sym, [])
        series  = get_series_after(ts_list, cand["ts"])
        result  = simulate_trade(cand, series)
        results.append(result)

        reason = result["exit_reason"]
        pnl_p  = f"{result['pnl_pct']:+.2f}%" if result["pnl_pct"] is not None else "—"
        pnl_u  = f"${result['pnl_usd']:+.2f}" if result["pnl_usd"] is not None else "—"
        hi     = f"{result['max_high_pct']:+.2f}%" if result["max_high_pct"] is not None else "—"
        lo     = f"{result['max_low_pct']:+.2f}%"  if result["max_low_pct"]  is not None else "—"
        print(f"    [{chain.upper():6}] {sym:12} | {REASON_KO.get(reason, reason):8} | {pnl_p:8} | {pnl_u:9} | hi={hi} lo={lo}")

    print("\n[4] 집계...")
    valid   = [r for r in results if r["pnl_usd"] is not None]
    winners = [r for r in valid if r["pnl_usd"] > 0]
    losers  = [r for r in valid if r["pnl_usd"] <= 0]
    total_pnl = sum(r["pnl_usd"] for r in valid)
    gw = sum(r["pnl_usd"] for r in winners)
    gl = abs(sum(r["pnl_usd"] for r in losers))
    pf = gw / gl if gl > 0 else float("inf")

    print(f"\n    유효 시뮬: {len(valid)}건")
    print(f"    승/패: {len(winners)}/{len(losers)}  승률: {100*len(winners)/max(1,len(valid)):.1f}%")
    print(f"    누적 PnL: ${total_pnl:+.2f}")
    print(f"    Profit Factor: {pf:.2f}")

    print("\n[5] Markdown 리포트 저장...")
    report_path = REPORT_DIR / "analysis_summary.md"
    md = build_markdown(results)
    report_path.write_text(md, encoding="utf-8")
    print(f"    저장 완료: {report_path}")


if __name__ == "__main__":
    main()
