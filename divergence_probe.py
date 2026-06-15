# -*- coding: utf-8 -*-
"""divergence_probe.py - Module 2(재귀성/divergence) thesis 1차 검증.

가설: 인식(가격 모멘텀)이 실수요(매수압)를 앞서면 = 약세 괴리(forward 하락).
      실수요가 받쳐주면 = 강세(forward 상승).

검증 방법: base_snapshots 시계열로 (신호 @ t -> 같은 토큰의 forward 가격변화 @ t+H)
데이터셋을 만들어, 신호가 forward 수익과 상관 있는지 본다. positions(n작음)가 아니라
스냅샷 universe(수만) 라 표본이 크다.

핵심 테스트:
  1) 단일 피처 버킷: buy_ratio / pc_1h 가 forward 와 상관?
  2) 2D 괴리 사분면: pc_1h(가격 인식) x buy_ratio(실수요) -> "가격↑+매도우위"가 최악인가?

데이터 주의: buys/sells 는 935283e 이후(최근)만 채워짐 -> buys+sells>0 인 행만 사용.
holders 는 죽어있어 v1 에선 제외.

사용:
  python divergence_probe.py
  python divergence_probe.py --horizon 4   # 4h forward (기본 1h)
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SNAP = ROOT / "data" / "base_snapshots.jsonl"


def _ts(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except Exception:
        return None


def load_series():
    """토큰별 (ts, price, buys, sells, pc1h, liq, avg_tx) 시계열. buys+sells>0 만."""
    by_tok = defaultdict(list)
    n_raw = n_used = 0
    for line in SNAP.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        n_raw += 1
        b = r.get("buys_1h"); s = r.get("sells_1h")
        if not isinstance(b, (int, float)) or not isinstance(s, (int, float)) or (b + s) <= 0:
            continue
        t = _ts(r.get("timestamp"))
        p = r.get("price_usd")
        addr = str(r.get("contract_address") or "").lower()
        if t is None or not isinstance(p, (int, float)) or p <= 0 or not addr:
            continue
        by_tok[addr].append((
            t, float(p), float(b), float(s),
            float(r.get("price_change_1h_pct") or 0.0),
            float(r.get("liquidity_usd") or 0.0),
            float(r.get("avg_tx_size_usd") or 0.0),
        ))
        n_used += 1
    for v in by_tok.values():
        v.sort()
    return by_tok, n_raw, n_used


def build_dataset(by_tok, horizon_h):
    """각 점 i -> t+H 의 forward 수익. (signal dict, fwd_ret%) 리스트."""
    H = horizon_h * 3600
    tol = max(1800, H * 0.3)
    rows = []
    for series in by_tok.values():
        n = len(series)
        j = 0
        for i in range(n):
            t, p, b, s, pc1h, liq, avgtx = series[i]
            # forward: t+H 에 가장 가까운 점
            target = t + H
            while j < n and series[j][0] < target:
                j += 1
            cand = []
            if j < n:
                cand.append(series[j])
            if j - 1 > i:
                cand.append(series[j - 1])
            best = None; bg = None
            for c in cand:
                g = abs(c[0] - target)
                if g <= tol and (best is None or g < bg):
                    best, bg = c, g
            if best is None:
                continue
            fwd = (best[1] / p - 1) * 100
            if fwd > 500 or fwd < -95:
                continue
            buy_ratio = b / (b + s)
            rows.append({"buy_ratio": buy_ratio, "pc_1h": pc1h, "liq": liq,
                         "avg_tx": avgtx, "fwd": fwd})
    return rows


def _stat(xs):
    if not xs:
        return None
    return len(xs), st.median(xs), sum(x > 0 for x in xs) / len(xs) * 100, st.mean(xs)


def buckets(rows, key, edges, labels):
    print(f"\n[{key}] vs forward")
    print(f"  {'bucket':>14}{'N':>7}{'med_fwd%':>10}{'win%':>7}{'mean%':>8}")
    for i, lab in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        xs = [r["fwd"] for r in rows if lo <= r[key] < hi]
        s = _stat(xs)
        if s:
            print(f"  {lab:>14}{s[0]:>7}{s[1]:>+9.2f}{s[2]:>6.0f}%{s[3]:>+7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=1, help="forward 시간(h), 기본 1")
    args = ap.parse_args()

    by_tok, n_raw, n_used = load_series()
    rows = build_dataset(by_tok, args.horizon)
    print(f"스냅샷 {n_raw} 중 buys/sells 유효 {n_used}, 토큰 {len(by_tok)}개")
    print(f"forward({args.horizon}h) 데이터셋: {len(rows)}건")
    if len(rows) < 50:
        print("표본 부족 - buys/sells 데이터가 더 쌓여야 함")
        return
    base = _stat([r["fwd"] for r in rows])
    print(f"baseline forward: median {base[1]:+.2f}%  win {base[2]:.0f}%")

    # 1) 단일 피처
    buckets(rows, "buy_ratio", [0, 0.40, 0.50, 0.55, 0.60, 1.01],
            ["<.40", ".40-.50", ".50-.55", ".55-.60", ">=.60"])
    buckets(rows, "pc_1h", [-1e9, -2, 0, 5, 15, 1e9],
            ["<-2%", "-2~0", "0~5%", "5~15%", ">15%"])

    # 2) 2D 괴리 사분면: pc_1h x buy_ratio
    print("\n=== 2D 괴리: 가격인식(pc_1h) x 실수요(buy_ratio), 값=median forward% (win%) ===")
    pc_bins = [("price<=0", lambda v: v <= 0), ("0~5%", lambda v: 0 < v <= 5),
               ("5~15%", lambda v: 5 < v <= 15), (">15%", lambda v: v > 15)]
    br_bins = [("매도우위<.45", lambda v: v < 0.45), ("중립.45-.55", lambda v: 0.45 <= v < 0.55),
               ("매수우위>=.55", lambda v: v >= 0.55)]
    print(f"  {'':>12}" + "".join(f"{b[0]:>16}" for b in br_bins))
    for pn, pf in pc_bins:
        cells = []
        for bn, bf in br_bins:
            xs = [r["fwd"] for r in rows if pf(r["pc_1h"]) and bf(r["buy_ratio"])]
            if xs:
                cells.append(f"{st.median(xs):+.1f}({sum(x>0 for x in xs)/len(xs)*100:.0f}%,n{len(xs)})")
            else:
                cells.append("-")
        print(f"  {pn:>12}" + "".join(f"{c:>16}" for c in cells))
    print("\n가설 검증: '가격>15% + 매도우위<.45' 칸이 제일 음수면 = 약세 괴리 신호 실재.")
    print("           '매수우위>=.55' 열이 좌->우로 좋아지면 = 실수요가 forward 예측.")


if __name__ == "__main__":
    main()
