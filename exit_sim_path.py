# -*- coding: utf-8 -*-
"""exit_sim_path.py - 경로 복원 기반 출구 변형 백테스트 (정밀판).

기존 exit_sim.py 는 진입/고점/청산 3점만으로 트레일링을 '근사'했다. 이 정밀판은
체인별 snapshots(가격 시계열)에서 각 포지션의 전체 가격 경로를 복원해, 청산 엔진
(mc_position_manager.ExitSignalEngine.evaluate)을 1:1로 replay 한다.

검증: baseline(.env 실측 파라미터) replay 가 실제 realized_pnl_pct 를 재현하면
시뮬을 신뢰 → 출구 변형의 기대값 비교. 라이브 거동 0 (오프라인 전용).
가격 점 = 라이브 매니저가 본 동일 스냅샷이라 시뮬 해상도 = 봇 실제 해상도.
"""
import datetime as dt
import json
from collections import Counter, defaultdict
from statistics import median

DATA = "data"
EARLY_WIN_MIN = 30
EARLY_THR_PCT = -7.0
PATH_WINDOW_H = 26          # 진입 후 이만큼의 스냅샷까지 끌어와 더 긴 보유 변형도 평가

# .env 실측 baseline
BASELINE = {
    "base": dict(stop=-12.0, trail_act=10.0, trail_stop=10.0, tp=20.0, max_hold=8.0,  liq_crash=-20.0),
    "bsc":  dict(stop=-12.0, trail_act=10.0, trail_stop=10.0, tp=20.0, max_hold=24.0, liq_crash=-20.0),
}


def parse(t):
    try:
        x = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def load_snaps(chain):
    idx = defaultdict(list)
    with open(f"{DATA}/{chain}_snapshots.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ca = d.get("contract_address")
            ts = parse(d.get("timestamp"))
            pr = d.get("price_usd")
            if ca and ts and pr:
                liq = d.get("liquidity_usd")
                idx[ca].append((ts, float(pr), float(liq) if liq else None))
    for ca in idx:
        idx[ca].sort(key=lambda x: x[0])
    return idx


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
    out = []
    for d in seen.values():
        if not d.get("is_closed"):
            continue
        et = parse(d.get("entry_timestamp"))
        try:
            ep = float(d.get("entry_price"))
            rp = float(d.get("realized_pnl_pct"))
        except Exception:
            continue
        if not et or ep <= 0:
            continue
        out.append({
            "chain": d.get("chain"), "ca": d.get("contract_address"),
            "entry_ts": et, "entry_price": ep,
            "entry_liq": float(d.get("entry_liquidity") or 0) or None,
            "actual_pnl": rp, "actual_reason": d.get("exit_reason"),
        })
    return out


def simulate(pos, path, cfg, ptp=None):
    """path: [(ts,price,liq)] (진입 이후). 반환 (reason, final_pnl_pct)."""
    entry = pos["entry_price"]; entry_liq = pos["entry_liq"]; entry_ts = pos["entry_ts"]
    peak = entry; trailing = False; locked = False
    part_realized = 0.0; remaining = 1.0
    ptp_trigger, ptp_frac = (ptp if ptp else (None, None))

    def fin(pnl):
        return part_realized + remaining * pnl

    last_pnl = 0.0
    for ts, price, liq in path:
        pnl = (price / entry - 1) * 100
        last_pnl = pnl
        held_min = (ts - entry_ts).total_seconds() / 60.0
        hold_h = held_min / 60.0
        peak = max(peak, price)
        peak_pnl = (peak / entry - 1) * 100
        dd = (price / peak - 1) * 100
        liq_change = ((liq / entry_liq - 1) * 100) if (entry_liq and liq) else 0.0

        # 부분익절(신규 변형): trigger 첫 도달 시 일부 확정, 나머지 계속
        if ptp and remaining == 1.0 and pnl >= ptp_trigger:
            part_realized = ptp_frac * pnl
            remaining = 1.0 - ptp_frac

        if held_min <= EARLY_WIN_MIN and pnl <= EARLY_THR_PCT:
            return "EARLY_STOP", fin(pnl)
        overdue = hold_h >= cfg["max_hold"]
        if pnl <= cfg["stop"] and not overdue:
            return "STOP", fin(pnl)
        if peak_pnl >= cfg["trail_act"]:
            trailing = True
        if not locked and peak_pnl >= cfg["tp"]:
            locked = True
        if locked and pnl < cfg["tp"]:
            return "PROFIT_LOCK", fin(pnl)
        if trailing and dd <= -cfg["trail_stop"]:
            return "TRAILING", fin(pnl)
        if pnl >= cfg["tp"] and not locked:
            return "TAKE_PROFIT", fin(pnl)
        if hold_h >= cfg["max_hold"]:
            return "TIME", fin(pnl)
        if liq_change <= cfg["liq_crash"] and pnl <= -5.0:
            return "LIQ_CRASH", fin(pnl)
    return "END", fin(last_pnl)


def run(cfg_by_chain, positions, snaps, ptp=None):
    res = []
    for p in positions:
        ch = p["chain"]
        if ch not in cfg_by_chain or ch not in snaps:
            continue
        cfg = cfg_by_chain[ch]
        end = p["entry_ts"] + dt.timedelta(hours=PATH_WINDOW_H)
        path = [(ts, pr, lq) for ts, pr, lq in snaps[ch].get(p["ca"], [])
                if p["entry_ts"] < ts <= end]
        if len(path) < 2:
            continue
        reason, pnl = simulate(p, path, cfg, ptp)
        res.append((ch, reason, pnl, p["actual_pnl"], p["actual_reason"]))
    return res


def stats(pnls):
    if not pnls:
        return dict(n=0, win=0, exp=0, med=0)
    n = len(pnls); w = [x for x in pnls if x > 0]
    return dict(n=n, win=len(w) / n * 100, exp=sum(pnls) / n, med=median(pnls))


def fmt(label, pnls):
    s = stats(pnls)
    return f"{label:30} n={s['n']:4d} 승률={s['win']:4.0f}% 기대값={s['exp']:+6.2f}% 중앙값={s['med']:+6.2f}%"


def _match(sim_reason, actual_reason):
    m = {"STOP": "STOP_LOSS", "EARLY_STOP": "EARLY_STOP_LOSS", "TRAILING": "TRAILING_STOP",
         "PROFIT_LOCK": "PROFIT_LOCK_BREAK", "TIME": "TIME_EXIT", "LIQ_CRASH": "LIQUIDITY_CRASH",
         "TAKE_PROFIT": "TAKE_PROFIT", "END": "TIME_EXIT"}
    return m.get(sim_reason) == actual_reason


def _variants():
    def cfg(**over):
        out = {}
        for ch in ("base", "bsc"):
            c = dict(BASELINE[ch]); c.update(over); out[ch] = c
        return out
    V = [("baseline", cfg(), None)]
    for tp in (10, 12, 15, 25, 30):
        V.append((f"profit_lock={tp}", cfg(tp=tp), None))
    for ts_ in (6, 8, 12, 15):
        V.append((f"trailing={ts_}", cfg(trail_stop=ts_), None))
    V.append(("trail_act5_stop8", cfg(trail_act=5, trail_stop=8), None))
    for trig, frac in ((10, 0.5), (15, 0.5), (15, 0.33), (20, 0.5)):
        V.append((f"partialTP@{trig}x{frac}", cfg(), (trig, frac)))
    V.append(("ptp@12x0.5+trail8", cfg(trail_stop=8), (12, 0.5)))
    return V


def main():
    print("스냅샷 로딩...")
    snaps = {ch: load_snaps(ch) for ch in ("base", "bsc")}
    positions = load_positions()

    base_res = run(BASELINE, positions, snaps)
    print(f"재구성된 포지션 {len(base_res)}건\n")

    print("=== [검증] baseline 복제 vs 실제 ===")
    sim_pnl = [r[2] for r in base_res]
    print(fmt("  시뮬(복제)", sim_pnl))
    print(fmt("  실제(realized)", [r[3] for r in base_res]))
    diffs = [r[2] - r[3] for r in base_res]
    print(f"  평균 절대오차: {sum(abs(d) for d in diffs)/len(diffs):.2f}%p | 편향: {sum(diffs)/len(diffs):+.2f}%p")
    print(f"  출구사유 일치율: {sum(1 for r in base_res if _match(r[1], r[4]))/len(base_res)*100:.0f}%")
    print(f"  시뮬 사유분포: {dict(Counter(r[1] for r in base_res).most_common())}\n")

    print("=== [변형] 전체 기대값 (기대값 내림차순) ===")
    base_exp = stats(sim_pnl)["exp"]
    rows = []
    for name, cfg, ptp in _variants():
        r = run(cfg, positions, snaps, ptp)
        rows.append((name, stats([x[2] for x in r]),
                     stats([x[2] for x in r if x[0] == "base"]),
                     stats([x[2] for x in r if x[0] == "bsc"])))
    rows.sort(key=lambda x: -x[1]["exp"])
    for name, s, bs, cs in rows:
        tag = "  <== baseline" if name == "baseline" else f"  ({s['exp']-base_exp:+.2f}%p)"
        print(f"{name:26} 기대={s['exp']:+6.2f}% 중앙={s['med']:+6.2f}% 승률={s['win']:3.0f}%"
              f" | base={bs['exp']:+5.2f}% bsc={cs['exp']:+5.2f}%{tag}")


if __name__ == "__main__":
    main()
