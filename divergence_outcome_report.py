# -*- coding: utf-8 -*-
"""divergence_outcome_report.py - divergence 신호가 봇 '실거래'에서 먹히나 검증.

divergence_probe 는 candidate 의 forward 가격(이상화)을 봤다. 이건 한 단계 강한 검증:
진입에 태깅된 divergence_label/score(entry_snapshots) 를 실제 청산 PnL(positions)과
조인해, flat_buy 가 실제로 벌고 extended_sell 이 까먹는지 realized 로 본다.

주의: divergence 태깅은 2026-06-15 재시작 이후 진입부터 붙음 -> 그 전 진입은
'untagged'. buys/sells 없는 체인(bsc 등)도 unknown. 표본은 누적될수록 신뢰↑.

사용:
  python divergence_outcome_report.py
  python divergence_outcome_report.py --days 7
  python divergence_outcome_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
ENTRY_LOG = ROOT / "data" / "entry_snapshots.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"
TOL = 1800
# probe 사분면 순서 (best -> worst 대략)
LABEL_ORDER = ["flat_buy", "flat_neutral", "flat_sell", "mid_buy", "mid_neutral",
               "mid_sell", "extended_buy", "extended_neutral", "extended_sell"]


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_entries(since):
    out = []
    if not ENTRY_LOG.exists():
        return out
    for line in ENTRY_LOG.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        d = _parse(r.get("logged_at"))
        if not d or (since and d < since):
            continue
        c = (r.get("contract_address") or "").lower()
        if not c:
            continue
        out.append({
            "ts": d.timestamp(), "chain": (r.get("chain") or "").lower(), "contract": c,
            "symbol": r.get("symbol"),
            "label": r.get("divergence_label"),
            "score": r.get("divergence_score"),
            "buy_ratio": r.get("buy_ratio"),
        })
    return out


def load_closed():
    if not POSITIONS.exists():
        return []
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
        et = _parse(r.get("entry_timestamp"))
        if not et:
            continue
        ep = float(r.get("entry_price") or 0)
        pk = float(r.get("peak_price") or 0)
        out.append({"chain": str(r.get("chain")).lower(),
                    "contract": str(r.get("contract_address")).lower(),
                    "ets": et.timestamp(), "pnl": float(r.get("realized_pnl_pct")),
                    "peak": (pk / ep - 1) * 100 if ep > 0 else 0.0, "_used": False})
    return out


def join(entries, closed):
    bt = defaultdict(list)
    for c in closed:
        bt[(c["chain"], c["contract"])].append(c)
    rows = []
    for e in sorted(entries, key=lambda x: x["ts"]):
        best = bg = None
        for c in bt.get((e["chain"], e["contract"]), []):
            if c["_used"]:
                continue
            g = abs(c["ets"] - e["ts"])
            if g > TOL:
                continue
            if best is None or g < bg:
                best, bg = c, g
        if best is None:
            continue
        best["_used"] = True
        rows.append({**e, "pnl": best["pnl"], "peak": best["peak"]})
    return rows


def _row(pnls, peaks=None):
    n = len(pnls)
    out = (n, sum(p > 0 for p in pnls) / n * 100, st.median(pnls), st.mean(pnls), sum(pnls))
    if peaks is not None:
        out = out + (sum(p >= 15 for p in peaks) / n * 100,)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rows = join(load_entries(since), load_closed())
    if not rows:
        print("조인된 거래 없음")
        return
    tagged = [r for r in rows if r.get("label") and r["label"] != "unknown"]
    print(f"조인 {len(rows)}건 중 divergence 태깅됨 {len(tagged)}건 (재시작 2026-06-15 이후 진입만)")
    if not tagged:
        print("아직 태깅된 거래 없음 - 재시작 후 base 진입이 쌓여야 함. (지금은 관찰만)")
        return
    allp = [r["pnl"] for r in tagged]
    print(f"태깅분 전체: 승률 {sum(p>0 for p in allp)/len(allp)*100:.0f}%  평균 {st.mean(allp):+.2f}%  합 {sum(allp):+.0f}%p\n")

    # 라벨별
    by = defaultdict(list)
    for r in tagged:
        by[r["label"]].append(r)
    print("[divergence_label 별 실현 성과]")
    print(f"  {'label':>18}{'N':>4}{'win%':>7}{'median':>9}{'mean':>8}{'run15%':>8}{'sum%p':>8}")
    out = {}
    order = [l for l in LABEL_ORDER if l in by] + [l for l in by if l not in LABEL_ORDER]
    for lab in order:
        g = by[lab]
        n, win, med, mean, s, run = _row([x["pnl"] for x in g], [x["peak"] for x in g])
        flag = "  <== 적음" if n < args.min_n else ""
        print(f"  {lab:>18}{n:>4}{win:>6.0f}%{med:>+8.2f}{mean:>+7.2f}{run:>7.0f}%{s:>+7.0f}{flag}")
        out[lab] = {"n": n, "win": win, "median": med, "mean": mean, "runner15": run, "sum": s}

    # divergence_score 버킷
    scored = [r for r in tagged if isinstance(r.get("score"), (int, float))]
    if scored:
        print("\n[divergence_score 버킷] (높을수록 강세 가설)")
        print(f"  {'score':>12}{'N':>4}{'win%':>7}{'median':>9}{'mean':>8}")
        edges = [-99, -0.5, -0.1, 0.1, 99]
        labs = ["<-0.5", "-0.5~-0.1", "-0.1~0.1", ">=0.1"]
        for i, lab in enumerate(labs):
            xs = [r["pnl"] for r in scored if edges[i] <= r["score"] < edges[i + 1]]
            if xs:
                n, win, med, mean, s = _row(xs)
                print(f"  {lab:>12}{n:>4}{win:>6.0f}%{med:>+8.2f}{mean:>+7.2f}")

    # 가설 판정
    fb = [x["pnl"] for x in by.get("flat_buy", [])]
    es = [x["pnl"] for x in by.get("extended_sell", [])]
    print("\n[가설 판정] flat_buy(강세) vs extended_sell(약세)")
    if fb and es:
        print(f"  flat_buy 평균 {st.mean(fb):+.2f}% (n{len(fb)})  vs  extended_sell 평균 {st.mean(es):+.2f}% (n{len(es)})")
        if st.mean(fb) > st.mean(es):
            tag = " <== 가설대로(강세>약세)"
            if min(len(fb), len(es)) < args.min_n:
                tag += " 단 표본 작음, 보류"
            print("  " + tag.strip())
        else:
            print("  가설과 반대 - divergence 신호가 realized 에선 안 먹힘(또는 표본 부족)")
    else:
        print(f"  아직 부족: flat_buy n{len(fb)}, extended_sell n{len(es)} - 더 쌓여야 판정")

    if args.json:
        Path(args.json).write_text(json.dumps({"tagged": len(tagged), "labels": out},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
