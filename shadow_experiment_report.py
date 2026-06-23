# -*- coding: utf-8 -*-
"""shadow_experiment_report.py — shadow 실험 arm별 "막았으면 더 나았나" 판정 (v2).

v2 변경점:
  * [버그픽스] d_mean 베이스라인을 전체평균이 아니라 "해당 arm의 평가가능 집합 평균"으로.
    (결측 패턴이 arm마다 달라서 전체평균 비교는 모집단 오염이었음 — e/f 등 재검증 필요)
  * utc_hour 미태깅이어도 logged_at timestamp 에서 직접 계산 (k arm 커버리지 100%).
    자정 걸치는 시간대 범위(예: 22-04)도 지원.
  * blocked/allowed 각각 승률(win%) 컬럼 추가.
  * --overlap   : arm 간 막은 거래 겹침 매트릭스 (교집합 / Jaccard) — j·k 가 같은 현상인지 판정.
  * --bootstrap : d_mean 95% 부트스트랩 CI — CI 가 0 을 포함하면 '판정 보류'.
  * --by-week   : ISO 주 단위 d_mean 부호 일관성 — 한 주 몰빵 신호 걸러내기.
  * --exit-reasons : 청산 reason 별 PnL 분해 (진입 필터와 별개로 손익 비대칭 구조 확인).

사용:
  python shadow_experiment_report.py --days 7 --bootstrap 2000 --overlap --by-week
  python shadow_experiment_report.py --exit-reasons
  python shadow_experiment_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow_rules import SHADOW_RULES

ROOT = Path(__file__).parent
ENTRY_LOG = ROOT / "data" / "entry_snapshots.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"
MATCH_TOLERANCE_SEC = 1800

EXIT_REASON_KEYS = ("exit_reason", "close_reason", "exit_label", "reason")


def _parse(x):
    if not x:
        return None
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _coerce_num(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pc1h(e):
    for k in ("pc_1h", "price_change_1h_pct", "price_change_1h"):
        v = e.get(k)
        if isinstance(v, (int, float)):
            return float(v)
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
            "symbol": r.get("symbol"), "entry_path": r.get("entry_path"),
            "pc_1h": _pc1h(r), "utc_hour": r.get("utc_hour"),
            "eth_4h_regime": r.get("eth_4h_regime"),
            "final_score": r.get("final_score"),
            "eth_24h_pct": r.get("eth_24h_pct"),
            "divergence_score": r.get("divergence_score"),
            "vol_1h_usd": r.get("vol_1h_usd"),
            "pool_age_hours": r.get("pool_age_hours"),
            "risk_level": r.get("risk_level"),
            "holders": _coerce_num(r.get("holders")),  # 구데이터는 문자열 → 숫자화
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
        reason = "?"
        for k in EXIT_REASON_KEYS:
            v = r.get(k)
            if v:
                reason = str(v)
                break
        out.append({"chain": str(r.get("chain")).lower(),
                    "contract": str(r.get("contract_address")).lower(),
                    "ets": et.timestamp(), "pnl": float(r.get("realized_pnl_pct")),
                    "reason": reason, "_used": False})
    return out


def join(entries, closed):
    bt = {}
    for c in closed:
        bt.setdefault((c["chain"], c["contract"]), []).append(c)
    rows = []
    for e in sorted(entries, key=lambda x: x["ts"]):
        best = bg = None
        for c in bt.get((e["chain"], e["contract"]), []):
            if c["_used"]:
                continue
            g = abs(c["ets"] - e["ts"])
            if g > MATCH_TOLERANCE_SEC:
                continue
            if best is None or g < bg:
                best, bg = c, g
        if best is None:
            continue
        best["_used"] = True
        rows.append({**e, "pnl": best["pnl"], "reason": best["reason"]})
    return rows


def utc_hour_of(e):
    h = e.get("utc_hour")
    if isinstance(h, int):
        return h
    return datetime.fromtimestamp(e["ts"], tz=timezone.utc).hour


def arm_blocks(overrides, e):
    """저장된 진입 피처로 arm 의 would_block 재계산. (blocked: bool, evaluable: bool)"""
    if "sweet_spot_max_15m_drop_pct" in overrides:
        return False, False  # gate-param override → 게이트 재실행 필요, skip

    if "enable_paths" in overrides:
        return e.get("entry_path") not in overrides["enable_paths"], True

    if "block_eth_regimes" in overrides:
        reg = e.get("eth_4h_regime")
        if not reg or reg == "unknown":
            return False, False
        bp = overrides.get("block_paths")
        path_match = bp is None or e.get("entry_path") in bp
        return (reg in overrides["block_eth_regimes"] and path_match), True

    pc = e.get("pc_1h")
    if "block_if_pc1h_above" in overrides:
        if pc is None:
            return False, False
        return pc > overrides["block_if_pc1h_above"], True
    if "allow_pc1h_range" in overrides:
        if pc is None:
            return False, False
        lo, hi = overrides["allow_pc1h_range"]
        return not (lo <= pc <= hi), True

    if "allow_utc_hours" in overrides:
        h = utc_hour_of(e)
        s, en = overrides["allow_utc_hours"]
        if s <= en:
            inside = s <= h < en
        else:  # 자정 걸침 (예: 22-04)
            inside = h >= s or h < en
        return (not inside), True

    if "block_if_score_below" in overrides:
        sc = e.get("final_score")
        chains = overrides.get("score_chains")
        if not isinstance(sc, (int, float)) or (chains is not None and e.get("chain") not in chains):
            return False, False  # 점수 미태깅/체인 불일치 → 평가 불가
        return sc < overrides["block_if_score_below"], True

    if "block_if_eth24h_below" in overrides:
        v = e.get("eth_24h_pct")
        chains = overrides.get("eth_chains")
        if not isinstance(v, (int, float)) or (chains is not None and e.get("chain") not in chains):
            return False, False  # eth_24h 미태깅/체인 불일치 → 평가 불가
        return v < overrides["block_if_eth24h_below"], True

    if "block_if_divergence_below" in overrides:
        v = e.get("divergence_score")
        if not isinstance(v, (int, float)):
            return False, False  # divergence 미태깅 → 평가 불가
        return v < overrides["block_if_divergence_below"], True

    if "block_if_risk_above" in overrides:
        rl = e.get("risk_level")
        if not isinstance(rl, (int, float)):
            return False, False  # risk_level 미태깅 → 평가 불가
        return rl > overrides["block_if_risk_above"], True

    if "block_if_holders_below" in overrides:
        h = e.get("holders")
        if not isinstance(h, (int, float)):
            return False, False  # holders 미태깅 → 평가 불가
        return h < overrides["block_if_holders_below"], True

    # vol_1h / pool_age (단일 또는 결합=OR). 하나라도 피처 있으면 평가 가능.
    if "block_if_vol1h_above" in overrides or "block_if_pool_age_above" in overrides:
        blocked = False
        evaluable = False
        if "block_if_vol1h_above" in overrides:
            v = e.get("vol_1h_usd")
            if isinstance(v, (int, float)):
                evaluable = True
                blocked = blocked or v > overrides["block_if_vol1h_above"]
        if "block_if_pool_age_above" in overrides:
            a = e.get("pool_age_hours")
            if isinstance(a, (int, float)):
                evaluable = True
                blocked = blocked or a > overrides["block_if_pool_age_above"]
        return blocked, evaluable

    return False, False


def _winrate(xs):
    return (sum(p > 0 for p in xs) / len(xs) * 100) if xs else None


def _fmt(v, spec="+.2f", none="-"):
    return format(v, spec) if v is not None else none


def bootstrap_ci(pairs, n_iter, seed=42):
    """pairs: [(pnl, blocked)] 평가가능 집합. d_mean = mean(allowed) - mean(all) 의 95% CI."""
    rng = random.Random(seed)
    n = len(pairs)
    deltas = []
    for _ in range(n_iter):
        samp = [pairs[rng.randrange(n)] for _ in range(n)]
        allowed = [p for p, b in samp if not b]
        if not allowed:
            continue
        deltas.append(st.mean(allowed) - st.mean([p for p, _ in samp]))
    if len(deltas) < max(100, n_iter // 4):
        return None
    deltas.sort()
    return deltas[int(0.025 * len(deltas))], deltas[int(0.975 * len(deltas))]


def week_key(ts):
    y, w, _ = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=10, help="판정 플래그 최소 blocked 표본")
    ap.add_argument("--bootstrap", type=int, default=0, metavar="N",
                    help="d_mean 95%% 부트스트랩 CI (반복 횟수, 예: 2000)")
    ap.add_argument("--overlap", action="store_true", help="arm 간 막은 거래 겹침 매트릭스")
    ap.add_argument("--by-week", action="store_true", help="주간 d_mean 부호 일관성")
    ap.add_argument("--exit-reasons", action="store_true", help="청산 reason 별 PnL 분해")
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rows = join(load_entries(since), load_closed())
    if not rows:
        print("조인된 거래 없음")
        return
    allp = [r["pnl"] for r in rows]
    print(f"조인된 거래: {len(rows)}건  전체 승률 {_winrate(allp):.0f}%  평균 {st.mean(allp):+.2f}%\n")

    # ---- arm 평가 (행 인덱스별 blocked 플래그 보존 → overlap/by-week 재사용) ----
    evals = {}  # name -> {"idx": [row_idx...], "blocked": [bool...]}
    for name, ov in SHADOW_RULES.items():
        idxs, blks = [], []
        for i, r in enumerate(rows):
            blk, evaluable = arm_blocks(ov, r)
            if not evaluable:
                continue
            idxs.append(i)
            blks.append(blk)
        evals[name] = {"idx": idxs, "blocked": blks}

    hdr = (f"{'arm':>26}{'eval':>6}{'blk':>5}{'blkPnL':>8}{'blkWin':>7}"
           f"{'alwN':>6}{'alwPnL':>8}{'alwWin':>7}{'d_mean':>8}{'removed%':>10}")
    if args.bootstrap:
        hdr += f"{'95% CI':>18}"
    print(hdr)
    print("-" * len(hdr))

    out = {}
    for name, ev in evals.items():
        if not ev["idx"]:
            print(f"{name:>26}{'skip':>6}{'-':>5}  (gate-param override 또는 피처 미태깅)")
            continue
        pnls = [rows[i]["pnl"] for i in ev["idx"]]
        blocked = [p for p, b in zip(pnls, ev["blocked"]) if b]
        allowed = [p for p, b in zip(pnls, ev["blocked"]) if not b]
        base_mean = st.mean(pnls)  # ★ v2: 베이스라인 = 이 arm의 평가가능 집합 평균
        bmean = st.mean(blocked) if blocked else None
        amean = st.mean(allowed) if allowed else None
        dmean = (amean - base_mean) if amean is not None else None
        removed = sum(blocked)

        ci = None
        if args.bootstrap and blocked and allowed:
            ci = bootstrap_ci(list(zip(pnls, ev["blocked"])), args.bootstrap)

        flag = ""
        if blocked and len(blocked) >= args.min_n and bmean is not None and amean is not None:
            if bmean < amean and bmean < 0:
                flag = "  <== 도움"
                if ci is not None and ci[0] <= 0 <= ci[1]:
                    flag = "  <== 도움(CI가 0 포함 - 보류)"

        line = (f"{name:>26}{len(ev['idx']):>6}{len(blocked):>5}"
                f"{_fmt(bmean):>8}{_fmt(_winrate(blocked), '.0f'):>6}%"
                f"{len(allowed):>6}{_fmt(amean):>8}{_fmt(_winrate(allowed), '.0f'):>6}%"
                f"{_fmt(dmean):>8}{removed:>+10.1f}")
        if args.bootstrap:
            line += (f"  [{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else f"{'-':>18}")
        print(line + flag)

        out[name] = {"n_eval": len(ev["idx"]), "n_blocked": len(blocked),
                     "blocked_mean": bmean, "blocked_winrate": _winrate(blocked),
                     "allowed_mean": amean, "allowed_winrate": _winrate(allowed),
                     "base_mean": base_mean, "delta_mean": dmean,
                     "removed_pnl_pct_sum": removed,
                     "ci95": list(ci) if ci else None}

    print("\n해석: d_mean = (남는 거래 평균) - (그 arm 평가가능 집합 평균). v2부터 베이스라인이 arm별이라")
    print("      결측 패턴이 다른 arm 간에도 공정 비교. CI 가 0 을 포함하면 표본 부족 → 판정 보류.")

    # ---- arm 간 겹침 매트릭스 ----
    if args.overlap:
        names = [n for n, ev in evals.items() if any(ev["blocked"])]
        bsets = {n: {i for i, b in zip(evals[n]["idx"], evals[n]["blocked"]) if b} for n in names}
        print("\n[overlap] 막은 거래 겹침 (교집합 / Jaccard) - 값이 크면 사실상 같은 현상을 막는 중")
        w = max((len(n) for n in names), default=4)
        print(" " * (w + 2) + "".join(f"{n[:10]:>12}" for n in names))
        for a in names:
            cells = []
            for b in names:
                inter = len(bsets[a] & bsets[b])
                union = len(bsets[a] | bsets[b]) or 1
                cells.append(f"{inter:>4}/{inter/union:>5.2f}  ")
            print(f"{a:>{w}}  " + "".join(f"{c:>12}" for c in cells))

    # ---- 주간 일관성 ----
    if args.by_week:
        print("\n[by-week] 주간 d_mean 부호 일관성 (블록이 발생한 arm 만)")
        for name, ev in evals.items():
            if not any(ev["blocked"]):
                continue
            wk = {}
            for i, b in zip(ev["idx"], ev["blocked"]):
                wk.setdefault(week_key(rows[i]["ts"]), []).append((rows[i]["pnl"], b))
            parts, signs = [], []
            for k in sorted(wk):
                pairs = wk[k]
                alw = [p for p, b in pairs if not b]
                nblk = sum(b for _, b in pairs)
                if not alw or nblk == 0:
                    parts.append(f"{k}: n{len(pairs)} blk{nblk} d=NA")
                    continue
                d = st.mean(alw) - st.mean([p for p, _ in pairs])
                signs.append(d > 0)
                parts.append(f"{k}: n{len(pairs)} blk{nblk} d{d:+.2f}")
            cons = ("일관(+)" if signs and all(signs)
                    else "일관(-)" if signs and not any(signs)
                    else "혼재" if signs else "판정불가")
            print(f"  {name:>26} [{cons}]  " + " | ".join(parts))

    # ---- 청산 reason 분해 ----
    if args.exit_reasons:
        by = {}
        for r in rows:
            by.setdefault(r["reason"], []).append(r["pnl"])
        print("\n[exit-reasons] 청산 reason 별 PnL - 손익 비대칭 구조 확인용")
        h = f"{'reason':>24}{'n':>5}{'win%':>7}{'mean%':>8}{'sum%p':>9}{'best':>8}{'worst':>8}"
        print(h)
        print("-" * len(h))
        for k in sorted(by, key=lambda x: sum(by[x])):
            xs = by[k]
            print(f"{k[:24]:>24}{len(xs):>5}{_winrate(xs):>6.0f}%"
                  f"{st.mean(xs):>+8.2f}{sum(xs):>+9.1f}{max(xs):>+8.2f}{min(xs):>+8.2f}")
        print("-> 어떤 청산 경로가 돈을 벌고(러너) 어떤 경로가 까먹는지. 진입 필터와 독립된 레버리지.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated_for_rows": len(rows),
            "overall_mean_pnl_pct": st.mean(allp),
            "overall_winrate": _winrate(allp),
            "arms": out,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
