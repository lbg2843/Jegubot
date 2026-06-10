import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Optional

from .time_utils import iso_utc_now, parse_iso_utc, utc_now


log = logging.getLogger("trading.executor")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


class ManualExitReason(str, Enum):
    MANUAL_STOP = "manual_stop"


class TradeExecutor:
    def __init__(self, position_manager, notifier, safety, mode: str = "dry_run", state_path: str | Path | None = None):
        self.position_manager = position_manager
        self.notifier = notifier
        self.safety = safety
        self.mode = mode
        self.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "4"))
        self.wallet_balance_cache_ttl_sec = int(os.getenv("WALLET_BALANCE_CACHE_TTL_SEC", "45"))
        self.stop_loss_cooldown_minutes = int(os.getenv("STOP_LOSS_COOLDOWN_MINUTES", "60"))
        # DEPRECATED: reason별 차등 cooldown으로 대체. _get_cooldown_minutes_by_reason() 참조.
        self.reentry_cooldown_minutes = int(os.getenv("POSITION_REENTRY_COOLDOWN_MINUTES", "120"))
        self.state_path = Path(state_path) if state_path else None
        self._state = {
            "pending_entries": {},
            "exit_failures": {},
            "wallet_mismatches": {},
            "stop_loss_cooldowns": {},
            "reentry_cooldowns": {},
        }
        self._bsc_wallet = None
        self._sol_wallet = None
        self._wallet_balance_cache: dict[str, dict] = {}
        self._load_state()
        if self.mode == "live":
            log.info("live mode enabled for trade executor")

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state.setdefault("pending_entries", {})
            self._state.setdefault("exit_failures", {})
            self._state.setdefault("wallet_mismatches", {})
            self._state.setdefault("stop_loss_cooldowns", {})
            self._state.setdefault("reentry_cooldowns", {})
            self._prune_stop_loss_cooldowns(save=False)
            self._prune_reentry_cooldowns(save=False)
        except Exception:
            self._state = {
                "pending_entries": {},
                "exit_failures": {},
                "wallet_mismatches": {},
                "stop_loss_cooldowns": {},
                "reentry_cooldowns": {},
            }

    def _save_state(self):
        if not self.state_path:
            return
        _atomic_write_json(self.state_path, self._state)

    def _prune_stop_loss_cooldowns(self, save: bool = True) -> None:
        cooldowns = self._state.setdefault("stop_loss_cooldowns", {})
        ttl_seconds = max(0, int(self.stop_loss_cooldown_minutes)) * 60
        if ttl_seconds <= 0 or not cooldowns:
            return
        now = utc_now()
        removed = []
        for key, payload in list(cooldowns.items()):
            try:
                last_stop = parse_iso_utc(payload.get("last_stop_at"))
            except Exception:
                removed.append(key)
                continue
            if (now - last_stop).total_seconds() >= ttl_seconds:
                removed.append(key)
        for key in removed:
            cooldowns.pop(key, None)
        if removed and save:
            self._save_state()

    def _active_stop_loss_cooldown(self, signal: dict):
        self._prune_stop_loss_cooldowns(save=False)
        key = self._signal_key(signal)
        payload = self._state.setdefault("stop_loss_cooldowns", {}).get(key)
        if not payload:
            return None
        try:
            last_stop = parse_iso_utc(payload.get("last_stop_at"))
        except Exception:
            self._state["stop_loss_cooldowns"].pop(key, None)
            self._save_state()
            return None
        ttl_seconds = max(0, int(self.stop_loss_cooldown_minutes)) * 60
        elapsed = (utc_now() - last_stop).total_seconds()
        if elapsed >= ttl_seconds:
            self._state["stop_loss_cooldowns"].pop(key, None)
            self._save_state()
            return None
        payload = dict(payload)
        payload["remaining_seconds"] = max(0, int(ttl_seconds - elapsed))
        return payload

    def _get_cooldown_minutes_by_reason(self, reason: str) -> int:
        reason_env_map = {
            "STOP_LOSS": "COOLDOWN_STOP_LOSS_MINUTES",
            "LIQUIDITY_CRASH": "COOLDOWN_LIQUIDITY_CRASH_MINUTES",
            "TIME_EXIT": "COOLDOWN_TIME_EXIT_MINUTES",
            "TRAILING_STOP": "COOLDOWN_TRAILING_STOP_MINUTES",
            "TAKE_PROFIT": "COOLDOWN_TAKE_PROFIT_MINUTES",
            "PROFIT_LOCK_BREAK": "COOLDOWN_PROFIT_LOCK_BREAK_MINUTES",
            "HOLDER_EXODUS": "COOLDOWN_HOLDER_EXODUS_MINUTES",
        }
        env_key = reason_env_map.get(reason)
        if env_key:
            raw = os.getenv(env_key)
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass

        fallback = (
            os.getenv("COOLDOWN_DEFAULT_MINUTES")
            or os.getenv("POSITION_REENTRY_COOLDOWN_MINUTES")
            or "120"
        )
        try:
            return int(fallback)
        except ValueError:
            return 120

    def _prune_reentry_cooldowns(self, save: bool = True) -> None:
        cooldowns = self._state.setdefault("reentry_cooldowns", {})
        if not cooldowns:
            return
        now = utc_now()
        removed = []
        for key, payload in list(cooldowns.items()):
            try:
                last_exit = parse_iso_utc(payload.get("last_exit_at"))
            except Exception:
                removed.append(key)
                continue
            exit_reason = payload.get("exit_reason", "DEFAULT")
            cooldown_minutes = self._get_cooldown_minutes_by_reason(exit_reason)
            ttl_seconds = max(0, int(cooldown_minutes)) * 60
            if ttl_seconds <= 0:
                removed.append(key)
                continue
            if (now - last_exit).total_seconds() >= ttl_seconds:
                removed.append(key)
        for key in removed:
            cooldowns.pop(key, None)
        if removed and save:
            self._save_state()

    def _active_reentry_cooldown(self, signal: dict):
        self._prune_reentry_cooldowns(save=False)
        key = self._signal_key(signal)
        payload = self._state.setdefault("reentry_cooldowns", {}).get(key)
        if not payload:
            return None
        try:
            last_exit = parse_iso_utc(payload.get("last_exit_at"))
        except Exception:
            self._state["reentry_cooldowns"].pop(key, None)
            self._save_state()
            return None
        exit_reason = payload.get("exit_reason", "DEFAULT")
        cooldown_minutes = self._get_cooldown_minutes_by_reason(exit_reason)
        ttl_seconds = max(0, int(cooldown_minutes)) * 60
        elapsed = (utc_now() - last_exit).total_seconds()
        if elapsed >= ttl_seconds:
            self._state["reentry_cooldowns"].pop(key, None)
            self._save_state()
            return None
        payload = dict(payload)
        payload["remaining_seconds"] = max(0, int(ttl_seconds - elapsed))
        payload["cooldown_minutes"] = cooldown_minutes
        payload["exit_reason"] = payload.get("exit_reason", "unknown")
        return payload

    def _signal_key(self, signal: dict) -> str:
        chain = signal.get("chain", "?")
        contract = signal.get("contract_address") or signal.get("symbol") or "unknown"
        return f"{chain}:{contract}"

    def _open_position_exists(self, signal: dict) -> bool:
        return self._signal_key(signal) in self.position_manager.positions

    def _at_open_position_limit(self) -> bool:
        return len(self.position_manager.positions) >= self.max_open_positions

    def _finalize_pending(self, signal_key: str):
        self._state["pending_entries"].pop(signal_key, None)
        self._save_state()

    def _position_key(self, position) -> str:
        return f"{position.chain}:{position.contract_address}"

    def _estimate_token_amount(self, position) -> float:
        explicit = float(getattr(position, "token_amount", 0.0) or 0.0)
        if explicit > 0:
            return explicit
        entry_price = float(getattr(position, "entry_price", 0.0) or 0.0)
        size_usd = float(getattr(position, "size_usd", 0.0) or 0.0)
        if entry_price > 0 and size_usd > 0:
            return size_usd / entry_price
        return 0.0

    def _try_get_wallet_token_balance(self, chain: str, token: str) -> tuple[bool, float]:
        wallet = self._get_live_wallet(chain)
        if wallet is None or not token:
            return False, 0.0

        cache_key = f"{str(chain).lower()}:{token}"
        ttl = max(0, int(self.wallet_balance_cache_ttl_sec))
        cached = self._wallet_balance_cache.get(cache_key)
        if cached and ttl > 0:
            try:
                age = (utc_now() - parse_iso_utc(cached["checked_at"])).total_seconds()
            except Exception:
                age = ttl + 1
            if age <= ttl:
                return bool(cached.get("ok")), float(cached.get("balance", 0.0))

        try:
            result = (True, float(wallet.get_token_balance(token) or 0.0))
        except Exception as exc:
            log.warning("wallet token balance lookup failed: [%s] %s %s", chain, token, exc)
            result = (False, 0.0)

        self._wallet_balance_cache[cache_key] = {
            "ok": result[0],
            "balance": result[1],
            "checked_at": iso_utc_now(),
        }
        return result

    def _get_wallet_token_balance(self, chain: str, token: str) -> float:
        ok, balance = self._try_get_wallet_token_balance(chain, token)
        return balance if ok else 0.0

    def _get_exit_failure_state(self, position) -> dict:
        return self._state.setdefault("exit_failures", {}).setdefault(
            self._position_key(position),
            {
                "count": 0,
                "last_attempt_at": None,
                "last_error": None,
                "notified_manual": False,
            },
        )

    def _clear_exit_failure_state(self, position) -> None:
        self._state.setdefault("exit_failures", {}).pop(self._position_key(position), None)
        self._save_state()

    def _wallet_mismatch_key(self, chain: str, token: str) -> str:
        return f"{str(chain).lower()}:{token}"

    def get_wallet_mismatch(self, chain: str, token: str) -> Optional[dict]:
        return self._state.setdefault("wallet_mismatches", {}).get(self._wallet_mismatch_key(chain, token))

    def set_wallet_mismatch(self, chain: str, token: str, payload: dict) -> None:
        payload = dict(payload or {})
        payload.setdefault("detected_at", iso_utc_now())
        self._state.setdefault("wallet_mismatches", {})[self._wallet_mismatch_key(chain, token)] = payload
        self._save_state()

    def clear_wallet_mismatch(self, chain: str, token: str) -> None:
        key = self._wallet_mismatch_key(chain, token)
        if self._state.setdefault("wallet_mismatches", {}).pop(key, None) is not None:
            self._save_state()

    def _next_exit_slippage_pct(self, chain: str, failure_count: int) -> float:
        base = 2.0 if chain == "bsc" else 2.5
        if failure_count >= 2:
            return base + 10.0
        if failure_count >= 1:
            return base + 5.0
        return base

    def _fallback_snapshot(self, position, price_usd: float) -> dict:
        return {
            "symbol": position.symbol,
            "contract_address": position.contract_address,
            "price_usd": price_usd,
            "liquidity_usd": float(getattr(position, "current_liquidity", 0.0) or 0.0),
            "holders_binance": int(getattr(position, "current_b_holders", 0) or 0),
            "timestamp": iso_utc_now(),
            "_fallback_disappearance": True,
        }

    def build_market_snapshot(self, position) -> Optional[dict]:
        chain = str(position.chain).lower()
        token = position.contract_address
        if not token:
            return None

        # base: 라이브 DEX 호가 경로가 없어 포지션이 trending 피드에서 빠지면 가격이
        # 동결되던 문제(PITCH/BNKR 수십 시간 방치) → 지갑 불필요한 GeckoTerminal
        # 컨트랙트 가격으로 재호가. dry_run 포함 모든 모드에서 동작한다.
        if chain == "base":
            try:
                from gecko_client import fetch_token_price

                price_usd = fetch_token_price(chain, token)
            except Exception as exc:
                log.warning("base fallback price failed for %s: %s", position.symbol, exc)
                return None
            if not price_usd or price_usd <= 0:
                return None
            return self._fallback_snapshot(position, price_usd)

        # bsc/solana: 실제 매도 호가 기반(체결 정확도 우선, 지갑 필요 → live 전용).
        if self.mode != "live":
            return None
        amount_token = self._estimate_token_amount(position)
        if amount_token <= 0:
            return None

        try:
            if chain == "bsc":
                from .dex_pancakeswap import PancakeSwap

                usd_out = PancakeSwap(self._get_live_wallet(chain)).quote_sell(token, amount_token)
            elif chain == "solana":
                from .dex_jupiter import Jupiter

                quote = Jupiter(self._get_live_wallet(chain)).quote_sell_to_usdc(token, amount_token)
                usd_out = int(quote["outAmount"]) / 10**6
            else:
                return None
        except Exception as exc:
            log.warning("fallback quote failed for [%s] %s: %s", position.chain, position.symbol, exc)
            return None

        if usd_out <= 0:
            return None

        price_usd = usd_out / amount_token if amount_token > 0 else float(position.current_price)
        return self._fallback_snapshot(position, price_usd)

    def submit_entry_signal(self, signal: dict):
        amount_usd = float(signal.get("position_size_usd", 0.0))
        chain = signal.get("chain", "?")
        signal_key = self._signal_key(signal)

        if self._at_open_position_limit():
            return {"status": "blocked", "reason": "max_open_positions"}
        if self._open_position_exists(signal):
            return {"status": "duplicate_open", "signal_key": signal_key}
        if signal_key in self._state["pending_entries"]:
            return {"status": "already_pending", "signal_key": signal_key}
        cooldown = self._active_stop_loss_cooldown(signal)
        if cooldown:
            return {"status": "blocked", "reason": "stop_loss_cooldown", "cooldown_remaining_sec": cooldown.get("remaining_seconds", 0)}
        reentry_cd = self._active_reentry_cooldown(signal)
        if reentry_cd:
            return {
                "status": "blocked",
                "reason": "reentry_cooldown",
                "cooldown_remaining_sec": reentry_cd.get("remaining_seconds", 0),
                "last_exit_reason": reentry_cd.get("exit_reason"),
            }
        mismatch = self.get_wallet_mismatch(chain, signal.get("contract_address"))
        if mismatch:
            log.warning("trade blocked by wallet/ledger mismatch: [%s] %s details=%s", chain, signal.get("symbol", "?"), mismatch)
            return {"status": "blocked", "reason": "wallet_ledger_mismatch"}
        can_open, open_reason = self.position_manager.can_open(chain, signal.get("contract_address"))
        if not can_open:
            # 중복 로그 제거: orchestrator가 호출 직후 "executor entry result: blocked reason=..." 형태로 동일 사건 기록
            return {"status": "blocked", "reason": open_reason}

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

        approval_timeout = int(signal.get("approval_timeout", 300))
        signal["approval_timeout"] = approval_timeout
        # 시그널 단계 알림 제거 — 체결 확정 후 send_entry_notification에서만 1회 발사
        # auto_approve_on_timeout=True 가정. 수동 승인 모드 재도입 시 분기 복구 필요.
        signal_id = self.notifier.create_pending_approval(signal, auto_entry=True)

        self._state["pending_entries"][signal_key] = {
            "signal_id": signal_id,
            "queued_at": iso_utc_now(),
            "approval_timeout": approval_timeout,
            "signal": signal,
            "notification_sent": False,
        }
        self._save_state()
        return {"status": "queued", "signal_id": signal_id, "signal_key": signal_key}

    def process_pending_entries(self) -> list[dict]:
        results = []
        pending_items = list(self._state.get("pending_entries", {}).items())
        now = utc_now()

        for signal_key, item in pending_items:
            signal = item.get("signal") or {}
            signal_id = item.get("signal_id")
            decision = self.notifier.get_decision(signal_id)

            if not decision:
                try:
                    queued_at = parse_iso_utc(item.get("queued_at", now.isoformat()))
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

            if self._open_position_exists(signal):
                self._finalize_pending(signal_key)
                results.append({"status": "duplicate_open", "signal_id": signal_id, "signal_key": signal_key})
                continue
            if self._at_open_position_limit():
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), "max_open_positions")
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": "max_open_positions", "signal_id": signal_id, "signal_key": signal_key})
                continue
            cooldown = self._active_stop_loss_cooldown(signal)
            if cooldown:
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), "stop_loss_cooldown")
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": "stop_loss_cooldown", "signal_id": signal_id, "signal_key": signal_key})
                continue
            reentry_cd = self._active_reentry_cooldown(signal)
            if reentry_cd:
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), "reentry_cooldown")
                self._finalize_pending(signal_key)
                results.append({
                    "status": "blocked",
                    "reason": "reentry_cooldown",
                    "signal_id": signal_id,
                    "signal_key": signal_key,
                })
                continue
            chain = signal.get("chain", "?")
            mismatch = self.get_wallet_mismatch(chain, signal.get("contract_address"))
            if mismatch:
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), "wallet_ledger_mismatch")
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": "wallet_ledger_mismatch", "signal_id": signal_id, "signal_key": signal_key})
                continue
            can_open, open_reason = self.position_manager.can_open(chain, signal.get("contract_address"))
            if not can_open:
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), open_reason)
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": open_reason, "signal_id": signal_id, "signal_key": signal_key})
                continue

            amount_usd = float(signal.get("position_size_usd", 0.0))
            allowed, reason = self.safety.can_execute(chain, amount_usd)
            if not allowed:
                log.warning("trade blocked by safety at execution time: %s", reason)
                log.info("entry skipped (no notification sent): %s reason=%s", signal.get("symbol", "?"), reason)
                self._finalize_pending(signal_key)
                results.append({"status": "blocked", "reason": reason, "signal_id": signal_id, "signal_key": signal_key})
                continue

            buy_result = self._execute_buy(signal)
            if not buy_result["ok"]:
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": buy_result["reason"], "signal_id": signal_id, "signal_key": signal_key})
                continue

            cfg = self.position_manager.chain_configs[chain]
            signal_for_position = self._signal_with_execution_fill(signal, buy_result)
            env_capital = os.getenv("TOTAL_CAPITAL_USD")
            if env_capital:
                try:
                    effective_total_capital_usd = float(env_capital)
                except ValueError:
                    effective_total_capital_usd = amount_usd / (cfg.capital_per_position_pct / 100.0)
            else:
                if cfg.capital_per_position_pct <= 0:
                    self._finalize_pending(signal_key)
                    results.append({"status": "failed", "reason": "invalid_chain_position_pct", "signal_id": signal_id, "signal_key": signal_key})
                    continue
                effective_total_capital_usd = amount_usd / (cfg.capital_per_position_pct / 100.0)
            position = self.position_manager.open_position(
                chain,
                signal_for_position,
                total_capital_usd=effective_total_capital_usd,
            )
            if position is None:
                self._finalize_pending(signal_key)
                results.append({"status": "failed", "reason": "position_manager_rejected", "signal_id": signal_id, "signal_key": signal_key})
                continue

            if not item.get("notification_sent"):
                sent = self.notifier.send_entry_notification(signal_for_position)
                if not sent:
                    log.warning("post-execution entry notification failed: %s", signal.get("symbol", "?"))
            self.safety.record_trade_executed(mode_at_execute=self.mode)
            self._finalize_pending(signal_key)
            results.append(
                {
                    "status": "executed",
                    "position": position,
                    "approval": decision,
                    "signal_id": signal_id,
                    "signal_key": signal_key,
                    "tx_hash": buy_result.get("tx_hash"),
                }
            )

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
        failure_state = self._get_exit_failure_state(position)
        last_attempt_at = failure_state.get("last_attempt_at")
        if last_attempt_at:
            try:
                elapsed = (utc_now() - parse_iso_utc(last_attempt_at)).total_seconds()
            except Exception:
                elapsed = 9999
            if elapsed < 30:
                return {
                    "status": "retry_wait",
                    "reason": f"retry_backoff_{30-int(elapsed)}s",
                    "attempts": int(failure_state.get("count", 0)),
                }

        sell_result = self._execute_sell(position)
        if not sell_result["ok"]:
            failure_state["count"] = int(failure_state.get("count", 0)) + 1
            failure_state["last_attempt_at"] = iso_utc_now()
            failure_state["last_error"] = sell_result["reason"]
            attempts = failure_state["count"]
            log.warning(
                "exit failed: [%s] %s reason=%s attempts=%s",
                position.chain,
                position.symbol,
                sell_result["reason"],
                attempts,
            )
            if attempts >= 5 and not failure_state.get("notified_manual"):
                failure_state["notified_manual"] = True
                self.notifier.send_emergency(
                    f"Manual exit needed: {position.symbol} [{position.chain.upper()}]\n"
                    f"Reason: {sell_result['reason']}\n"
                    f"Attempts: {attempts}"
                )
            self._save_state()
            return {"status": "failed", "reason": sell_result["reason"], "attempts": attempts}

        pnl_usd = float(sell_result.get("amount_out_usd", 0.0)) - float(position.size_usd)
        pnl_pct = (pnl_usd / float(position.size_usd)) * 100.0 if position.size_usd else 0.0
        self.safety.record_trade_closed(pnl_usd, mode_at_close=self.mode)
        closed = self.position_manager.close_position(
            position,
            reason,
            f"executor:{reason.value}",
            exit_price=sell_result.get("exit_price"),
            realized_pnl_pct=pnl_pct,
            realized_pnl_usd=pnl_usd,
            exit_tx_hash=sell_result.get("tx_hash"),
        )
        reason_value = getattr(reason, "value", str(reason))
        if reason_value == "STOP_LOSS":
            self._state.setdefault("stop_loss_cooldowns", {})[self._position_key(position)] = {
                "last_stop_at": iso_utc_now(),
                "symbol": position.symbol,
            }
        self._state.setdefault("reentry_cooldowns", {})[self._position_key(position)] = {
            "last_exit_at": iso_utc_now(),
            "symbol": position.symbol,
            "exit_reason": reason_value,
        }
        self._save_state()
        self.notifier.send_exit_notification(closed, reason.value, pnl_pct, pnl_usd)
        self._clear_exit_failure_state(position)
        return {"status": "closed", "position": closed, "tx_hash": sell_result.get("tx_hash")}

    def _execute_buy(self, signal: dict) -> dict:
        if self.mode == "live":
            return self._buy_live(signal)
        return self._simulate_buy(signal)

    def _execute_sell(self, position) -> dict:
        if self.mode == "live":
            return self._sell_live(position)
        return self._simulate_sell(position)

    def _signal_with_execution_fill(self, signal: dict, buy_result: dict) -> dict:
        filled = dict(signal)
        amount_out = float(buy_result.get("amount_out_token") or 0.0)
        amount_in = float(signal.get("position_size_usd", 0.0))
        if amount_out > 0 and amount_in > 0:
            filled["price_usd"] = amount_in / amount_out
            filled["_token_amount"] = amount_out
        filled["_entry_tx_hash"] = buy_result.get("tx_hash")
        return filled

    def _buy_live(self, signal: dict) -> dict:
        chain = str(signal.get("chain", "")).lower()
        token = signal.get("contract_address")
        amount_usd = float(signal.get("position_size_usd", 0.0))
        if not token:
            return {"ok": False, "reason": "missing_contract_address"}
        if amount_usd <= 0:
            return {"ok": False, "reason": "invalid_position_size"}

        try:
            if chain == "bsc":
                from .dex_pancakeswap import PancakeSwap

                result = PancakeSwap(self._get_live_wallet(chain)).buy(token, amount_usd, slippage_pct=2.0)
                if not result.success:
                    return {"ok": False, "reason": result.error or "bsc_buy_failed", "tx_hash": result.tx_hash}
                return {
                    "ok": True,
                    "tx_hash": result.tx_hash,
                    "amount_out_token": float(result.amount_out or 0.0),
                    "timestamp": iso_utc_now(),
                }

            if chain == "solana":
                from .dex_jupiter import Jupiter

                result = Jupiter(self._get_live_wallet(chain)).buy(token, amount_usd, slippage_pct=2.5)
                if not result.success:
                    return {"ok": False, "reason": result.error or "solana_buy_failed", "tx_hash": result.tx_signature}
                return {
                    "ok": True,
                    "tx_hash": result.tx_signature,
                    "amount_out_token": float(result.amount_out or 0.0),
                    "timestamp": iso_utc_now(),
                }
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

        return {"ok": False, "reason": f"unsupported_chain:{chain}"}

    def _sell_live(self, position) -> dict:
        chain = str(position.chain).lower()
        token = position.contract_address
        estimated_amount = self._estimate_token_amount(position)
        wallet_balance = self._get_wallet_token_balance(chain, token)
        amount_token = wallet_balance if wallet_balance > 0 else estimated_amount
        failure_count = int(self._get_exit_failure_state(position).get("count", 0))
        if not token:
            return {"ok": False, "reason": "missing_contract_address"}
        if amount_token <= 0:
            return {"ok": False, "reason": "missing_token_amount_for_live_sell"}
        if wallet_balance > 0 and estimated_amount > 0 and wallet_balance < estimated_amount:
            log.warning(
                "sell amount adjusted to wallet balance: [%s] %s estimated=%.9f actual=%.9f",
                chain,
                position.symbol,
                estimated_amount,
                wallet_balance,
            )

        try:
            if chain == "bsc":
                from .dex_pancakeswap import PancakeSwap

                wallet = self._get_live_wallet(chain)
                dex = PancakeSwap(wallet)
                slippage_pct = self._next_exit_slippage_pct(chain, failure_count)
                fractions = [0.995, 0.98, 0.95, 0.90] if wallet_balance > 0 else [1.0]
                last_failure = None
                token_decimals = dex.get_token_info(token)["decimals"]
                quantum = 10 ** token_decimals

                for fraction in fractions:
                    sell_amount = amount_token * fraction
                    sell_amount = int(sell_amount * quantum) / quantum
                    if sell_amount <= 0:
                        continue
                    if fraction < 1.0:
                        log.warning(
                            "partial sell retry: [%s] %s fraction=%.3f amount=%.9f",
                            chain,
                            position.symbol,
                            fraction,
                            sell_amount,
                        )
                    result = dex.sell(token, sell_amount, slippage_pct=slippage_pct)
                    if result.success:
                        exit_price = (float(result.amount_out or 0.0) / sell_amount) if sell_amount > 0 else None
                        return {
                            "ok": True,
                            "tx_hash": result.tx_hash,
                            "amount_out_usd": float(result.amount_out or 0.0),
                            "exit_price": exit_price,
                            "timestamp": iso_utc_now(),
                        }
                    last_failure = result
                    error_text = str(result.error or "")
                    if "TRANSFER_FROM_FAILED" not in error_text:
                        return {"ok": False, "reason": result.error or "bsc_sell_failed", "tx_hash": result.tx_hash}

                if last_failure is not None:
                    return {"ok": False, "reason": last_failure.error or "bsc_sell_failed", "tx_hash": last_failure.tx_hash}
                return {"ok": False, "reason": "bsc_sell_failed_no_attempt"}

            if chain == "solana":
                from .dex_jupiter import Jupiter

                slippage_pct = self._next_exit_slippage_pct(chain, failure_count)
                result = Jupiter(self._get_live_wallet(chain)).sell(token, amount_token, slippage_pct=slippage_pct)
                if not result.success:
                    return {"ok": False, "reason": result.error or "solana_sell_failed", "tx_hash": result.tx_signature}
                exit_price = (float(result.amount_out or 0.0) / amount_token) if amount_token > 0 else None
                return {
                    "ok": True,
                    "tx_hash": result.tx_signature,
                    "amount_out_usd": float(result.amount_out or 0.0),
                    "exit_price": exit_price,
                    "timestamp": iso_utc_now(),
                }
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

        return {"ok": False, "reason": f"unsupported_chain:{chain}"}

    def _simulate_buy(self, signal: dict) -> dict:
        amount_in_usd = float(signal.get("position_size_usd", 0.0) or 0.0)
        price_usd = float(signal.get("price_usd", 0.0) or 0.0)
        amount_out_token = (amount_in_usd / price_usd) if price_usd > 0 else 0.0
        log.info(
            "dry_run simulated buy: [%s] %s @ $%.6f size=$%.2f",
            signal.get("chain", "?"),
            signal.get("symbol", "?"),
            price_usd,
            amount_in_usd,
        )
        return {
            "ok": True,
            "timestamp": iso_utc_now(),
            "amount_out_token": amount_out_token,
            "tx_hash": "dry_run_buy",
        }

    def _simulate_sell(self, position) -> dict:
        token_amount = float(getattr(position, "token_amount", 0.0) or 0.0)
        current_price = float(getattr(position, "current_price", 0.0) or 0.0)
        size_usd = float(getattr(position, "size_usd", 0.0) or 0.0)
        amount_out_usd = token_amount * current_price if token_amount > 0 and current_price > 0 else (
            size_usd * (1.0 + float(position.unrealized_pnl_pct) / 100.0)
        )
        log.info(
            "dry_run simulated sell: [%s] %s @ $%.6f pnl=%+.2f%%",
            position.chain,
            position.symbol,
            current_price,
            float(position.unrealized_pnl_pct),
        )
        return {
            "ok": True,
            "timestamp": iso_utc_now(),
            "amount_out_usd": amount_out_usd,
            "exit_price": current_price if current_price > 0 else None,
            "tx_hash": "dry_run_sell",
        }

    def emergency_close_all(self):
        closed = 0
        for position in list(self.position_manager.positions.values()):
            self.handle_exit_signal(position, ManualExitReason.MANUAL_STOP)
            closed += 1
        self.notifier.send_emergency(f"manual stop closed {closed} open positions")
        return f"manual stop closed {closed} open positions"
