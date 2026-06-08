# -*- coding: utf-8 -*-
"""regime_outcome_report.py — ETH 4h 레짐별 진입 성과 재검증 리포트.

feat(analysis) 진입시점 ETH 4h 레짐 태깅의 동반 도구. 2주 뒤 한 줄로 돌려
"완만한 상승(up)" 레짐 거래가 실제로 다른 레짐보다 성과가 나쁜지 확인한다.

조인:
  data/entry_snapshots.jsonl  (진입시점, eth_4h_regime 박힘)
    ⨝ data/positions.jsonl    (청산 손익 realized_pnl_pct 보유)
  키: (chain, contract_address) + logged_at ≈ entry_timestamp 근접매칭.
  같은 토큰 반복 진입은 시각이 가장 가까운 청산 1건에 그리디 매칭.

사용:
  python regime_outcome_report.py                 # 전체
  python regime_outcome_report.py --days 14        # 최근 14일 진입만
  python regime_outcome_report.py --backfill       # 미태깅 진입은 CoinGecko로 ETH4h 역산
  python regime_outcome_report.py --json out.json  # 결과 JSON 저장
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 분석 버킷과 동일 경계 (진입 태깅과 1:1).
from eth_macro_filter import ETH_4H_REGIME_BANDS, classify_eth_4h_regime

ROOT = Path(__file__).parent
ENTRY_LOG = ROOT / "data" / "entry_snapshots.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"

BAND_ORDER = [name for name, _, _ in ETH_4H_REGIME_BANDS]
MATCH_TOLERANCE_SEC = 1800  # logged_at 와 entry_timestamp 허용 격차 (30분)


def _parse_dt(value):
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_entries(since: datetime | None) -> list[dict]:
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
        dt = _parse_dt(r.get("logged_at"))
        if dt is None or (since and dt < since):
            continue
        contract = (r.get("contract_address") or "").lower()
        if not contract:
            continue
        out.append({
            "logged_at": dt,
            "ts": dt.timestamp(),
            "chain": (r.get("chain") or "?").lower(),
            "contract": contract,
            "symbol": r.get("symbol") or "?",
            "regime": r.get("eth_4h_regime"),
            "eth_4h_pct": r.get("eth_4h_pct"),
        })
    return out


def load_closed_positions() -> list[dict]:
    """append-only 스냅샷을 (chain,contract,entry_timestamp)별 마지막 상태로 접고,
    청산되어 realized_pnl_pct 가 있는 포지션만 반환."""
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
        key = (str(r.get("chain")).lower(), str(r.get("contract_address")).lower(), str(r.get("entry_timestamp")))
        last[key] = r
    closed = []
    for r in last.values():
        if not r.get("is_closed"):
            continue
        if r.get("realized_pnl_pct") is None:
            continue
        et = _parse_dt(r.get("entry_timestamp"))
        if et is None:
            continue
        closed.append({
            "chain": str(r.get("chain")).lower(),
            "contract": str(r.get("contract_address")).lower(),
            "symbol": r.get("symbol") or "?",
            "entry_ts": et.timestamp(),
            "pnl_pct": float(r.get("realized_pnl_pct")),
            "pnl_usd": float(r.get("realized_pnl_usd") or 0.0),
            "exit_reason": r.get("exit_reason"),
            "_used": False,
        })
    return closed


def join(entries: list[dict], closed: list[dict]) -> tuple[list[dict], int]:
    """진입 ⨝ 청산: 같은 (chain,contract) 중 시각 가장 가까운 청산에 그리디 매칭."""
    by_token: dict[tuple, list[dict]] = {}
    for c in closed:
        by_token.setdefault((c["chain"], c["contract"]), []).append(c)

    matched = []
    unmatched = 0
    for e in sorted(entries, key=lambda x: x["ts"]):
        cands = by_token.get((e["chain"], e["contract"]), [])
        best, best_gap = None, None
        for c in cands:
            if c["_used"]:
                continue
            gap = abs(c["entry_ts"] - e["ts"])
            if gap > MATCH_TOLERANCE_SEC:
                continue
            if best is None or gap < best_gap:
                best, best_gap = c, gap
        if best is None:
            unmatched += 1
            continue
        best["_used"] = True
        matched.append({**e, "pnl_pct": best["pnl_pct"], "pnl_usd": best["pnl_usd"],
                        "exit_reason": best["exit_reason"], "match_gap_sec": int(best_gap)})
    return matched, unmatched


def backfill_regimes(matched: list[dict]) -> int:
    """regime 이 없는(과거) 진입을 CoinGecko ETH 4h 로 역산 분류. 채운 개수 반환."""
    todo = [m for m in matched if not m.get("regime") or m["regime"] == "unknown"]
    if not todo:
        return 0
    try:
        import time
        import btc_correlation as bc
    except Exception as exc:  # noqa: BLE001
        print(f"[backfill] skipped (btc_correlation import 실패: {exc})")
        return 0
    earliest = min(m["ts"] for m in todo) - 30 * 3600
    latest = max(m["ts"] for m in todo) + 3600
    try:
        eth = bc.fetch_coingecko_prices("ethereum", earliest, latest)
        time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[backfill] skipped (가격 fetch 실패: {exc})")
        return 0
    filled = 0
    for m in todo:
        chg = bc.compute_change_pct(eth, m["ts"], 4)
        if chg is None:
            continue
        m["eth_4h_pct"] = chg
        m["regime"] = classify_eth_4h_regime(chg)
        m["_backfilled"] = True
        filled += 1
    return filled


def aggregate(matched: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for m in matched:
        reg = m.get("regime") or "unknown"
        groups.setdefault(reg, []).append(m)
    stats = {}
    for reg, rows in groups.items():
        pnls = [r["pnl_pct"] for r in rows]
        wins = [p for p in pnls if p > 0]
        stats[reg] = {
            "n": len(rows),
            "win_rate": len(wins) / len(rows) * 100.0,
            "mean_pnl_pct": statistics.mean(pnls),
            "median_pnl_pct": statistics.median(pnls),
            "sum_pnl_usd": sum(r["pnl_usd"] for r in rows),
        }
    return stats


def print_report(stats: dict, matched: int, unmatched: int, untagged: int, filled: int):
    order = BAND_ORDER + [k for k in stats if k not in BAND_ORDER]
    print("\n===== ETH 4h 레짐별 진입 성과 =====")
    hdr = f"{'regime':>12} {'N':>4} {'win%':>7} {'mean PnL':>10} {'median':>9} {'sum $':>10}"
    print(hdr)
    print("-" * len(hdr))
    for reg in order:
        s = stats.get(reg)
        if not s:
            continue
        print(f"{reg:>12} {s['n']:>4} {s['win_rate']:>6.1f}% {s['mean_pnl_pct']:>+9.2f}% "
              f"{s['median_pnl_pct']:>+8.2f}% {s['sum_pnl_usd']:>+10.2f}")
    print("-" * len(hdr))
    print(f"matched={matched}  unmatched_entries={unmatched}  untagged(regime없음)={untagged}"
          + (f"  backfilled={filled}" if filled else ""))
    up = stats.get("up")
    if up and up["n"] >= 5:
        worst = min(stats.items(), key=lambda kv: kv[1]["mean_pnl_pct"])
        flag = "  <== 가설대로 최악" if worst[0] == "up" else ""
        print(f"\n가설 점검: 'up'(완만한 상승) N={up['n']} win={up['win_rate']:.0f}% "
              f"mean={up['mean_pnl_pct']:+.2f}%{flag}")
        if up["n"] < 20:
            print("  주의: n<20 — 아직 통계적으로 약함. 더 쌓고 재검증.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="최근 N일 진입만")
    ap.add_argument("--backfill", action="store_true", help="미태깅 진입을 CoinGecko ETH4h로 역산 분류")
    ap.add_argument("--json", type=str, default=None, help="결과 JSON 저장 경로")
    args = ap.parse_args()

    since = None
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    entries = load_entries(since)
    closed = load_closed_positions()
    matched, unmatched = join(entries, closed)
    untagged = sum(1 for m in matched if not m.get("regime") or m["regime"] == "unknown")

    filled = 0
    if args.backfill:
        filled = backfill_regimes(matched)
        untagged = sum(1 for m in matched if not m.get("regime") or m["regime"] == "unknown")

    stats = aggregate(matched)
    print(f"entries(loaded)={len(entries)}  closed_positions={len(closed)}")
    print_report(stats, len(matched), unmatched, untagged, filled)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "matched": len(matched), "unmatched": unmatched, "untagged": untagged,
            "backfilled": filled, "regimes": stats,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {args.json}")


if __name__ == "__main__":
    main()
