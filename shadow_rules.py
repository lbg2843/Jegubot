"""
Shadow rules - comparison-only candidate rule sets.
No effect on live entries; logs decision differences only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SHADOW_RULES = {
    'shadow_a_disable_gz': {
        'description': 'golden_zone path disabled',
        'enable_paths': ['sweet_spot', 'reflexivity'],
    },
    'shadow_b_falling_3pct': {
        'description': 'falling-knife -2.5 -> -3.0',
        'sweet_spot_max_15m_drop_pct': -3.0,
    },
    'shadow_c_falling_2pct': {
        'description': 'falling-knife -2.5 -> -2.0',
        'sweet_spot_max_15m_drop_pct': -2.0,
    },
    'shadow_d_combo': {
        'description': 'gz disabled + falling -3.0',
        'enable_paths': ['sweet_spot', 'reflexivity'],
        'sweet_spot_max_15m_drop_pct': -3.0,
    },
}

DATA_PATH = Path(__file__).resolve().parent / 'data' / 'shadow_decisions.jsonl'
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)


def evaluate_shadow(token_dict: dict, current_passes_safety_gate: Callable) -> dict:
    results = {}
    for shadow_name, overrides in SHADOW_RULES.items():
        backup_vars = {}
        try:
            if 'enable_paths' in overrides:
                if token_dict.get('entry_path') not in overrides['enable_paths']:
                    results[shadow_name] = {
                        'passed': False,
                        'reason': f"path_disabled_{token_dict.get('entry_path')}",
                    }
                    continue

            if 'sweet_spot_max_15m_drop_pct' in overrides:
                key = 'SWEET_SPOT_MAX_15M_DROP_PCT'
                backup_vars[key] = os.environ.get(key)
                os.environ[key] = str(overrides['sweet_spot_max_15m_drop_pct'])

            chain = token_dict.get('chain', 'base')
            passed, reason = current_passes_safety_gate(token_dict, chain)
            results[shadow_name] = {
                'passed': bool(passed),
                'reason': reason if not passed else 'passed',
            }
        finally:
            for key, value in backup_vars.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return results


def log_shadow_decision(token_dict: dict, current_result: tuple, shadow_results: dict, log_path: str | None = None):
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'symbol': token_dict.get('symbol'),
        'chain': token_dict.get('chain'),
        'token_address': token_dict.get('token_address') or token_dict.get('contract_address'),
        'entry_path': token_dict.get('entry_path'),
        'candidate_metrics': {
            'price_change_5m_pct': token_dict.get('price_change_5m_pct'),
            'price_change_15m_pct': token_dict.get('price_change_15m_pct'),
            'price_change_1h_pct': token_dict.get('price_change_1h_pct'),
            'liquidity_usd': token_dict.get('liquidity_usd'),
            'volume_15m_usd': token_dict.get('volume_15m_usd'),
            'final_score': token_dict.get('final_score'),
        },
        'decisions': {
            'current': {
                'passed': bool(current_result[0]),
                'reason': current_result[1] if not current_result[0] else 'passed',
            },
            **shadow_results,
        },
    }

    current_passed = bool(current_result[0])
    all_same = all(bool(s.get('passed')) == current_passed for s in shadow_results.values())
    if all_same:
        return

    path = Path(log_path) if log_path else DATA_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')