"""
Manual close helper for an open position.

Usage:
  python tools/manual_close.py SYMBOL
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from mc_position_manager import CHAIN_CONFIGS, DISABLED_CHAIN_CONFIGS, MultichainPositionManager, PortfolioConfig
from trading.executor import ManualExitReason, TradeExecutor
from trading.notifier import TelegramTradeNotifier
from trading.safety import SafetyCircuitBreaker


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")
        return

    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", help="Open position symbol to close")
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()

    portfolio_cfg = PortfolioConfig(
        chain_allocation={
            "bsc": float(os.getenv("CHAIN_ALLOCATION_BSC", "0.55")),
            "solana": float(os.getenv("CHAIN_ALLOCATION_SOLANA", "0.45")),
            "base": float(os.getenv("CHAIN_ALLOCATION_BASE", "0.00")),
        }
    )
    chain_configs = dict(CHAIN_CONFIGS)
    chain_configs.update(DISABLED_CHAIN_CONFIGS)
    pm = MultichainPositionManager(
        chain_configs,
        portfolio_cfg,
        storage_path=str(BASE_DIR / "data" / "positions.jsonl"),
    )
    position = next((pos for pos in pm.positions.values() if pos.symbol.upper() == symbol), None)
    if position is None:
        print(f"Open position not found: {symbol}")
        return

    safety = SafetyCircuitBreaker(
        BASE_DIR / "data" / "trading_safety_state.json",
        mode=os.getenv("TRADING_MODE", "live"),
        daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT_USD", "100")),
        max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "8")),
        max_position_size_usd=float(os.getenv("MAX_POSITION_SIZE_USD", "50")),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "30")),
        halt_duration_hours_first=int(os.getenv("HALT_DURATION_HOURS_FIRST", "1")),
        halt_duration_hours_second=int(os.getenv("HALT_DURATION_HOURS_SECOND", "4")),
        halt_duration_hours_third=int(os.getenv("HALT_DURATION_HOURS_THIRD", "24")),
        loss_window_hours=float(os.getenv("LOSS_WINDOW_HOURS", "1")),
    )
    notifier = TelegramTradeNotifier(
        bot_token=os.getenv("TELEGRAM_TRADING_BOT_TOKEN"),
        chat_id=os.getenv("TELEGRAM_TRADING_CHAT_ID"),
        state_path=BASE_DIR / "data" / "telegram_trade_state.json",
        auto_approve_on_timeout=os.getenv("AUTO_APPROVE_ON_TIMEOUT", "true").strip().lower() in {"1", "true", "yes", "on"},
    )
    executor = TradeExecutor(
        pm,
        notifier,
        safety,
        mode=os.getenv("TRADING_MODE", "live"),
        state_path=BASE_DIR / "data" / "trade_executor_state.json",
    )

    result = executor.handle_exit_signal(position, ManualExitReason.MANUAL_STOP)
    print(result)


if __name__ == "__main__":
    main()
