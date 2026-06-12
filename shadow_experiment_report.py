# -*- coding: utf-8 -*-
"""shadow_experiment_report.py — shadow 실험 arm별 "막았으면 더 나았나" 판정.

설계: shadow_decisions.jsonl(사이클별 중복/미진입 포함) 대신, 실제 진입
(entry_snapshots.jsonl ⨝ data/positions.jsonl) 의 저장된 피처로 각 arm 의
would_block 을 재계산해 실현 PnL 과 직접 대조한다. 이게 결과 연결이 깔끔하다.

각 arm 에 대해 blocked(막았을 진입) vs allowed(남길 진입) 의 PnL 을 비교:
  - blocked 평균이 음수이고 allowed 평균이 overall 보다 높으면 → 그 게이트가 도움.
  - "removed $" = 막았을 거래들의 합(음수면 그만큼 손실 회피).

SHADOW_RULES 의 피처 기반 arm(enable_paths / block_eth_regimes(+block_paths) /
block_if_pc1h_above / allow_pc1h_range / allow_utc_hours)을 재계산한다.
gate 파라미터 override(a~d 의 sweet_spot_max_15m_drop_pct 등)는 게이트 재실행이
필요해 여기선 'gate-param (skip)' 으로 표시(그건 shadow_decisions.jsonl 로 본다).

사용:
  python shadow_experiment_report.py                # 누적 전체
  python shadow_experiment_report.py --days 7         # 최근 7일 진입
  python shadow_experiment_report.py --min-n 15       # 판정 플래그 최소 표본
  python shadow_experiment_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow_rules import SHADOW_RULES

ROOT = Path(__file__).parent
ENTRY_LOG = ROOT / "data" / "entry_snapshots.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"
MATCH_TOLERANCE_SEC = 1800


def _parse(x):
    if not x:
        return None
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
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
        last[(str(r.get("chain")).lower(), str(r.get("contract_address")).lower(), str(r.get("entry_timestamp")))] = r
    out = []
    for r in last.values():
        if not r.get("is_closed") or r.get("realized_pnl_pct") is None:
            continue
        et = _parse(r.get("entry_timestamp"))
        if not et:
            continue
        out.append({"chain": str(r.get("chain")).lower(), "contract": str(r.get("contract_address")).lower(),
                    "ets": et.timestamp(), "pnl": float(r.get("realized_pnl_pct")), "_used": False})
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
        rows.append({**e, "pnl": best["pnl"]})
    return rows


def arm_blocks(overrides, e):
    """저장된 진입 피처로 arm 의 would_block 재계산. (blocked: bool, evaluable: bool)
    gate-param override 는 평가 불가(evaluable=False)."""
    if "sweet_spot_max_15m_drop_pct" in overrides:
        return False, False  # gate 재실행 필요 → skip

    if "enable_paths" in overrides:
        return e.get("entry_path") not in overrides["enable_paths"], True

    if "block_eth_regimes" in overrides:
        reg = e.get("eth_4h_regime")
        if not reg or reg == "unknown":
            return False, False  # 레짐 미태깅 → 평가 불가
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
        h = e.get("utc_hour")
        if not isinstance(h, int):
            return False, False
        s, en = overrides["allow_utc_hours"]
        return not (s <= h < en), True

    return False, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=10, help="판정 플래그 최소 blocked 표본")
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rows = join(load_entries(since), load_closed())
    if not rows:
        print("조인된 거래 없음")
        return
    allp = [r["pnl"] for r in rows]
    overall = st.mean(allp)
    print(f"조인된 거래: {len(rows)}건  전체 승률 {sum(p>0 for p in allp)/len(allp)*100:.0f}%  평균 {overall:+.2f}%\n")

    hdr = f"{'arm':>26}{'eval':>6}{'blk':>5}{'blkPnL':>8}{'allowN':>7}{'allowPnL':>9}{'d_mean':>8}{'removed%':>10}"
    print(hdr); print("-" * len(hdr))
    out = {}
    for name, ov in SHADOW_RULES.items():
        blocked, allowed, n_eval = [], [], 0
        for r in rows:
            blk, evaluable = arm_blocks(ov, r)
            if not evaluable:
                continue
            n_eval += 1
            (blocked if blk else allowed).append(r["pnl"])
        if n_eval == 0:
            print(f"{name:>26}{'skip':>6}{'-':>5}{'gate-param/미태깅':>27}")
            continue
        bmean = st.mean(blocked) if blocked else None
        amean = st.mean(allowed) if allowed else None
        # 막은 거래를 제거했을 때 남는 집합(allowed)의 평균 vs 전체평균
        dmean = (amean - overall) if amean is not None else None
        removed = sum(blocked)  # %p 합(사이즈 동일 가정 → 음수면 손실 회피)
        flag = ""
        if blocked and len(blocked) >= args.min_n and bmean is not None and amean is not None:
            if bmean < amean and bmean < 0:
                flag = "  <== 도움"
        print(f"{name:>26}{n_eval:>6}{len(blocked):>5}"
              f"{(f'{bmean:+.2f}' if bmean is not None else '-'):>8}"
              f"{len(allowed):>7}{(f'{amean:+.2f}' if amean is not None else '-'):>9}"
              f"{(f'{dmean:+.2f}' if dmean is not None else '-'):>8}{removed:>+10.1f}{flag}")
        out[name] = {"n_eval": n_eval, "n_blocked": len(blocked),
                     "blocked_mean": bmean, "allowed_mean": amean,
                     "delta_mean": dmean, "removed_pnl_pct_sum": removed}

    print("\n해석: blkPnL<allowPnL 이고 blk 표본이 충분하면 그 arm 이 막은 거래가 실제로 더 나빴다는 뜻.")
    print("      d_mean = 막은 뒤 남는 거래 평균 - 전체평균(클수록 개선). removed% = 막았을 거래 PnL%p 합.")
    print("      eval=0(skip) 은 gate-param override(a~d) 거나 해당 피처 미태깅 — shadow_decisions.jsonl 로 별도 확인.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated_for_rows": len(rows), "overall_mean_pnl_pct": overall, "arms": out,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
