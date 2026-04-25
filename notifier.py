"""Telegram notification formatting helpers for live trading."""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote_plus


def format_price(price: float) -> str:
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:,.5f}"
    if price >= 0.0001:
        return f"${price:,.6f}"
    return f"${price:,.8f}"


def format_usd_compact(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def gecko_url(chain: str, address: str) -> str:
    return f"https://www.geckoterminal.com/{quote_plus(chain)}/pools/{quote_plus(address)}"


def pancake_url(contract: str) -> str:
    return f"https://pancakeswap.finance/swap?outputCurrency={quote_plus(contract)}"


def jupiter_url(contract: str) -> str:
    return f"https://jup.ag/swap/SOL-{quote_plus(contract)}"


def uniswap_base_url(contract: str) -> str:
    return f"https://app.uniswap.org/swap?chain=base&outputCurrency={quote_plus(contract)}"


def swap_url(chain: str, contract: str) -> str:
    chain = chain.lower()
    if chain == "bsc":
        return pancake_url(contract)
    if chain == "solana":
        return jupiter_url(contract)
    return uniswap_base_url(contract)


def compute_levels(entry_price: float, params) -> dict[str, float]:
    stop_price = entry_price * (1 + params.stop_loss_pct / 100.0)
    target_price = entry_price * (1 + params.take_profit_pct / 100.0)
    return {
        "stop_price": stop_price,
        "target_price": target_price,
    }


def format_entry_alert(snapshot: dict, chain: str, position_size_usd: float, params) -> str:
    entry_price = float(snapshot.get("price_usd") or 0.0)
    levels = compute_levels(entry_price, params)
    contract = snapshot.get("contract_address") or ""
    gecko_target = snapshot.get("pool_address") or contract
    lines = [
        f"🎯 <b>ENTRY SIGNAL [{escape(chain.upper())}]</b>",
        f"Symbol: <code>{escape(snapshot.get('symbol', '?'))}</code>",
        f"Price: {format_price(entry_price)}",
        f"1h change: {snapshot.get('price_change_1h_pct', 0):+.1f}%",
        f"Liquidity: {format_usd_compact(float(snapshot.get('liquidity_usd') or 0.0))}",
        f"Volume 1h: {format_usd_compact(float(snapshot.get('volume_1h_usd') or 0.0))}",
        f"Recommended: ${position_size_usd:.0f} ({chain.upper()} standard size)",
        f"⚠️ Stop loss: {params.stop_loss_pct:+.0f}% = {format_price(levels['stop_price'])}",
        f"🎯 Take profit: {params.take_profit_pct:+.0f}% = {format_price(levels['target_price'])} (Profit Lock active)",
        f"⏱️ Max hold: {params.max_hold_hours}h",
        "Quick links:",
        f"GeckoTerminal: {gecko_url(chain, gecko_target)}",
        f"{'PancakeSwap' if chain == 'bsc' else 'Jupiter' if chain == 'solana' else 'Uniswap'}: {swap_url(chain, contract)}",
    ]
    return "\n".join(lines)


def format_exit_alert(pos, reason, current_price: float, current_timestamp: datetime) -> str:
    hold_hours = pos.hold_hours
    pnl_pct = 0.0 if pos.entry_price == 0 else (current_price / pos.entry_price - 1.0) * 100.0
    contract = getattr(pos, "contract_address", "") or ""
    action_name = "PancakeSwap" if pos.chain == "bsc" else "Jupiter" if pos.chain == "solana" else "Uniswap"
    lines = [
        f"🚨 <b>EXIT SIGNAL [{escape(pos.chain.upper())}]</b>",
        f"Symbol: <code>{escape(pos.symbol)}</code>",
        f"Reason: {escape(reason.value.lower() if hasattr(reason, 'value') else str(reason).lower())}",
        f"Entry was: {format_price(pos.entry_price)} ({hold_hours:.1f}h ago)",
        f"Current: {format_price(current_price)}",
        f"Realized PnL: {pnl_pct:+.1f}% (if you sell now)",
        f"Action: Sell on {action_name}",
    ]
    if contract:
        lines.append(swap_url(pos.chain, contract))
    return "\n".join(lines)


def format_summary_alert(
    *,
    open_positions: list,
    realized_today_usd: float,
    cash_available_usd: float,
    as_of: datetime,
) -> str:
    best_line = "Best: n/a"
    worst_line = "Worst: n/a"
    if open_positions:
        ranked = sorted(open_positions, key=lambda pos: pos.unrealized_pnl_pct, reverse=True)
        best = ranked[0]
        worst = ranked[-1]
        best_line = f"Best: {best.symbol} {best.unrealized_pnl_pct:+.0f}%"
        worst_line = f"Worst: {worst.symbol} {worst.unrealized_pnl_pct:+.0f}%"

    lines = [
        "📊 <b>4h SUMMARY</b>",
        f"Open positions: {len(open_positions)}",
        f"Total realized today: {realized_today_usd:+.2f}",
        best_line,
        worst_line,
        f"Cash available: ${cash_available_usd:.0f}",
        f"As of: {as_of.strftime('%Y-%m-%d %H:%M')}",
    ]
    return "\n".join(lines)
