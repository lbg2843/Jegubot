# -*- coding: utf-8 -*-
"""score_threshold_report.py - final_score 게이트 임계 튜닝.

score_outcomes.jsonl(전 후보의 score -> horizon별 forward 가격변화)로
"임계를 어디에 걸면 빈도 vs 엣지가 어떻게 되나"를 곡선으로 뽑는다.
SCORE_SHADOW_MODE=true 라 현재 score 는 진입을 게이팅하지 않음 - 이걸 라이브로
켤지/어디에 걸지 결정하는 근거.

핵심 지표는 median(팻테일 왜곡 회피) + win% + cand/day(빈도).
임계는 통과 후보가 너무 적으면(거래 증발) 의미 없으므로 빈도를 같이 본다.

사용:
  python score_threshold_report.py                  # 전 horizon
  python score_threshold_report.py --horizon 24h
  python score_threshold_report.py --reflexivity     # reflexivity_passed=True 결합
  python score_threshold_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SCORE_OUTCOMES = ROOT / "data" / "score_outcomes.jsonl"
THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load(reflexivity_only):
    by_h = defaultdict(list)  # horizon -> [(score, pct, ts)]
    tspan = [None, None]
    for line in SCORE_OUTCOMES.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("fetch_status") != "ok":
            continue
        if reflexivity_only and not r.get("reflexivity_passed"):
            continue
        s, p = r.get("final_score"), r.get("price_change_pct")
        if not isinstance(s, (int, float)) or not isinstance(p, (int, float)):
            continue
        if p > 500 or p < -95:  # 스크랩 오류성 극단 컷
            continue
        d = _parse(r.get("timestamp"))
        if d:
            tspan[0] = d if tspan[0] is None or d < tspan[0] else tspan[0]
            tspan[1] = d if tspan[1] is None or d > tspan[1] else tspan[1]
        by_h[r.get("horizon", "?")].append((float(s), float(p)))
    span_days = ((tspan[1] - tspan[0]).total_seconds() / 86400) if tspan[0] and tspan[1] else None
    return by_h, span_days


def _winrate(xs):
    return sum(p > 0 for p in xs) / len(xs) * 100 if xs else 0.0


def sweep(data, span_days):
    total = len(data)
    base_med = st.median([p for _, p in data]) if data else 0.0
    base_win = _winrate([p for _, p in data])
    print(f"  baseline(전체) N={total}  median={base_med:+.2f}%  win={base_win:.0f}%")
    print(f"  {'thr>=':>7}{'N':>7}{'%univ':>7}{'cand/day':>10}{'median%':>9}{'win%':>7}{'mean%':>8}{'p25':>8}{'p75':>8}")
    res = {}
    for t in THRESHOLDS:
        sub = [p for s, p in data if s >= t]
        if not sub:
            print(f"  {t:>7.2f}{0:>7}")
            continue
        sub_sorted = sorted(sub)
        p25 = sub_sorted[int(0.25 * len(sub))]
        p75 = sub_sorted[int(0.75 * len(sub))]
        cpd = (len(sub) / span_days) if span_days else float("nan")
        print(f"  {t:>7.2f}{len(sub):>7}{len(sub)/total*100:>6.1f}%{cpd:>10.1f}"
              f"{st.median(sub):>+9.2f}{_winrate(sub):>6.0f}%{st.mean(sub):>+8.1f}{p25:>+8.1f}{p75:>+8.1f}")
        res[f"{t:.2f}"] = {"n": len(sub), "pct_universe": len(sub) / total * 100,
                           "cand_per_day": cpd, "median": st.median(sub),
                           "winrate": _winrate(sub), "mean": st.mean(sub)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default=None, help="1h | 4h | 24h (기본: 전부)")
    ap.add_argument("--reflexivity", action="store_true", help="reflexivity_passed=True 만")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    by_h, span_days = load(args.reflexivity)
    if not by_h:
        print("score_outcomes 데이터 없음")
        return
    print(f"기간 {span_days:.1f}일" if span_days else "기간 미상", end="")
    print(f"  {'(reflexivity_passed=True 만)' if args.reflexivity else ''}")
    if span_days and span_days < 2:
        print("  [경고] timestamp 범위가 좁음(한 창구) → cand/day 는 재채점 카운트라 무의미. N/%univ/median/win% 만 신뢰.")
    print("팻테일 때문에 mean 보다 median/win% 를 본다.\n")

    horizons = [args.horizon] if args.horizon else sorted(by_h)
    out = {}
    for h in horizons:
        data = by_h.get(h)
        if not data:
            print(f"[horizon {h}] 데이터 없음"); continue
        print(f"=== horizon {h} ===")
        out[h] = sweep(data, span_days)
        print()

    print("해석: median 이 baseline 대비 크게 오르면서 cand/day 가 0 으로 죽지 않는 임계가 sweet spot.")
    print("      통과율(%univ)이 2~4%면 진입이 그만큼 줄어듦 - '양보다 질' 트레이드오프를 보고 결정.")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"span_days": span_days, "reflexivity_only": args.reflexivity, "horizons": out},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
