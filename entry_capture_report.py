# -*- coding: utf-8 -*-
"""entry_capture_report.py - 실행 갭 분해: 진입품질 vs 청산capture.

쟁점: 골든존 후보 forward median +54% 인데 실현 -1%. 이 갭이 (진입이 나빠서
토큰이 안 오른 건가) vs (올랐는데 청산이 못 챙긴 건가)?

측정: 청산된 포지션마다
  peak_pnl% = peak_price/entry_price - 1   # 진입 후 토큰이 얼마나 올랐나(진입품질)
  realized% = 실현 PnL                       # 실제로 챙긴 것
  capture   = realized / peak_pnl            # 상승분 중 챙긴 비율(청산 효율)

해석:
  * peak_pnl 분포가 0 근처에 몰림 -> 토큰이 진입 후 안 오름 = 진입 문제.
  * peak_pnl 은 큰데 realized 가 작음(capture 낮음) -> 러너를 못 챙김 = 청산 문제.

주의: score_outcomes(5/9~10)와 봇 진입(6월)은 시간이 안 겹쳐 직접 조인 불가.
그래서 score-snapshot-vs-entry-price 대신, 포지션 내부의 peak 로 진입품질을 본다.
(peak 은 봇이 보유 중 관측한 최고가라 '진입 후 실제 상승폭'의 보수적 하한.)

사용:
  python entry_capture_report.py --days 14
  python entry_capture_report.py --by-reason     # 청산 reason 교차
  python entry_capture_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
POSITIONS = ROOT / "data" / "positions.jsonl"


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load(since):
    last = {}
    for line in POSITIONS.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        last[(str(r.get("chain")).lower(), str(r.get("contract_address")).lower(),
              str(r.get("entry_timestamp")))] = r
    out = []
    for r in last.values():
        if not r.get("is_closed") or r.get("realized_pnl_pct") is None:
            continue
        xt = _parse(r.get("exit_timestamp"))
        if not xt or (since and xt < since):
            continue
        ep = float(r.get("entry_price") or 0)
        pk = float(r.get("peak_price") or 0)
        if ep <= 0:
            continue
        peak_pnl = (pk / ep - 1) * 100
        realized = float(r.get("realized_pnl_pct"))
        out.append({
            "sym": str(r.get("symbol"))[:10], "chain": str(r.get("chain")).lower(),
            "reason": str(r.get("exit_reason")), "peak_pnl": peak_pnl,
            "realized": realized,
            "capture": (realized / peak_pnl) if peak_pnl > 0.5 else None,
        })
    return out


def _stats(rows):
    if not rows:
        return None
    real = [r["realized"] for r in rows]
    caps = [r["capture"] for r in rows if r["capture"] is not None]
    return {
        "n": len(rows),
        "win": sum(x > 0 for x in real) / len(real) * 100,
        "mean_real": st.mean(real),
        "med_peak": st.median(r["peak_pnl"] for r in rows),
        "mean_cap": st.mean(caps) if caps else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--by-reason", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rows = load(since)
    if not rows:
        print("청산 포지션 없음")
        return

    real = [r["realized"] for r in rows]
    print(f"청산 {len(rows)}건  승률 {sum(x>0 for x in real)/len(real)*100:.0f}%  평균실현 {st.mean(real):+.2f}%")
    print(f"  진입 후 peak 중앙값 {st.median(r['peak_pnl'] for r in rows):+.2f}%  "
          f"(peak 은 '진입 후 토큰이 오른 폭'의 보수적 하한)\n")

    # peak_pnl 버킷별 진입품질 + capture
    EDGES = [-1, 0.5, 5, 15, 40, 1e9]
    LABS = ["~0(안오름)", "0.5-5%", "5-15%", "15-40%", ">40%"]
    print("[peak_pnl 버킷] 진입 후 상승폭별 - 몇 건이고 실제로 얼마 챙겼나")
    print(f"  {'peak대역':>12}{'N':>5}{'%':>6}{'win%':>7}{'mean실현':>10}{'capture':>9}")
    out = {}
    for i, lab in enumerate(LABS):
        lo, hi = EDGES[i], EDGES[i + 1]
        sub = [r for r in rows if lo <= r["peak_pnl"] < hi]
        s = _stats(sub)
        if not s:
            continue
        cap = f"{s['mean_cap']*100:.0f}%" if s["mean_cap"] is not None else "-"
        print(f"  {lab:>12}{s['n']:>5}{s['n']/len(rows)*100:>5.0f}%{s['win']:>6.0f}%"
              f"{s['mean_real']:>+9.2f}%{cap:>9}")
        out[lab] = s

    # 핵심 판정
    ran = [r for r in rows if r["peak_pnl"] >= 15]   # +15% 가 테이블에 올라온 적 있는 포지션
    never = [r for r in rows if r["peak_pnl"] < 5]
    print(f"\n[판정]")
    print(f"  진입 후 +15% 이상 찍은 적 있는 포지션: {len(ran)}건({len(ran)/len(rows)*100:.0f}%)"
          + (f", 그중 실현 평균 {st.mean(r['realized'] for r in ran):+.2f}%" if ran else ""))
    print(f"  진입 후 +5% 도 못 간 포지션: {len(never)}건({len(never)/len(rows)*100:.0f}%)"
          + (f", 실현 평균 {st.mean(r['realized'] for r in never):+.2f}%" if never else ""))
    if ran and st.mean(r["realized"] for r in ran) < 8:
        print("  -> 러너가 있었는데 못 챙김(capture 낮음) = 청산 문제 비중 큼.")
    if never and len(never) / len(rows) > 0.5:
        print("  -> 절반 이상이 진입 후 안 오름 = 진입 문제 비중 큼.")

    if args.by_reason:
        print("\n[청산 reason x peak] 각 청산이 얼마나 올랐다가 닫혔나")
        by = {}
        for r in rows:
            by.setdefault(r["reason"], []).append(r)
        print(f"  {'reason':>20}{'N':>5}{'med_peak':>10}{'mean실현':>10}")
        for k in sorted(by, key=lambda x: st.mean(rr["realized"] for rr in by[x])):
            v = by[k]
            print(f"  {k[:20]:>20}{len(v):>5}{st.median(rr['peak_pnl'] for rr in v):>+9.2f}%"
                  f"{st.mean(rr['realized'] for rr in v):>+9.2f}%")

    if args.json:
        Path(args.json).write_text(json.dumps({"n": len(rows), "buckets": out}, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
