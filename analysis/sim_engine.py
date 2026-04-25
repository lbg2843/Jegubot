"""Unified trade simulation used by all analysis scripts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from mc_position_manager import CHAIN_CONFIGS


ANALYSIS_TOTAL_CAPITAL_USD = 10_000.0


@dataclass
class TradeResult:
    symbol: str
    chain: str
    entry_timestamp: datetime
    exit_timestamp: Optional[datetime]
    entry_price: float
    exit_price: Optional[float]
    peak_price: float
    position_size_usd: float
    pnl_pct: Optional[float]
    pnl_usd: Optional[float]
    exit_reason: str
    hold_hours: Optional[float]
    max_drawdown_pct: float
    profit_locked: bool = False


@dataclass
class SimParams:
    """All parameters in one place. Default from CHAIN_CONFIGS."""

    stop_loss_pct: float
    trailing_stop_pct: float
    trailing_activation_pct: float
    take_profit_pct: float
    max_hold_hours: int
    slippage_pct: float = 0.0
    gas_fee_usd: float = 0.0
    rug_as_total_loss: bool = False
    profit_lock_enabled: bool = True


def _all_chain_configs() -> dict:
    from mc_position_manager import DISABLED_CHAIN_CONFIGS

    return {**CHAIN_CONFIGS, **DISABLED_CHAIN_CONFIGS}


def params_for_chain(chain: str) -> SimParams:
    cfg = _all_chain_configs()[chain.lower()]
    return SimParams(
        stop_loss_pct=cfg.stop_loss_pct,
        trailing_stop_pct=cfg.trailing_stop_pct,
        trailing_activation_pct=cfg.trailing_activation_pct,
        take_profit_pct=cfg.take_profit_pct,
        max_hold_hours=cfg.max_hold_hours,
    )


def position_size_usd_for_chain(chain: str, total_capital_usd: float = ANALYSIS_TOTAL_CAPITAL_USD) -> float:
    cfg = _all_chain_configs()[chain.lower()]
    return total_capital_usd * (cfg.capital_per_position_pct / 100.0)


def detect_rug(entry_snapshot: dict, current_snapshot: dict) -> bool:
    entry_liquidity = float(entry_snapshot.get("liquidity_usd") or 0.0)
    current_liquidity = float(current_snapshot.get("liquidity_usd") or 0.0)
    if entry_liquidity > 0 and current_liquidity <= entry_liquidity * 0.1:
        return True

    entry_holders = int(entry_snapshot.get("holders_total") or entry_snapshot.get("holders_binance") or 0)
    current_holders = int(current_snapshot.get("holders_total") or current_snapshot.get("holders_binance") or 0)
    return entry_holders > 0 and current_holders <= max(1, int(entry_holders * 0.2))


def _pct_change(current_price: float, base_price: float) -> float:
    if base_price <= 0:
        return 0.0
    return (current_price / base_price - 1.0) * 100.0


def _apply_entry_slippage(price: float, slippage_pct: float) -> float:
    return price * (1.0 + slippage_pct / 100.0)


def _apply_exit_slippage(price: float, slippage_pct: float) -> float:
    return price * (1.0 - slippage_pct / 100.0)


def _build_open_result(
    entry_snapshot: dict,
    last_snapshot: dict | None,
    entry_price: float,
    peak_price: float,
    position_size_usd: float,
    max_drawdown_pct: float,
    profit_locked: bool,
    slippage_pct: float,
) -> TradeResult:
    exit_price = None
    pnl_pct = None
    pnl_usd = None
    hold_hours = None
    if last_snapshot is not None and last_snapshot.get("_ts") is not None:
        exit_price = _apply_exit_slippage(float(last_snapshot.get("_price") or 0.0), slippage_pct)
        pnl_pct = _pct_change(exit_price, entry_price)
        pnl_usd = position_size_usd * (pnl_pct / 100.0)
        hold_hours = (last_snapshot["_ts"] - entry_snapshot["_ts"]).total_seconds() / 3600.0

    return TradeResult(
        symbol=entry_snapshot["symbol"],
        chain=entry_snapshot["chain"],
        entry_timestamp=entry_snapshot["_ts"],
        exit_timestamp=None,
        entry_price=entry_price,
        exit_price=exit_price,
        peak_price=peak_price,
        position_size_usd=position_size_usd,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        exit_reason="open",
        hold_hours=hold_hours,
        max_drawdown_pct=max_drawdown_pct,
        profit_locked=profit_locked,
    )


def _build_closed_result(
    *,
    entry_snapshot: dict,
    entry_price: float,
    exit_snapshot: dict,
    exit_price: float,
    peak_price: float,
    position_size_usd: float,
    exit_reason: str,
    max_drawdown_pct: float,
    gas_fee_usd: float,
    profit_locked: bool,
) -> TradeResult:
    pnl_pct = _pct_change(exit_price, entry_price)
    pnl_usd = position_size_usd * (pnl_pct / 100.0) - gas_fee_usd
    hold_hours = (exit_snapshot["_ts"] - entry_snapshot["_ts"]).total_seconds() / 3600.0
    return TradeResult(
        symbol=entry_snapshot["symbol"],
        chain=entry_snapshot["chain"].lower(),
        entry_timestamp=entry_snapshot["_ts"],
        exit_timestamp=exit_snapshot["_ts"],
        entry_price=entry_price,
        exit_price=exit_price,
        peak_price=peak_price,
        position_size_usd=position_size_usd,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        exit_reason=exit_reason,
        hold_hours=hold_hours,
        max_drawdown_pct=max_drawdown_pct,
        profit_locked=profit_locked,
    )


def simulate_trade(
    entry_snapshot: dict,
    future_snapshots: list[dict],
    params: SimParams,
) -> TradeResult:
    """
    Tick-by-tick simulation using one shared rule set.
    """
    raw_entry_price = float(entry_snapshot.get("price_usd") or 0.0)
    entry_price = _apply_entry_slippage(raw_entry_price, params.slippage_pct)
    chain = entry_snapshot["chain"].lower()
    position_size_usd = float(
        entry_snapshot.get("_position_size_usd")
        or position_size_usd_for_chain(chain)
    )

    if entry_price <= 0:
        return TradeResult(
            symbol=entry_snapshot["symbol"],
            chain=chain,
            entry_timestamp=entry_snapshot["_ts"],
            exit_timestamp=None,
            entry_price=entry_price,
            exit_price=None,
            peak_price=entry_price,
            position_size_usd=position_size_usd,
            pnl_pct=None,
            pnl_usd=None,
            exit_reason="open",
            hold_hours=None,
            max_drawdown_pct=0.0,
        )

    peak_price = entry_price
    max_drawdown_pct = 0.0
    profit_locked = False
    last_snapshot = future_snapshots[-1] if future_snapshots else None
    max_hold_ts = entry_snapshot["_ts"] + timedelta(hours=params.max_hold_hours)
    take_profit_price = entry_price * (1.0 + params.take_profit_pct / 100.0)
    stop_loss_price = entry_price * (1.0 + params.stop_loss_pct / 100.0)

    for snapshot in future_snapshots:
        market_price = float(snapshot.get("_price") or 0.0)
        if market_price <= 0:
            continue

        effective_exit_price = _apply_exit_slippage(market_price, params.slippage_pct)
        peak_price = max(peak_price, effective_exit_price)
        current_pnl_pct = _pct_change(effective_exit_price, entry_price)
        peak_pnl_pct = _pct_change(peak_price, entry_price)
        drawdown_pct = _pct_change(effective_exit_price, peak_price)
        max_drawdown_pct = min(max_drawdown_pct, drawdown_pct)

        if effective_exit_price <= stop_loss_price:
            return _build_closed_result(
                entry_snapshot=entry_snapshot,
                entry_price=entry_price,
                exit_snapshot=snapshot,
                exit_price=stop_loss_price,
                peak_price=peak_price,
                position_size_usd=position_size_usd,
                exit_reason="stop_loss",
                max_drawdown_pct=max_drawdown_pct,
                gas_fee_usd=params.gas_fee_usd,
                profit_locked=profit_locked,
            )

        trailing_active = peak_pnl_pct >= params.trailing_activation_pct

        if params.profit_lock_enabled:
            if not profit_locked and effective_exit_price >= take_profit_price:
                profit_locked = True

            if profit_locked:
                if effective_exit_price < take_profit_price:
                    return _build_closed_result(
                        entry_snapshot=entry_snapshot,
                        entry_price=entry_price,
                        exit_snapshot=snapshot,
                        exit_price=max(effective_exit_price, take_profit_price),
                        peak_price=peak_price,
                        position_size_usd=position_size_usd,
                        exit_reason="profit_lock_break",
                        max_drawdown_pct=max_drawdown_pct,
                        gas_fee_usd=params.gas_fee_usd,
                        profit_locked=True,
                    )
                if trailing_active:
                    trail_stop = peak_price * (1.0 - params.trailing_stop_pct / 100.0)
                    if effective_exit_price <= trail_stop:
                        return _build_closed_result(
                            entry_snapshot=entry_snapshot,
                            entry_price=entry_price,
                            exit_snapshot=snapshot,
                            exit_price=max(trail_stop, take_profit_price),
                            peak_price=peak_price,
                            position_size_usd=position_size_usd,
                            exit_reason="trailing_stop_post_target",
                            max_drawdown_pct=max_drawdown_pct,
                            gas_fee_usd=params.gas_fee_usd,
                            profit_locked=True,
                        )
            elif trailing_active and drawdown_pct <= -params.trailing_stop_pct:
                trail_stop = peak_price * (1.0 - params.trailing_stop_pct / 100.0)
                return _build_closed_result(
                    entry_snapshot=entry_snapshot,
                    entry_price=entry_price,
                    exit_snapshot=snapshot,
                    exit_price=trail_stop,
                    peak_price=peak_price,
                    position_size_usd=position_size_usd,
                    exit_reason="trailing_stop",
                    max_drawdown_pct=max_drawdown_pct,
                    gas_fee_usd=params.gas_fee_usd,
                    profit_locked=False,
                )
        else:
            if trailing_active and drawdown_pct <= -params.trailing_stop_pct:
                trail_stop = peak_price * (1.0 - params.trailing_stop_pct / 100.0)
                return _build_closed_result(
                    entry_snapshot=entry_snapshot,
                    entry_price=entry_price,
                    exit_snapshot=snapshot,
                    exit_price=trail_stop,
                    peak_price=peak_price,
                    position_size_usd=position_size_usd,
                    exit_reason="trailing_stop",
                    max_drawdown_pct=max_drawdown_pct,
                    gas_fee_usd=params.gas_fee_usd,
                    profit_locked=False,
                )
            if effective_exit_price >= take_profit_price:
                return _build_closed_result(
                    entry_snapshot=entry_snapshot,
                    entry_price=entry_price,
                    exit_snapshot=snapshot,
                    exit_price=take_profit_price,
                    peak_price=peak_price,
                    position_size_usd=position_size_usd,
                    exit_reason="take_profit",
                    max_drawdown_pct=max_drawdown_pct,
                    gas_fee_usd=params.gas_fee_usd,
                    profit_locked=False,
                )

        if snapshot["_ts"] >= max_hold_ts:
            return _build_closed_result(
                entry_snapshot=entry_snapshot,
                entry_price=entry_price,
                exit_snapshot=snapshot,
                exit_price=effective_exit_price,
                peak_price=peak_price,
                position_size_usd=position_size_usd,
                exit_reason="time_exit",
                max_drawdown_pct=max_drawdown_pct,
                gas_fee_usd=params.gas_fee_usd,
                profit_locked=profit_locked,
            )

        if params.rug_as_total_loss and detect_rug(entry_snapshot, snapshot):
            return _build_closed_result(
                entry_snapshot=entry_snapshot,
                entry_price=entry_price,
                exit_snapshot=snapshot,
                exit_price=0.0,
                peak_price=peak_price,
                position_size_usd=position_size_usd,
                exit_reason="rug",
                max_drawdown_pct=max_drawdown_pct,
                gas_fee_usd=params.gas_fee_usd,
                profit_locked=profit_locked,
            )

    return _build_open_result(
        entry_snapshot=entry_snapshot,
        last_snapshot=last_snapshot,
        entry_price=entry_price,
        peak_price=peak_price,
        position_size_usd=position_size_usd,
        max_drawdown_pct=max_drawdown_pct,
        profit_locked=profit_locked,
        slippage_pct=params.slippage_pct,
    )
