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
    # 실험(2026-06-11): btc_correlation 누적에서 ETH 4h "완만한 상승(up)" 레짐 진입이
    # 반복적으로 최악(승 27%/평균 -3.9%)이었다. 그 레짐 진입을 차단하면 실제로 나아지는지
    # shadow 로 검증. 거동 영향 없음, 차단 여부만 기록.
    'shadow_e_block_eth_up': {
        'description': 'block entries when ETH 4h regime == up',
        'block_eth_regimes': ['up'],
    },
    'shadow_f_block_eth_up_strong': {
        'description': 'block entries when ETH 4h regime in {up, strong_up}',
        'block_eth_regimes': ['up', 'strong_up'],
    },
    # 실험: sweet_spot 경로 자체가 net-negative 인지 검증(현 진입 대부분이 sweet_spot).
    'shadow_g_disable_sweet_spot': {
        'description': 'sweet_spot path disabled',
        'enable_paths': ['golden_zone', 'reflexivity'],
    },
    # 실험: "최악 레짐 x 주력 경로" 조합만 차단 — up 레짐의 sweet_spot 진입만.
    'shadow_h_block_up_sweet_spot': {
        'description': 'block sweet_spot entries when ETH 4h regime == up',
        'block_eth_regimes': ['up'],
        'block_paths': ['sweet_spot'],
    },
}

DATA_PATH = Path(__file__).resolve().parent / 'data' / 'shadow_decisions.jsonl'
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)


def _current_eth_4h_regime() -> str:
    """진입시점 ETH 4h 레짐(게이트가 받아둔 캐시 스냅샷 재사용). 실패 시 'unknown'
    → 어떤 block 리스트에도 안 들어가므로 안전하게 현 게이트와 동일 동작."""
    try:
        from eth_macro_filter import get_eth_macro_filter
        _, regime = get_eth_macro_filter().current_regime()
        return regime
    except Exception:
        return 'unknown'


def evaluate_shadow(token_dict: dict, current_passes_safety_gate: Callable) -> dict:
    results = {}
    for shadow_name, overrides in SHADOW_RULES.items():
        backup_vars = {}
        try:
            if 'block_eth_regimes' in overrides:
                # 현 게이트를 먼저 통과한 진입만 의미 있음. 통과 + 차단 레짐이면 would_block.
                chain = token_dict.get('chain', 'base')
                passed, reason = current_passes_safety_gate(token_dict, chain)
                if passed:
                    regime = _current_eth_4h_regime()
                    block_paths = overrides.get('block_paths')  # None = 전 경로
                    path_match = block_paths is None or token_dict.get('entry_path') in block_paths
                    if regime in overrides['block_eth_regimes'] and path_match:
                        results[shadow_name] = {
                            'passed': False,
                            'reason': f'eth_regime_blocked_{regime}',
                        }
                        continue
                results[shadow_name] = {
                    'passed': bool(passed),
                    'reason': reason if not passed else 'passed',
                }
                continue

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