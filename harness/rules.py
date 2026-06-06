RULES_CURRENT = {
    'eth_macro_threshold_pct': -1.5,
    'enable_paths': ['sweet_spot', 'reflexivity', 'golden_zone'],
    'sweet_spot_max_15m_drop_pct': -2.5,
    'early_stop_window_min': 30,
    'early_stop_threshold_pct': -7.0,
    'stop_loss_pct': -20.0,
    'trailing_stop_pct': -10.0,
    'profit_lock_threshold_pct': 20.0,
    'base_max_hold_hours': 8,
    'bsc_max_hold_hours': 24,
    'solana_max_hold_hours': 2,
    'slippage_pct': 0.5,
    'position_size_usd': 50.0,
    # entry-gate refinements (None = gate disabled)
    'min_price_change_24h_pct': None,   # reject entries below this 24h change
    'min_liquidity_usd': None,          # reject entries below this liquidity
}

RULES_PROPOSED = {
    'disable_golden_zone': {
        **RULES_CURRENT,
        'enable_paths': ['sweet_spot', 'reflexivity'],
    },
    'early_stop_minus_10': {
        **RULES_CURRENT,
        'early_stop_threshold_pct': -10.0,
    },
    'max_hold_6h': {
        **RULES_CURRENT,
        'base_max_hold_hours': 6,
    },
    'falling_knife_minus_2': {
        **RULES_CURRENT,
        'sweet_spot_max_15m_drop_pct': -2.0,
    },
    'combo_a': {
        **RULES_CURRENT,
        'enable_paths': ['sweet_spot', 'reflexivity'],
        'early_stop_threshold_pct': -10.0,
        'base_max_hold_hours': 6,
    },
    # G1: don't enter tokens already net-negative over 24h.
    # OOS-robust (H1 +$145 / H2 +$134), preserves the profitable bsc book.
    'reject_24h_negative': {
        **RULES_CURRENT,
        'min_price_change_24h_pct': 0.0,
    },
    # G1 + G3: also require >= $800k liquidity. Stronger in-sample (flips to
    # net-positive) but cuts ~71% of entries; the 800k threshold is in-sample
    # fit and weaker out-of-sample (H2 only +$67). Keep for comparison only.
    'reject_24h_neg_liq800k': {
        **RULES_CURRENT,
        'min_price_change_24h_pct': 0.0,
        'min_liquidity_usd': 800000.0,
    },
}