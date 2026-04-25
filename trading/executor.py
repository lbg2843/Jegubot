import logging
from datetime import datetime
from enum import Enum


log = logging.getLogger("trading.executor")


class ManualExitReason(str, Enum):
    MANUAL_STOP = "manual_stop"


class TradeExecutor:
    def __init__(self, position_manager, notifier, safety, mode: str = "dry_run"):
        self.position_manager = position_manager
        self.notifier = notifier
        self.safety = safety
        self.mode = mode
        if self.mode != "dry_run":
            log.warning("live mode is not implemented yet; executor will refuse live trades")

    def handle_entry_signal(self, signal: dict):
        amount_usd = float(signal.get("position_size_usd", 0.0))
        chain = signal.get("chain", "?")
        allowed, reason = self.safety.can_execute(chain, amount_usd)
        if not allowed:
            log.warning("trade blocked by safety: %s", reason)
            return {"status": "blocked", "reason": reason}

        signal_id = self.notifier.send_entry_signal(signal)
        decision = self.notifier.wait_for_approval(signal_id, timeout=int(signal.get("approval_timeout", 300)))
        if decision == "send_failed":
            log.warning("approval message failed to send for signal %s", signal_id)
            return {"status": "failed", "reason": "send_failed"}
        if decision not in {"approved", "auto_approved"}:
            log.info("user rejected signal %s (%s)", signal_id, decision)
            return {"status": "rejected", "reason": decision}

        if self.mode != "dry_run":
            return {"status": "rejected", "reason": "live_not_implemented"}

        buy_result = self._simulate_buy(signal)
        if not buy_result["ok"]:
            return {"status": "failed", "reason": buy_result["reason"]}

        cfg = self.position_manager.chain_configs[chain]
        if cfg.capital_per_position_pct <= 0:
            return {"status": "failed", "reason": "invalid_chain_position_pct"}
        effective_total_capital_usd = amount_usd / (cfg.capital_per_position_pct / 100.0)
        position = self.position_manager.open_position(
            chain,
            signal,
            total_capital_usd=effective_total_capital_usd,
        )
        if position is None:
            return {"status": "failed", "reason": "position_manager_rejected"}

        self.safety.record_trade_executed()
        return {"status": "executed", "position": position, "approval": decision}

    def handle_exit_signal(self, position, reason):
        if self.mode != "dry_run":
            return {"status": "rejected", "reason": "live_not_implemented"}

        sell_result = self._simulate_sell(position)
        if not sell_result["ok"]:
            return {"status": "failed", "reason": sell_result["reason"]}

        pnl_pct = position.unrealized_pnl_pct
        pnl_usd = position.size_usd * (pnl_pct / 100.0)
        self.safety.record_trade_closed(pnl_usd)
        closed = self.position_manager.close_position(position, reason, f"executor:{reason.value}")
        self.notifier.send_exit_notification(closed, reason.value, pnl_pct, pnl_usd)
        return {"status": "closed", "position": closed}

    def _simulate_buy(self, signal: dict) -> dict:
        log.info(
            "dry_run simulated buy: [%s] %s @ $%.6f size=$%.2f",
            signal.get("chain", "?"),
            signal.get("symbol", "?"),
            float(signal.get("price_usd", 0.0)),
            float(signal.get("position_size_usd", 0.0)),
        )
        return {"ok": True, "timestamp": datetime.utcnow().isoformat()}

    def _simulate_sell(self, position) -> dict:
        log.info(
            "dry_run simulated sell: [%s] %s @ $%.6f pnl=%+.2f%%",
            position.chain,
            position.symbol,
            float(position.current_price),
            float(position.unrealized_pnl_pct),
        )
        return {"ok": True, "timestamp": datetime.utcnow().isoformat()}

    def emergency_close_all(self):
        closed = 0
        for position in list(self.position_manager.positions.values()):
            self.handle_exit_signal(position, ManualExitReason.MANUAL_STOP)
            closed += 1
        self.notifier.send_emergency(f"manual stop closed {closed} open positions")
        return f"manual stop closed {closed} open positions"
