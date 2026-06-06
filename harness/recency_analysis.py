"""
Recency (token freshness) analysis for the entry book -- offline, read-only.

Consolidates three earlier scratch scripts (_recency_bt / _recency_soft /
_recency_revalidate) into one module. Judges recency by PnL / expectancy,
NOT AUC. Covers:

  1. age-bucket expectancy           (where the signal actually lives)
  2. hard gate sweep                 (age<=Xh; shows sample death)
  3. soft weight schemes             (n kept; effect vs capital concentration)
  4. uncensored re-validation        (true first-seen, drop truncated tokens)

age_h = hours from a token's true first-seen to entry. true first-seen =
min(timestamp) per (chain, contract) over data/{chain}_snapshots.jsonl, which
the live SnapshotStore already appends for every trending token each scan --
so NO live / entry_logger change is needed to obtain it.

Caveat: tokens already trending before snapshot collection began (per-chain
data_start) have first-seen pinned at ~data_start, so their age is
underestimated. The uncensored pass drops those (margin < N hours); it cannot
recover their true age, so that analysis is conditional on tokens that first
appeared AFTER data_start.

Run:  python -m harness.recency_analysis
Reports: reports/recency_soft_weight_analysis_2026-06-07.md
         reports/recency_revalidation_uncensored_2026-06-07.md
"""
import sys

import numpy as np

from .load_data import load_candidates, load_snapshots, parse_iso_utc
from .rule_engine import passes_entry_gates
from .rules import RULES_CURRENT
from .simulator import simulate_trade_v2

# raw snapshots carry some CJK symbols; avoid cp949 console crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENTRY_PATHS = ['sweet_spot', 'reflexivity', 'golden_zone']
POS_USD = 50.0  # nominal position size for relative $ sums


def build_trades():
    """Return (trades, chain_start). One row per entered+simulated trade with
    true age_h and margin_h (= first_seen - chain_start)."""
    cands = load_candidates(only_paths=ENTRY_PATHS)
    snaps = load_snapshots(chains=sorted({c.chain for c in cands}))

    chain_start, first_seen = {}, {}
    for (chain, contract), series in snaps.items():
        ts = [parse_iso_utc(s.timestamp) for s in series if s.timestamp]
        if not ts:
            continue
        fs = min(ts)
        first_seen[(chain, contract)] = fs
        if chain not in chain_start or fs < chain_start[chain]:
            chain_start[chain] = fs

    trades = []
    for c in cands:
        if not passes_entry_gates(c, RULES_CURRENT, 0.0)[0]:
            continue
        key = (c.chain, c.token_address.lower())
        fs, ct = first_seen.get(key), parse_iso_utc(c.timestamp)
        if not fs or not ct:
            continue
        r = simulate_trade_v2(c, snaps.get(key, []), RULES_CURRENT)
        if not r.get('entered'):
            continue
        trades.append({
            'ts': c.timestamp,
            'age_h': max(0.0, (ct - fs).total_seconds() / 3600.0),
            'margin_h': (fs - chain_start[c.chain]).total_seconds() / 3600.0,
            'pnl_pct': r['pnl_pct'],
            'chain': c.chain,
            'sym': c.symbol,
        })
    trades.sort(key=lambda t: t['ts'])
    return trades, chain_start


# ---- soft-weight schemes (keep n; compare at equal total capital) ----
def w_equal(a):  return np.ones_like(a)
def w_inv8(a):   return 1.0 / (1.0 + a / 8.0)
def w_inv24(a):  return 1.0 / (1.0 + a / 24.0)
def w_exp12(a):  return np.exp(-a / 12.0)


SCHEMES = [('equal', w_equal), ('inv(8h)', w_inv8), ('inv(24h)', w_inv24), ('exp(12h)', w_exp12)]
GATES = [('no-gate', 1e9), ('age<=2h', 2), ('age<=4h', 4), ('age<=8h', 8), ('age<=12h', 12), ('age<=24h', 24)]
BUCKETS = [('<=2h', 0, 2), ('2-4h', 2, 4), ('4-8h', 4, 8), ('8-24h', 8, 24),
           ('24-72h', 24, 72), ('72-168h', 72, 168), ('>168h', 168, 1e9)]


def _wexp(a, p, wf):
    w = wf(a)
    return float((w * p).sum() / w.sum()) if w.sum() else 0.0


def _top_decile(a, wf):
    w = wf(a)
    order = np.argsort(a)
    k = max(1, int(round(0.1 * len(a))))
    return float(w[order[:k]].sum() / w.sum()) if w.sum() else 0.0


def bucket_table(age, pnl):
    print("\n-- expectancy by age bucket (equal weight) --")
    print(f"{'bucket':10s} {'n':>4s} {'wr%':>5s} {'avg%':>7s}")
    for lab, lo, hi in BUCKETS:
        m = (age > lo) & (age <= hi) if lo > 0 else (age <= hi)
        if m.sum() == 0:
            print(f"{lab:10s} {0:>4d}     -       -")
            continue
        print(f"{lab:10s} {int(m.sum()):>4d} {(pnl[m]>0).mean()*100:>5.1f} {pnl[m].mean():>+7.2f}")


def hard_gate_table(age, pnl):
    print("\n-- hard gate (PnL/expectancy) --")
    print(f"{'gate':10s} {'n':>4s} {'kept%':>6s} {'wr%':>5s} {'avg%':>7s} {'sum(rel$)':>9s}")
    for lab, thr in GATES:
        m = age <= thr
        if m.sum() == 0:
            print(f"{lab:10s} {0:>4d} {0:>5.0f}%      -       -         -")
            continue
        p = pnl[m]
        print(f"{lab:10s} {int(m.sum()):>4d} {m.mean()*100:>5.0f}% "
              f"{(p>0).mean()*100:>5.1f} {p.mean():>+7.2f} {(p*POS_USD/100).sum():>+9.0f}")


def soft_weight_table(age, pnl):
    print("\n-- soft weight (n kept, equal total capital) --")
    print(f"{'scheme':10s} {'E_w(avg%)':>10s} {'top10%cap':>10s}")
    for name, wf in SCHEMES:
        print(f"{name:10s} {_wexp(age,pnl,wf):>+10.2f} {_top_decile(age,wf)*100:>9.0f}%")


def oos_table(trades, age, pnl):
    if len(trades) < 4:
        print("\n-- OOS split: too few trades --")
        return
    half = len(trades) // 2
    ts_split = trades[half]['ts']
    h1 = np.array([t['ts'] < ts_split for t in trades])
    print("\n-- OOS split (H1/H2) --")
    print(f"{'scheme':10s} {'H1 E_w':>8s} {'H1 n':>5s} {'H2 E_w':>8s} {'H2 n':>5s}")
    for name, wf in SCHEMES:
        e1, e2 = _wexp(age[h1], pnl[h1], wf), _wexp(age[~h1], pnl[~h1], wf)
        print(f"{name:10s} {e1:>+8.2f} {int(h1.sum()):>5d} {e2:>+8.2f} {int((~h1).sum()):>5d}")


def report_block(trades, title):
    print("\n" + "=" * 64)
    print(title + f"   (n={len(trades)})")
    print("=" * 64)
    if len(trades) < 4:
        print("  too few trades to analyze.")
        return
    age = np.array([t['age_h'] for t in trades])
    pnl = np.array([t['pnl_pct'] for t in trades])
    bucket_table(age, pnl)
    hard_gate_table(age, pnl)
    soft_weight_table(age, pnl)
    oos_table(trades, age, pnl)


def main():
    trades, chain_start = build_trades()
    print("=== per-chain snapshot data start (UTC) ===")
    for ch, st in sorted(chain_start.items()):
        print(f"  {ch:7s} {st.isoformat()}")
    print(f"\ntotal entered+simulated trades: {len(trades)}")

    # full book (tracking-age; truncation bias present)
    report_block(trades, "### FULL BOOK (tracking-age, truncation bias present)")

    # uncensored: keep tokens first seen >= data_start + N hours
    for N in (24, 48, 72):
        sub = [t for t in trades if t['margin_h'] >= N]
        report_block(sub, f"### UNCENSORED: first-seen >= data_start + {N}h")


if __name__ == '__main__':
    main()
