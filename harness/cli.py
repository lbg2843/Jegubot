import argparse
import json
from pathlib import Path

from .analyzer import aggregate, compare
from .load_data import filter_by_period, load_actual_trades, load_candidates, load_outcomes, load_snapshots
from .rules import RULES_CURRENT, RULES_PROPOSED
from .simulator import simulate_trade, simulate_trade_v2

# Limits / caveats:
# - fixed slippage 0.5% each side
# - no gas costs
# - v1 uses sparse checkpoints and is only for legacy comparison
# - v2 uses raw snapshots, but data can stop when tokens leave trending
# - use for relative rule comparison, not absolute PnL prediction


def print_summary(agg: dict):
    print(f"  entries:   {agg['entries']}")
    print(f"  rejected:  {agg['rejected']}")
    print(f"  wins:      {agg['wins']}")
    print(f"  losses:    {agg['losses']}")
    print(f"  win rate:  {agg['win_rate']:.2f}%")
    print(f"  avg pnl:   {agg['avg_pnl_pct']:.2f}%")
    print(f"  sum usd:   ${agg['sum_usd']:.2f}")
    if agg.get('by_reason'):
        print("  by reason:")
        for reason, info in sorted(agg['by_reason'].items()):
            print(f"    - {reason}: n={info['n']} sum=${info['sum_usd']:.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', default='current')
    parser.add_argument('--candidate', default=None)
    parser.add_argument('--period', default=None, help='YYYY-MM-DD:YYYY-MM-DD KST')
    parser.add_argument('--output-json', default=None)
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--use-v2', action='store_true', default=True, help='Use raw snapshot based simulator (v2)')
    parser.add_argument('--use-v1', dest='use_v2', action='store_false', help='Use checkpoint based simulator (v1, deprecated)')
    args = parser.parse_args()

    print('Loading candidates...')
    candidates = load_candidates(only_paths=['sweet_spot', 'reflexivity', 'golden_zone'])

    start_kst = end_kst = None
    if args.period:
        start_kst, end_kst = args.period.split(':', 1)
        candidates = filter_by_period(candidates, start_kst, end_kst)

    if args.use_v2:
        print('Loading raw snapshots (v2 mode)...')
        snapshots_by_token = load_snapshots(chains=sorted({c.chain for c in candidates}))
        def sim_fn(cand, rules):
            key = (cand.chain, cand.token_address.lower())
            token_snaps = snapshots_by_token.get(key, [])
            return simulate_trade_v2(cand, token_snaps, rules)
    else:
        print('Loading outcomes (v1 mode)...')
        outcomes = load_outcomes()
        def sim_fn(cand, rules):
            return simulate_trade(cand, outcomes.get(cand.snapshot_id, []), rules)

    baseline_rules = RULES_CURRENT if args.baseline == 'current' else RULES_PROPOSED[args.baseline]
    baseline_results = [sim_fn(c, baseline_rules) for c in candidates]
    baseline_agg = aggregate(baseline_results)

    print('\n' + '=' * 60)
    print(f'BASELINE: {args.baseline}')
    print_summary(baseline_agg)

    actual_summary = None
    if args.validate and start_kst and end_kst:
        actual_trades = load_actual_trades(start_kst=start_kst, end_kst=end_kst)
        actual_sum = sum(t.realized_pnl_usd for t in actual_trades)
        gap = abs(baseline_agg['sum_usd'] - actual_sum) / max(abs(actual_sum), 1e-9) * 100.0 if actual_trades else 0.0
        actual_summary = {
            'closed_trades': len(actual_trades),
            'sum_usd': actual_sum,
            'gap_pct': gap,
        }
        print('\n' + '=' * 60)
        print('VALIDATION')
        print(f"  actual closed trades: {len(actual_trades)}")
        print(f"  actual sum usd:       ${actual_sum:.2f}")
        print(f"  sim-vs-actual gap:    {gap:.2f}%")

    cand_agg = None
    diff = None
    if args.candidate:
        cand_rules = RULES_PROPOSED[args.candidate]
        cand_results = [sim_fn(c, cand_rules) for c in candidates]
        cand_agg = aggregate(cand_results)

        print('\n' + '=' * 60)
        print(f'CANDIDATE: {args.candidate}')
        print_summary(cand_agg)

        diff = compare(baseline_agg, cand_agg)
        print('\n' + '=' * 60)
        print('DIFF')
        print(f"  PnL:      ${diff['pnl_diff_usd']:+.2f}")
        print(f"  Entries:  {diff['entries_diff']:+d}")
        print(f"  Win rate: {diff['win_rate_diff']:+.2f}%")

    if args.output_json:
        output = {
            'baseline': {'name': args.baseline, 'agg': baseline_agg},
            'candidate': {'name': args.candidate, 'agg': cand_agg} if args.candidate else None,
            'diff': diff,
            'validation': actual_summary,
            'use_v2': args.use_v2,
        }
        Path(args.output_json).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f'\nJSON saved: {args.output_json}')


if __name__ == '__main__':
    main()