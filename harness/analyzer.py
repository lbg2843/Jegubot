from collections import Counter, defaultdict


def aggregate(results: list[dict]) -> dict:
    entered = [r for r in results if r.get('entered')]
    rejected = [r for r in results if not r.get('entered')]

    if not entered:
        return {
            'entries': 0,
            'rejected': len(rejected),
            'wins': 0,
            'losses': 0,
            'win_rate': 0.0,
            'avg_pnl_pct': 0.0,
            'sum_usd': 0.0,
            'by_reason': {},
            'by_path': {},
            'rejected_by_reason': dict(Counter(r.get('reject_reason') for r in rejected)),
        }

    pnls = [float(r.get('pnl_pct') or 0.0) for r in entered]
    usds = [float(r.get('pnl_usd') or 0.0) for r in entered]
    wins = sum(1 for p in pnls if p > 0)

    by_reason = defaultdict(list)
    by_path = defaultdict(list)
    for r in entered:
        by_reason[r.get('exit_reason')].append(r)
        by_path[r.get('path', '?')].append(r)

    return {
        'entries': len(entered),
        'rejected': len(rejected),
        'wins': wins,
        'losses': len(entered) - wins,
        'win_rate': wins / len(entered) * 100.0,
        'avg_pnl_pct': sum(pnls) / len(pnls),
        'sum_usd': sum(usds),
        'by_reason': {
            k: {'n': len(v), 'sum_usd': sum(float(r.get('pnl_usd') or 0.0) for r in v)}
            for k, v in by_reason.items()
        },
        'by_path': {
            k: {'n': len(v), 'sum_usd': sum(float(r.get('pnl_usd') or 0.0) for r in v)}
            for k, v in by_path.items()
        },
        'rejected_by_reason': dict(Counter(r.get('reject_reason') for r in rejected)),
    }


def compare(baseline_agg: dict, candidate_agg: dict) -> dict:
    return {
        'pnl_diff_usd': float(candidate_agg.get('sum_usd', 0.0)) - float(baseline_agg.get('sum_usd', 0.0)),
        'entries_diff': int(candidate_agg.get('entries', 0)) - int(baseline_agg.get('entries', 0)),
        'win_rate_diff': float(candidate_agg.get('win_rate', 0.0)) - float(baseline_agg.get('win_rate', 0.0)),
    }