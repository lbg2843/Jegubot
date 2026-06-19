# -*- coding: utf-8 -*-
"""entry_signal_outcome.py - 진입신호 ↔ 실현손익 분리력 랭킹.

entry_snapshots(진입시점 신호) ↔ positions(realized_pnl_pct) 를 contract+시간근접
조인 후, 각 신호가 승자/패자를 얼마나 잘 가르는지(상위절반 vs 하위절반 평균 PnL 격차)
를 랭킹. "어떤 진입필터를 추가하면 기대값이 오를까"의 근거. 오프라인 분석 전용.
"""
import datetime as dt
import json
from statistics import median, mean

DATA = "data"
NUMERIC = ["divergence_score", "buy_ratio", "pc_5m", "pc_15m", "pc_1h", "pc_6h", "pc_24h",
           "liquidity_usd", "mcap_usd", "vol_1h_usd", "txns_1h", "pool_age_hours",
           "final_score", "eth_4h_pct", "utc_hour"]
CATEG = ["entry_path", "divergence_label", "eth_4h_regime", "reflexivity_passed"]


def parse(t):
    try:
        x = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def load_positions():
    seen = {}
    with open(f"{DATA}/positions.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            seen[(d.get("contract_address"), d.get("entry_timestamp"))] = d
    by_ca = {}
    for d in seen.values():
        if not d.get("is_closed"):
            continue
        et = parse(d.get("entry_timestamp"))
        try:
            pnl = float(d.get("realized_pnl_pct"))
        except Exception:
            continue
        if not et:
            continue
        by_ca.setdefault(d.get("contract_address"), []).append((et, pnl))
    return by_ca


def load_entries():
    rows = []
    with open(f"{DATA}/entry_snapshots.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def join(entries, by_ca):
    """entry → 같은 contract 중 logged_at 에 가장 가까운(±2h) 진입의 pnl."""
    out = []
    for e in entries:
        ca = e.get("contract_address")
        t = parse(e.get("logged_at") or e.get("timestamp"))
        cands = by_ca.get(ca) or []
        if not cands or not t:
            continue
        best = min(cands, key=lambda x: abs((x[0] - t).total_seconds()))
        if abs((best[0] - t).total_seconds()) > 2 * 3600:
            continue
        out.append((e, best[1]))
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rank_numeric(joined):
    print("=== 수치신호 분리력 (상위절반 vs 하위절반 평균PnL 격차, |격차| 내림차순) ===")
    print(f"{'신호':18}{'N':>4}{'하위½평균':>11}{'상위½평균':>11}{'격차':>9}{'방향':>14}")
    out = []
    for sig in NUMERIC:
        pairs = [(_num(e.get(sig)), p) for e, p in joined if _num(e.get(sig)) is not None]
        if len(pairs) < 20:
            continue
        vals = sorted(x[0] for x in pairs)
        med = median(vals)
        lo = [p for v, p in pairs if v <= med]
        hi = [p for v, p in pairs if v > med]
        if len(lo) < 5 or len(hi) < 5:
            continue
        d = mean(hi) - mean(lo)
        out.append((abs(d), sig, len(pairs), mean(lo), mean(hi), d))
    out.sort(reverse=True)
    for ad, sig, n, ml, mh, d in out:
        direction = "값↑일수록 좋음" if d > 0 else "값↓일수록 좋음"
        print(f"{sig:18}{n:>4}{ml:>+10.2f}%{mh:>+10.2f}%{d:>+8.2f}%  {direction}")
    return out


def rank_categ(joined):
    print("\n=== 범주신호별 ===")
    for sig in CATEG:
        groups = {}
        for e, p in joined:
            k = e.get(sig)
            if k is None:
                continue
            groups.setdefault(str(k), []).append(p)
        groups = {k: v for k, v in groups.items() if len(v) >= 3}
        if not groups:
            continue
        print(f"[{sig}]")
        for k, v in sorted(groups.items(), key=lambda kv: -mean(kv[1])):
            w = sum(1 for x in v if x > 0) / len(v) * 100
            print(f"   {k:16} n={len(v):3d} 승률={w:3.0f}% 평균={mean(v):+6.2f}% 중앙={median(v):+6.2f}%")


def main():
    by_ca = load_positions()
    entries = load_entries()
    joined = join(entries, by_ca)
    print(f"진입 {len(entries)}건 중 손익 조인 {len(joined)}건\n")
    allp = [p for _, p in joined]
    print(f"조인 전체: 승률={sum(1 for x in allp if x>0)/len(allp)*100:.0f}% 평균={mean(allp):+.2f}% 중앙={median(allp):+.2f}%\n")
    rank_numeric(joined)
    rank_categ(joined)
    print("\n※ N 작은 신호(divergence/final_score)는 추세만. forward 데이터로 재확인 필요.")


if __name__ == "__main__":
    main()
