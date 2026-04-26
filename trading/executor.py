import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


log = logging.getLogger("trading.executor")


class ManualExitReason(str, Enum):
    MANUAL_STOP = "manual_stop"


class TradeExecutor:
    def __init__(self, position_manager, notifier, safety, mode: str = "dry_run", state_path: str | Path | None = None):
        self.position_manager = position_manager
        self.notifier = notifier
        self.safety = safety
        self.mode = mode
        self.state_path = Path(state_path) if state_path else None
        self._state = {"pending_entries": {}}
        self._bsc_wallet = None
        self._sol_wallet = None
        self._load_state()
        if self.mode != "dry_run":
            log.warning("live mode is not implemented yet; executor will refuse live trades")

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state.setdefault("pending_entries", {})
        except Exception:
            self._state = {"pending_entries": {}}

    def _save_state(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _signal_key(self, signal: dict) -> str:
        chain = signal.get("chain", "?")
        contract = signal.get("contract_address") or signal.get("symbol") or "unknown"
        return f"{chain}:{contract}"

    def _open_position_exists(self, signal: dict) -> bool:
        return self._signal_key(signal) in self.position_manager.positions

    def _finalize_pending(self, signal_key: str):
        self._state["pending_entries"].pop(signal_key, None)
        self._save_state()

    def submit_entry_signal(self, signal: dict):
        amount_usd = float(signal.get("position_size_usd", 0.0))
        chain = signal.get("chain", "?")
        signal_key = self._signal_key(signal)

        if self._open_position_exists(signal):
            return {"status": "duplicate_open", "signal_key": signal_key}
        if signal_key in self._state["pending_entries"]:
            return {"status": "already_pending", "signal_key": signal_key}

        allowed, reason = self.safety.can_execute(chain, amount_usd)
        if not allowed:
            log.warning("trade blocked by safety: %s", reason)
            return {"status": "blocked", "reason": reason}

        honeypot_result = self._run_honeypot_check(signal)
        if honeypot_result is not None and not honeypot_result.is_safe:
            symbol = signal.get("symbol", "?")
            self.notifier.send_emergency(
                f"🛡 Honeypot 의심: {symbol}\n"
                f"Layer: {honeypot_result.layer}\n"
                f"Reason: {honeypot_result.reason}"
            )
            self._log_honeypot_block(signal, honeypot_result)
            return {
                "status": "blocked",
                "reason": honeypot_result.reason,
                "layer": honeypot_result.layer,
            }

        signal_id = self.notifier.send_entry_signal(signal)
        if not self.notifier.last_send_ok:
            self.notifier.mark_decision(signal_id, "send_failed")
            return {"status": "failed", "reason": "send_failed", "signal_id": signal_id}

        self._state["pending_entries"][signal_key] = {
            "signal_id": signal_id,
            "queued_at": datetime.utcnow().isoformat(),
            "approval_timeout": int(signal.get("approval_timeout", 300)),
            "signal": signal,
        }
        self._save_state()
        return {"status": "queued", "signal_id": signal_id, "signal_key": signal_key}

    def process_pending_entries(self) -> list[dict]:
        results = []
        pending_items = list(self._state.get("pending_entries", {}).items())
        now = datetime.utcnow()

        for signal_key, item in pending_items:
            signal = item.get("signal") or {}
            signal_id = item.get("signal_id")
            decision = self.notifier.get_decision(signal_id)

            if not decision:
                try:
                    queued_at = datetime.fromisoformat(item.get("queued_at", now.isoformat()))
                except Exception:
                    queued_at = now
                elapsed = (now - queued_at).total_seconds()
                if elapsed >= int(item.get("approval_timeout", 300)):
                    decision = self.notifier.resolve_timeout(signal_id)
                else:
                    results.append({"status": "pending", "signal_id": signal_id, "signal_key": signal_key})
                    continue

            if decision == "send_failed":
                log.warning("approval message failed to send for signal %s", signal_id)
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": "send_failed", "signal_id": signal_id, "signal_key": signal_key})
                continue

            if decision not in {"approved", "auto_approved"}:
                log.info("user rejected signal %s (%s)", signal_id, decision)
                self._finalize_pending(signal_key)
                results.append({"status": "rejected", "reason": decision, "signal_id": signal_id, "signal_key": signal_key})
                continue

            if self.mode != "dry_run":
                self._finalize_pending(signal_key)
                results.append({"status": "rejected", "reason": "live_not_implemented", "signal_id": signal_id, "signal_key": signal_key})
                continue

            if self._open_position_exists(signal):
                self._finalize_pending(signal_key)
                results.append({"status": "duplicate_open", "signal_id": signal_id, "signal_key": signal_key})
                continue

            amount_usd = float(signal.get("position_size_usd", 0.0))
            chain = signal.get("chain", "?")
            allowed, reason = self.safety.can_execute(chain, amount_usd)
            if not allowed:
                log.warning("trade blocked by safety at execution time: %s", reason)
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": reason, "signal_id": signal_id, "signal_key": signal_key})
                continue

            buy_result = self._simulate_buy(signal)
            if not buy_result["ok"]:
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": buy_result["reason"], "signal_id": signal_id, "signal_key": signal_key})
                continue

            cfg = self.position_manager.chain_configs[chain]
            if cfg.capital_per_position_pct <= 0:
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": "invalid_chain_position_pct", "signal_id": signal_id, "signal_key": signal_key})
                continue

            effective_total_capital_usd = amount_usd / (cfg.capital_per_position_pct / 100.0)
            position = self.position_manager.open_position(
                chain,
                signal,
                total_capital_usd=effective_total_capital_usd,
            )
            if position is None:
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": "position_manager_rejected", "signal_id": signal_id, "signal_key": signal_key})
                continue

            self.safety.record_trade_executed()
            self._finalize_pending(signal_key)
            results.append({"status": "executed", "position": position, "approval": decision, "signal_id": signal_id, "signal_key": signal_key})

        return results

    def handle_entry_signal(self, signal: dict):
        return self.submit_entry_signal(signal)

    def _get_live_wallet(self, chain: str):
        chain = (chain or "").lower()
        if chain == "bsc":
            if self._bsc_wallet is None:
                from .wallet_bsc import BSCWallet

                self._bsc_wallet = BSCWallet()
            return self._bsc_wallet

        if chain == "solana":
            if self._sol_wallet is None:
                from .wallet_solana import SolanaWallet

                self._sol_wallet = SolanaWallet()
            return self._sol_wallet

        return None

    def _run_honeypot_check(self, signal: dict):
        if self.mode != "live":
            return None

        chain = str(signal.get("chain", "")).lower()
        token_address = signal.get("contract_address") or signal.get("contract")
        if not token_address:
            log.warning("honeypot check skipped: missing contract address for %s", signal.get("symbol", "?"))
            return None

        from .honeypot import HoneypotChecker

        wallet = self._get_live_wallet(chain)
        checker = HoneypotChecker(chain, wallet)
        return checker.check(
            str(token_address),
            test_amount_usd=5.0,
            max_friction_pct=15.0,
        )

    def _log_honeypot_block(self, signal: dict, result) -> None:
        log.warning(
            "honeypot blocked entry: [%s] %s %s layer=%s reason=%s details=%s",
            signal.get("chain", "?"),
            signal.get("symbol", "?"),
            signal.get("contract_address") or signal.get("contract") or "?",
            result.layer,
            result.reason,
            result.details,
        )

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
