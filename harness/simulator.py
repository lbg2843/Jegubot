from datetime import datetime

from .load_data import Candidate, Outcome, Snapshot, parse_iso_utc
from .rule_engine import passes_entry_gates


def parse_iso(s: str) -> datetime:
    txt = str(s).strip().replace('Z', '').replace('+00:00', '')
    try:
        return datetime.fromisoformat(txt)
    except Exception:
        return datetime.fromisoformat(txt.split('.')[0])


def parse_hours(start_iso: str, end_iso: str) -> float:
    start = parse_iso_utc(start_iso)
    end = parse_iso_utc(end_iso)
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _close(reason: str, hold_h: float, pnl_pct: float, size: float, cand: Candidate, peak_pct: float = 0.0) -> dict:
    return {
        'entered': True,
        'reject_reason': None,
        'exit_reason': reason,
        'hold_hours': hold_h,
        'pnl_pct': pnl_pct,
        'pnl_usd': size * pnl_pct / 100.0,
        'peak_pct': peak_pct,
        'path': cand.entry_path,
        'symbol': cand.symbol,
        'chain': cand.chain,
        'snapshot_id': cand.snapshot_id,
        'entry_timestamp': cand.timestamp,
    }


def simulate_trade(cand: Candidate, outcomes: list[Outcome], rules: dict) -> dict:
    """Deprecated v1 checkpoint simulator. Use simulate_trade_v2()."""
    passes, reason = passes_entry_gates(cand, rules)
    if not passes:
        return {
            'entered': False,
            'reject_reason': reason,
            'path': cand.entry_path,
            'symbol': cand.symbol,
            'chain': cand.chain,
            'snapshot_id': cand.snapshot_id,
        }

    slippage = float(rules['slippage_pct'])
    entry_price = float(cand.price_usd or 0.0)
    size = float(rules['position_size_usd'])
    if entry_price <= 0:
        return _close('NO_DATA', 0.0, 0.0, size, cand)

    valid = [o for o in outcomes if str(o.fetch_status).lower() == 'ok' and float(o.current_price or 0.0) > 0]
    valid.sort(key=lambda o: parse_iso_utc(o.checked_at) or parse_iso_utc(o.timestamp))

    max_hold_h = rules['base_max_hold_hours'] if cand.chain == 'base' else (
        rules['solana_max_hold_hours'] if cand.chain == 'solana' else rules['bsc_max_hold_hours']
    )

    peak_pct = 0.0
    profit_locked = False
    early_window_h = float(rules['early_stop_window_min']) / 60.0

    for outcome in valid:
        hold_h = parse_hours(cand.timestamp, outcome.checked_at)
        current_price = float(outcome.current_price or 0.0)
        pnl_pct_raw = ((current_price / entry_price) - 1.0) * 100.0
        pnl_pct = pnl_pct_raw - (slippage * 2.0)
        peak_pct = max(peak_pct, pnl_pct)

        if hold_h <= max(1.0, early_window_h) and pnl_pct <= rules['early_stop_threshold_pct']:
            return _close('EARLY_STOP_LOSS', min(hold_h, early_window_h), pnl_pct, size, cand, peak_pct)
        if pnl_pct <= rules['stop_loss_pct']:
            return _close('STOP_LOSS', hold_h, pnl_pct, size, cand, peak_pct)
        if peak_pct >= rules['profit_lock_threshold_pct']:
            profit_locked = True
        if profit_locked and pnl_pct <= peak_pct + rules['trailing_stop_pct']:
            return _close('PROFIT_LOCK_BREAK', hold_h, pnl_pct, size, cand, peak_pct)
        if peak_pct > 5.0 and pnl_pct <= peak_pct + rules['trailing_stop_pct']:
            return _close('TRAILING_STOP', hold_h, pnl_pct, size, cand, peak_pct)
        if hold_h >= max_hold_h:
            return _close('TIME_EXIT', hold_h, pnl_pct, size, cand, peak_pct)

    if valid:
        last = valid[-1]
        hold_h = parse_hours(cand.timestamp, last.checked_at)
        current_price = float(last.current_price or 0.0)
        pnl_pct = ((current_price / entry_price) - 1.0) * 100.0 - (slippage * 2.0)
        return _close('HORIZON_END', hold_h, pnl_pct, size, cand, peak_pct)

    return _close('NO_DATA', 0.0, 0.0, size, cand)


def simulate_trade_v2(cand: Candidate, token_snapshots: list[Snapshot], rules: dict, eth_4h_change: float = 0.0) -> dict:
    """
    raw snapshot time-series simulator.
    Limits:
    - raw snapshots are roughly 60s cadence, but data can stop when token leaves trending
    - LIQUIDITY_CRASH is not simulated yet
    - fixed slippage 0.5%
    """
    passes, reason = passes_entry_gates(cand, rules, eth_4h_change)
    if not passes:
        return {
            'entered': False,
            'reject_reason': reason,
            'path': cand.entry_path,
            'symbol': cand.symbol,
            'chain': cand.chain,
            'snapshot_id': cand.snapshot_id,
        }

    entry_ts = parse_iso(cand.timestamp)
    future_snaps = [s for s in token_snapshots if parse_iso(s.timestamp) > entry_ts]
    if not future_snaps:
        return _close('NO_DATA', 0.0, 0.0, float(rules.get('position_size_usd', 50.0)), cand)

    slippage = float(rules.get('slippage_pct', 0.5))
    entry_price = float(cand.price_usd or 0.0) * (1.0 + slippage / 100.0)
    size = float(rules.get('position_size_usd', 50.0))
    if entry_price <= 0:
        return _close('NO_DATA', 0.0, 0.0, size, cand)

    peak_pct = 0.0
    profit_locked = False
    max_hold = rules['base_max_hold_hours'] if cand.chain == 'base' else (
        rules['solana_max_hold_hours'] if cand.chain == 'solana' else rules['bsc_max_hold_hours']
    )

    for snap in future_snaps:
        if float(snap.price_usd or 0.0) <= 0:
            continue
        snap_ts = parse_iso(snap.timestamp)
        hold_minutes = (snap_ts - entry_ts).total_seconds() / 60.0
        hold_hours = hold_minutes / 60.0
        pnl_pct = ((float(snap.price_usd) / entry_price) - 1.0) * 100.0 - slippage
        peak_pct = max(peak_pct, pnl_pct)

        if hold_minutes <= float(rules['early_stop_window_min']):
            if pnl_pct <= float(rules['early_stop_threshold_pct']):
                return _close('EARLY_STOP_LOSS', hold_hours, pnl_pct, size, cand, peak_pct)

        if pnl_pct <= float(rules['stop_loss_pct']):
            return _close('STOP_LOSS', hold_hours, pnl_pct, size, cand, peak_pct)

        if peak_pct >= float(rules['profit_lock_threshold_pct']):
            profit_locked = True

        if profit_locked:
            if pnl_pct <= peak_pct + float(rules['trailing_stop_pct']):
                return _close('PROFIT_LOCK_BREAK', hold_hours, pnl_pct, size, cand, peak_pct)
        elif peak_pct >= 5.0:
            if pnl_pct <= peak_pct + float(rules['trailing_stop_pct']):
                return _close('TRAILING_STOP', hold_hours, pnl_pct, size, cand, peak_pct)

        if hold_hours >= float(max_hold):
            return _close('TIME_EXIT', hold_hours, pnl_pct, size, cand, peak_pct)

    last = future_snaps[-1]
    final_pnl = ((float(last.price_usd) / entry_price) - 1.0) * 100.0 - slippage
    last_hours = (parse_iso(last.timestamp) - entry_ts).total_seconds() / 3600.0
    return _close('HORIZON_END', last_hours, final_pnl, size, cand, peak_pct)