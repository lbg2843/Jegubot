# -*- coding: utf-8 -*-
"""phantom_report.py — 게이트가 떨어낸 진입의 '가상 실현손익'을 reason 별로 집계.

"막은 게 사실 좋았으면 게이트를 손봐야 한다"를 검증한다. reason_tag(divergence/
falling_knife/eth_down/already_pumped) 별로 팬텀 PnL 을 보고, +면 그 게이트 규칙이
위너를 버리는 중일 수 있다는 신호. 라이브 실제 매매(positions.jsonl)와 나란히 비교.

사용:
  python phantom_report.py            # 전체
  python phantom_report.py --days 7   # 최근 7일(청산 기준)
  python phantom_report.py --open     # 아직 안 닫힌 팬텀(미실현) 같이
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POS = ROOT / "data" / "phantom_positions.jsonl"
STATE = ROOT / "data" / "phantom_state.json"


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_closed(since):
    out = []
    if not POS.exists():
        return out
    for line in POS.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if since:
            xt = _parse(r.get("exit_timestamp"))
            if not xt or xt < since:
                continue
        out.append(r)
    return out


def _wr(xs):
    return sum(p > 0 for p in xs) / len(xs) * 100 if xs else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--open", action="store_true", help="미청산 팬텀(미실현) 요약")
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rows = load_closed(since)
    if not rows:
        print("팬텀 청산 기록 없음 (아직 안 쌓였거나 트래커 미가동).")
        return

    pnls = [r["realized_pnl_pct"] for r in rows]
    print(f"=== 팬텀(게이트가 막은 진입을 샀다 치고) 청산 {len(rows)}건 "
          f"{'(최근 '+str(args.days)+'일)' if since else '(전체)'} ===")
    print(f"전체: 승률 {_wr(pnls):.0f}%  평균 {st.mean(pnls):+.2f}%  중앙 {st.median(pnls):+.2f}%  "
          f"합 ${sum(r.get('realized_pnl_usd', 0) for r in rows):+.2f} (명목 $100/건)\n")

    by = {}
    for r in rows:
        by.setdefault(r.get("reason_tag", "?"), []).append(r)
    h = f"{'reason(막은 규칙)':>16}{'n':>5}{'승률':>6}{'평균%':>8}{'중앙%':>8}{'합$':>9}{'best':>7}{'worst':>7}"
    print(h); print("-" * len(h))
    for tag in sorted(by, key=lambda t: -st.mean([x["realized_pnl_pct"] for x in by[t]])):
        rs = by[tag]; ps = [x["realized_pnl_pct"] for x in rs]
        flag = "  <== 막은 게 +였음(게이트 재검토)" if st.mean(ps) > 0 and len(ps) >= 10 else ""
        print(f"{tag:>16}{len(rs):>5}{_wr(ps):>5.0f}%{st.mean(ps):>+8.2f}{st.median(ps):>+8.2f}"
              f"{sum(x.get('realized_pnl_usd',0) for x in rs):>+9.2f}{max(ps):>+7.1f}{min(ps):>+7.1f}{flag}")

    # 청산 reason 분해(어떤 경로로 끝났나)
    print("\n[exit_reason 분해]")
    er = Counter(r.get("exit_reason", "?") for r in rows)
    for k, v in er.most_common():
        sub = [r["realized_pnl_pct"] for r in rows if r.get("exit_reason") == k]
        print(f"  {k:>18}: n{v:<4} 평균{st.mean(sub):+.1f}%")

    print("\n해석: reason 별 평균이 +이고 표본이 충분하면 그 게이트 규칙이 위너를 버리는 중일 수 있음.")
    print("      단 팬텀은 슬리피지/체결 미반영 → 라이브보다 낙관 편향. 부호와 상대크기 위주로 판단.")

    if args.open and STATE.exists():
        try:
            d = json.loads(STATE.read_text(encoding="utf-8"))
            op = d.get("open", [])
            if op:
                print(f"\n[미청산 팬텀 {len(op)}건 (미실현)]")
                ob = Counter(o.get("reason_tag", "?") for o in op)
                for k, v in ob.most_common():
                    print(f"  {k:>16}: {v}건 추적 중")
        except Exception:
            pass


if __name__ == "__main__":
    main()
