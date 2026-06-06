from .load_data import Candidate


def passes_entry_gates(cand: Candidate, rules: dict, eth_4h_change_at_ts: float = 0.0) -> tuple[bool, str]:
    if cand.entry_path not in rules['enable_paths']:
        return False, f"path_disabled_{cand.entry_path}"

    if eth_4h_change_at_ts < rules['eth_macro_threshold_pct']:
        return False, f"eth_4h_down ({eth_4h_change_at_ts:+.2f}%)"

    if cand.entry_path == 'sweet_spot':
        if cand.price_change_15m_pct < rules['sweet_spot_max_15m_drop_pct']:
            return False, f"sweet_spot_falling_15m ({cand.price_change_15m_pct:+.2f}%)"

    # entry-gate refinements (disabled when threshold is None)
    min_24h = rules.get('min_price_change_24h_pct')
    if min_24h is not None and cand.price_change_24h_pct < min_24h:
        return False, f"pc24h_below_{min_24h:g} ({cand.price_change_24h_pct:+.2f}%)"

    min_liq = rules.get('min_liquidity_usd')
    if min_liq is not None and cand.liquidity_usd < min_liq:
        return False, f"liq_below_{min_liq:g} (${cand.liquidity_usd:,.0f})"

    if not cand.passed:
        return False, 'underlying_gate_failed'

    return True, 'passed'