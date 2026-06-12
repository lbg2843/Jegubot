# -*- coding: utf-8 -*-
"""stoploss_tail_report.py - 손절 꼬리 분해 + "동결 버그 꼬리가 사라졌나" 추적.

배경: exit-reasons 분해에서 적자가 통째로 STOP_LOSS 에 집중됐는데, 그 catastrophic
꼬리(-57% 등)의 정체가 (a) 동결 버그(피드 상실로 max_hold 넘겨 뒤늦게 청산) 인지
(b) 슬리피지/얇은 유동성인지가 쟁점이었다. 분해 결과 (a)로 확인 - 그리고 그 버그는
hold_hours/base-fallback/eval-order 3중 픽스로 수정됨(2026-06-10~11).

이 스크립트는 손절을 frozen(peak==entry)/overdue(hold>max_hold)/모멘텀/유동성으로
쪼개고, **주간 추세로 overdue·catastrophic 손절이 픽스 후 실제로 사라지는지** 추적한다.

사용:
  python stoploss_tail_report.py                 # 전체
  python stoploss_tail_report.py --days 14         # 최근 14일
  python stoploss_tail_report.py --reason ALL      # 모든 청산 reason 포함
  python stoploss_tail_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
POSITIONS = ROOT / "data" / "positions.jsonl"
ENTRY_LOG = ROOT / "data" / "entry_snapshots.jsonl"
CATASTROPHIC_PCT = -25.0  # 정상 손절(-20% 안팎)을 크게 벗어난 손실


def _parse(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _max_hold(chain):
    defaults = {"base": 8.0, "bsc": 24.0, "solana": 2.0}
    try:
        return float(os.getenv(f"{chain.upper()}_MAX_HOLD_HOURS", str(defaults.get(chain, 24.0))))
    except ValueError:
        return defaults.get(chain, 24.0)


def load_entry_features():
    feats = {}
    if not ENTRY_LOG.exists():
        return feats
    for line in ENTRY_LOG.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        d = _parse(r.get("logged_at"))
        if not d:
            continue
        key = (str(r.get("chain")).lower(), (r.get("contract_address") or "").lower())
        pc1h = None
        for k in ("pc_1h", "price_change_1h_pct", "price_change_1h"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                pc1h = float(v)
                break
        feats.setdefault(key, []).append((d.timestamp(), pc1h, r.get("liquidity_usd")))
    return feats


def _feat(feats, chain, contract, ets):
    best = bg = None
    for ts, pc1h, liq in feats.get((chain, contract), []):
        g = abs(ts - ets)
        if g > 1800:
            continue
        if best is None or g < bg:
            best, bg = (pc1h, liq), g
    return best or (None, None)


def load_stops(since, reasons):
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
    feats = load_entry_features()
    out = []
    for r in last.values():
        if not r.get("is_closed") or r.get("realized_pnl_pct") is None:
            continue
        reason = str(r.get("exit_reason"))
        if reasons != "ALL" and reason not in reasons:
            continue
        et, xt = _parse(r.get("entry_timestamp")), _parse(r.get("exit_timestamp"))
        if not et or not xt or (since and xt < since):
            continue
        chain = str(r.get("chain")).lower()
        hold = (xt - et).total_seconds() / 3600
        ep = float(r.get("entry_price") or 0)
        pk = float(r.get("peak_price") or 0)
        peak_pnl = (pk / ep - 1) * 100 if ep > 0 else 0.0
        pc1h, liq = _feat(feats, chain, str(r.get("contract_address")).lower(), et.timestamp())
        out.append({
            "sym": str(r.get("symbol"))[:10], "chain": chain, "reason": reason,
            "xt": xt, "hold": hold, "pnl": float(r.get("realized_pnl_pct")),
            "peak_pnl": peak_pnl, "frozen": abs(pk - ep) < 1e-15,
            "overdue": hold > _max_hold(chain), "pc1h": pc1h, "liq": liq,
        })
    out.sort(key=lambda x: x["pnl"])
    return out


def _wk(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--reason", default="STOP_LOSS", help="STOP_LOSS(기본) | ALL | 콤마구분")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    reasons = "ALL" if args.reason.upper() == "ALL" else set(args.reason.split(","))
    rows = load_stops(since, reasons)
    if not rows:
        print("해당 청산 없음")
        return

    print(f"=== 손절 분해 {len(rows)}건 (reason={args.reason}), 손실 큰 순 ===")
    hdr = f"{'exit(UTC)':12}{'sym':>11}{'ch':>5}{'pnl%':>8}{'peak%':>8}{'hold':>7}{'froz':>6}{'odue':>6}{'pc1h':>7}{'liq':>8}"
    print(hdr); print("-" * len(hdr))
    for r in rows[:40]:
        liq = f"{r['liq']/1000:.0f}k" if isinstance(r["liq"], (int, float)) else "-"
        pc = f"{r['pc1h']:+.1f}" if isinstance(r["pc1h"], (int, float)) else "-"
        print(f"{str(r['xt'])[5:16]:12}{r['sym']:>11}{r['chain']:>5}{r['pnl']:>+7.1f}%{r['peak_pnl']:>+7.1f}%"
              f"{r['hold']:>6.1f}h{('Y' if r['frozen'] else '.'):>6}{('Y' if r['overdue'] else '.'):>6}{pc:>7}{liq:>8}")

    def seg(name, pred):
        s = [r["pnl"] for r in rows if pred(r)]
        o = [r["pnl"] for r in rows if not pred(r)]
        sm = f"{st.mean(s):+.1f}% (n{len(s)}, 합{sum(s):+.0f})" if s else "n0"
        om = f"{st.mean(o):+.1f}% (n{len(o)})" if o else "n0"
        print(f"  {name:>22}: {sm:>28}  | 그외 {om}")
        return s

    print("\n--- 무엇이 손실 크기를 가르나 ---")
    odue = seg("overdue(만기초과)", lambda r: r["overdue"])
    seg("frozen(peak==entry)", lambda r: r["frozen"])
    cat = seg(f"catastrophic(<{CATASTROPHIC_PCT:.0f}%)", lambda r: r["pnl"] < CATASTROPHIC_PCT)

    # 동결 꼬리 기여도: overdue 손절이 정상(-15% 가정)이었을 때 대비 초과손실
    NORMAL = -15.0
    excess = sum(min(0.0, p - NORMAL) for p in odue)
    print(f"\n  동결꼬리(overdue) 초과손실 추정: {excess:+.0f}%p "
          f"(overdue {len(odue)}건이 정상 {NORMAL:.0f}% 였다면 그만큼 덜 잃음)")

    print("\n--- 주간 추세 (픽스 후 overdue/catastrophic 이 사라지는지) ---")
    wks = {}
    for r in rows:
        wks.setdefault(_wk(r["xt"]), []).append(r)
    print(f"  {'week':>10}{'N':>5}{'overdue':>8}{'catas':>7}{'sumPnL':>9}{'mean':>8}")
    out_weeks = {}
    for k in sorted(wks):
        w = wks[k]
        nod = sum(r["overdue"] for r in w)
        ncat = sum(r["pnl"] < CATASTROPHIC_PCT for r in w)
        print(f"  {k:>10}{len(w):>5}{nod:>8}{ncat:>7}{sum(r['pnl'] for r in w):>+9.0f}{st.mean(r['pnl'] for r in w):>+8.1f}")
        out_weeks[k] = {"n": len(w), "overdue": nod, "catastrophic": ncat,
                        "sum_pnl": sum(r["pnl"] for r in w)}
    print("\n  -> 픽스(2026-06-10~11) 이후 주에 overdue/catas 가 0 으로 수렴하면 동결 꼬리 제거 확인.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "n": len(rows), "weeks": out_weeks,
            "overdue_excess_loss_pct": excess,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved: {args.json}")


if __name__ == "__main__":
    main()
