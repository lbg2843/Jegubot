from harness.analyzer import aggregate
from harness.load_data import filter_by_period, load_actual_trades, load_candidates, load_snapshots
from harness.rules import RULES_CURRENT
from harness.simulator import simulate_trade_v2


def test_baseline_matches_actual_within_30pct():
    candidates = filter_by_period(load_candidates(), '2026-05-28', '2026-06-02')
    snapshots_by_token = load_snapshots(chains=sorted({c.chain for c in candidates}))
    sim_results = [simulate_trade_v2(c, snapshots_by_token.get((c.chain, c.token_address.lower()), []), RULES_CURRENT) for c in candidates]
    sim_agg = aggregate(sim_results)

    actual_trades = load_actual_trades(start_kst='2026-05-28', end_kst='2026-06-02')
    actual_pnl = sum(t.realized_pnl_usd for t in actual_trades)
    sim_pnl = sim_agg['sum_usd']
    gap_pct = abs(sim_pnl - actual_pnl) / max(abs(actual_pnl), 1e-9) * 100.0

    print(f'Actual: ${actual_pnl:.2f}')
    print(f'Sim:    ${sim_pnl:.2f}')
    print(f'Gap:    {gap_pct:.1f}%')

    assert gap_pct < 30, f'Baseline gap too large ({gap_pct:.1f}%) - model review needed'