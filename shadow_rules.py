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
    # 실험(2026-06-12): 진입 미시구조 신호. discriminator 스캔에서 매크로보다
    # pc_1h(진입 1h 모멘텀)/시간대가 승패를 더 잘 갈랐다.
    # 급등 추격(pc_1h>15%)은 0% 승/-17%, 양극단 회피·눌림(-2~+5%)이 우세, 6-12 UTC 가 +.
    'shadow_i_block_pump_chase': {
        'description': 'block entries with pc_1h > +15% (FOMO 추격 컷)',
        'block_if_pc1h_above': 15.0,
    },
    'shadow_j_pullback_only': {
        'description': 'allow only pc_1h in [-2%, +5%] (눌림~완만 구간만)',
        'allow_pc1h_range': [-2.0, 5.0],
    },
    'shadow_k_hours_6_12': {
        'description': 'allow only 6-12 UTC entries',
        'allow_utc_hours': [6, 12],
    },
    # 실험(2026-06-12): score_outcomes(base) 검증 — golden zone [0.15,0.25) 는 +54%/80%승으로
    # 유효하나 그 아래 <.15 는 junk(median -15%/31%승). 골든존 하한 미만만 차단.
    'shadow_l_score_below_floor': {
        'description': 'block base entries with final_score < 0.15 (golden zone 하한 미만)',
        'block_if_score_below': 0.15,
        'score_chains': ['base'],
    },
    # 실험(2026-06-15): base 는 ETH 하락에 약함(ETH 24h<=0 시 base 평균 -1.8% vs 상승 -0.5%,
    # n=285/181). ETH 24h 하락 시 base 진입 차단했으면 나았는지 검증. (bsc 는 BNB 와 무상관이라 제외)
    'shadow_m_base_eth24h_down': {
        'description': 'block base entries when ETH 24h <= 0',
        'block_if_eth24h_below': 0.0,
        'eth_chains': ['base'],
    },
    # 실험(2026-06-19): divergence realized 검증 - score<-0.5(과열+매도우위)가 -9.5%,
    # mid_neutral(중간모멘텀)이 -10%. divergence_score 낮은 진입(인식이 실수요 앞섬)을
    # 차단했으면 나았는지. probe/divergence/realized 셋 다 '과열 회피' 일관 -> 진짜 후보.
    'shadow_n_block_div_below': {
        'description': 'block entries with divergence_score < -0.5 (과열+매도우위)',
        'block_if_divergence_below': -0.5,
    },
    'shadow_o_block_div_below_strict': {
        'description': 'block entries with divergence_score < -0.25 (mid_neutral 까지 포함)',
        'block_if_divergence_below': -0.25,
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


def _current_eth_24h_pct():
    """진입시점 ETH 24h 변동률(캐시 스냅샷 재사용). 실패/미가용 시 None → 차단 안 함."""
    try:
        from eth_macro_filter import get_eth_macro_filter
        return get_eth_macro_filter().current_eth_24h_pct()
    except Exception:
        return None


# 게이트 통과 후 진입 피처로 거르는 실험 키들(파라미터 override 가 아님).
_POST_GATE_KEYS = ('block_eth_regimes', 'block_if_pc1h_above', 'allow_pc1h_range', 'allow_utc_hours',
                   'block_if_score_below', 'block_if_eth24h_below', 'block_if_divergence_below')


def _divergence_of(token_dict):
    """candidate 의 divergence_score(인식 vs 실수요). 데이터 없으면 None -> 차단 안 함."""
    try:
        from divergence import divergence_score
        pc = token_dict.get('price_change_1h_pct')
        if pc is None:
            pc = token_dict.get('price_change_1h')
        return divergence_score(pc, token_dict.get('buys_1h'), token_dict.get('sells_1h'))
    except Exception:
        return None


def _pc_1h(token_dict: dict):
    for k in ('price_change_1h_pct', 'price_change_1h', 'pc_1h'):
        v = token_dict.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _post_gate_block_reason(token_dict: dict, overrides: dict):
    """게이트 통과한 진입에 대해 실험 피처 필터를 적용. 차단 사유 문자열 또는 None.
    값이 없으면(예: pc_1h 누락) 해당 필터는 건너뛴다(안전 패스스루)."""
    if 'block_eth_regimes' in overrides:
        regime = _current_eth_4h_regime()
        block_paths = overrides.get('block_paths')  # None = 전 경로
        path_match = block_paths is None or token_dict.get('entry_path') in block_paths
        if regime in overrides['block_eth_regimes'] and path_match:
            return f'eth_regime_blocked_{regime}'

    pc1h = _pc_1h(token_dict)
    if 'block_if_pc1h_above' in overrides and pc1h is not None:
        thr = overrides['block_if_pc1h_above']
        if pc1h > thr:
            return f'pc1h_above_{thr}({pc1h:+.2f})'
    if 'allow_pc1h_range' in overrides and pc1h is not None:
        lo, hi = overrides['allow_pc1h_range']
        if not (lo <= pc1h <= hi):
            return f'pc1h_out_of_[{lo},{hi}]({pc1h:+.2f})'

    if 'allow_utc_hours' in overrides:
        start, end = overrides['allow_utc_hours']
        hour = datetime.now(timezone.utc).hour
        if not (start <= hour < end):
            return f'utc_hour_{hour}_out_of_[{start},{end})'

    if 'block_if_score_below' in overrides:
        sc = token_dict.get('final_score')
        chains = overrides.get('score_chains')
        if isinstance(sc, (int, float)) and (chains is None or token_dict.get('chain') in chains):
            thr = overrides['block_if_score_below']
            if sc < thr:
                return f'score_below_{thr}({sc:.3f})'

    if 'block_if_eth24h_below' in overrides:
        chains = overrides.get('eth_chains')
        if chains is None or token_dict.get('chain') in chains:
            eth24 = _current_eth_24h_pct()
            if eth24 is not None and eth24 < overrides['block_if_eth24h_below']:
                return f'eth24h_below_{overrides["block_if_eth24h_below"]}({eth24:+.2f})'

    if 'block_if_divergence_below' in overrides:
        div = _divergence_of(token_dict)
        if div is not None and div < overrides['block_if_divergence_below']:
            return f'divergence_below_{overrides["block_if_divergence_below"]}({div:+.3f})'
    return None


def evaluate_shadow(token_dict: dict, current_passes_safety_gate: Callable) -> dict:
    results = {}
    for shadow_name, overrides in SHADOW_RULES.items():
        backup_vars = {}
        try:
            if any(k in overrides for k in _POST_GATE_KEYS):
                # 현 게이트를 먼저 통과한 진입만 의미 있음. 통과 + 실험 필터 차단이면 would_block.
                chain = token_dict.get('chain', 'base')
                passed, reason = current_passes_safety_gate(token_dict, chain)
                if not passed:
                    results[shadow_name] = {'passed': False, 'reason': reason}
                    continue
                block_reason = _post_gate_block_reason(token_dict, overrides)
                results[shadow_name] = (
                    {'passed': False, 'reason': block_reason} if block_reason
                    else {'passed': True, 'reason': 'passed'}
                )
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