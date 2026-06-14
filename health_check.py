# -*- coding: utf-8 -*-
"""health_check.py - Jegubot 주간 건강검진 (한 방).

매번 손으로 쿼리 짜지 말고 이거 하나로:
  [1] 동결 픽스 유지되나 (PASS/FAIL - 확정된 개선이라 깨지면 즉시 안다)
  [2] PnL 추세 (일별 + 누적, 노이즈 경고 포함)
  [3] 진입 품질 (러너율/덤퍼율 - 미해결 숙제 추적)
  [4] 청산 구조 (러너 vs 덤퍼 손익)

사용:
  python health_check.py                 # 최근 7일
  python health_check.py --days 14
  python health_check.py --since 2026-06-11
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
POSITIONS = ROOT / "data" / "positions.jsonl"
MAX_HOLD = {"base": 8.0, "bsc": 24.0, "solana": 2.0}
OVERDUE_MARGIN_H = 2.0   # cap+2h 초과만 '진짜 overdue'(8.1h 정상 종료는 제외)
CATASTROPHIC = -25.0


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load(since, now):
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
    closed, open_pos = [], []
    for r in last.values():
        chain = str(r.get("chain")).lower()
        et = _parse(r.get("entry_timestamp"))
        ep = float(r.get("entry_price") or 0)
        pk = float(r.get("peak_price") or 0)
        cap = MAX_HOLD.get(chain, 24.0)
        if r.get("is_closed") and r.get("realized_pnl_pct") is not None:
            xt = _parse(r.get("exit_timestamp"))
            if not xt or (since and xt < since):
                continue
            hold = (xt - et).total_seconds() / 3600 if et else 0
            closed.append({
                "chain": chain, "sym": str(r.get("symbol"))[:10], "xt": xt, "hold": hold,
                "reason": str(r.get("exit_reason")), "pnl": float(r.get("realized_pnl_pct")),
                "peak": (pk / ep - 1) * 100 if ep > 0 else 0.0,
                "overdue": hold > cap + OVERDUE_MARGIN_H,
            })
        elif not r.get("is_closed") and et:
            hold = (now - et).total_seconds() / 3600
            open_pos.append({"chain": chain, "sym": str(r.get("symbol"))[:10], "hold": hold,
                             "frozen_overdue": hold > cap + OVERDUE_MARGIN_H and abs(pk - ep) < 1e-15})
    return closed, open_pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    since = _parse(args.since + "T00:00:00") if args.since else (now - timedelta(days=args.days))
    closed, open_pos = load(since, now)
    label = args.since if args.since else f"최근 {args.days}일"
    print(f"=== Jegubot 건강검진 ({label}) ===")
    if not closed:
        print("청산 데이터 없음"); return
    pnls = [c["pnl"] for c in closed]
    print(f"청산 {len(closed)}건  승률 {sum(p>0 for p in pnls)/len(pnls)*100:.0f}%  "
          f"평균 {st.mean(pnls):+.2f}%  합 {sum(pnls):+.0f}%p\n")

    # [1] 동결 픽스
    overdue = [c for c in closed if c["overdue"]]
    catas = [c for c in closed if c["pnl"] < CATASTROPHIC]
    open_frozen = [o for o in open_pos if o["frozen_overdue"]]
    maxhold = max(c["hold"] for c in closed)
    fix_ok = not overdue and not catas and not open_frozen
    print(f"[1] 동결 픽스 유지        [{'OK' if fix_ok else 'FAIL'}]")
    print(f"    최장 청산 보유 {maxhold:.1f}h  | 진짜 overdue(cap+2h초과) {len(overdue)}건  "
          f"catastrophic(<{CATASTROPHIC:.0f}%) {len(catas)}건")
    print(f"    현재 오픈 중 동결의심 {len(open_frozen)}건"
          + ("" if fix_ok else "  <== 점검! 픽스 회귀 또는 재시작 누락 가능"))
    for c in overdue + catas:
        print(f"       ! {c['sym']} [{c['chain']}] hold {c['hold']:.1f}h pnl {c['pnl']:+.1f}% {c['reason']}")

    # [2] PnL 추세
    print(f"\n[2] PnL 추세              [WATCH]")
    byday = {}
    for c in closed:
        byday.setdefault(str(c["xt"])[:10], []).append(c["pnl"])
    parts = [f"{d[5:]} {sum(v):+.0f}({len(v)})" for d, v in sorted(byday.items())]
    print("    일별합(n): " + " | ".join(parts))
    print(f"    누적합 {sum(pnls):+.0f}%p"
          + (f"   (주의: n={len(closed)}, 일별 변동 큼 = 노이즈 영역, '추세'로만)" if len(closed) < 150 else ""))

    # [3] 진입 품질
    run = sum(c["peak"] >= 15 for c in closed) / len(closed) * 100
    dump = sum(c["peak"] < 5 for c in closed) / len(closed) * 100
    q = "WARN" if dump > 50 else "WATCH"
    print(f"\n[3] 진입 품질 (미해결)    [{q}]")
    print(f"    러너율(peak>=15%) {run:.0f}%   덤퍼율(peak<5%) {dump:.0f}%"
          + ("   (덤퍼>50% = 진입이 여전히 핵심 누수)" if dump > 50 else ""))

    # [4] 청산 구조
    print(f"\n[4] 청산 구조")
    by = {}
    for c in closed:
        by.setdefault(c["reason"], []).append(c["pnl"])
    for k in sorted(by, key=lambda x: sum(by[x])):
        v = by[k]
        print(f"    {k[:18]:>18} n{len(v):>3}  합{sum(v):>+6.0f}  평균{st.mean(v):>+6.1f}%")

    # 한줄 결론
    print("\n결론: ", end="")
    if not fix_ok:
        print("동결 픽스 회귀 의심 - [1] 먼저 점검.")
    elif sum(pnls) > 0:
        print(f"동결 픽스 유지 + 누적 +{sum(pnls):.0f}%p. 단 n={len(closed)}는 노이즈, 1주+ 더 보고 판단. 진입(덤퍼 {dump:.0f}%)은 숙제.")
    else:
        print(f"동결 픽스는 유지되나 누적 {sum(pnls):.0f}%p. 진입 덤퍼율 {dump:.0f}%가 핵심 누수.")


if __name__ == "__main__":
    main()
