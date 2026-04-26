import html
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

from .time_utils import format_for_user


log = logging.getLogger("trading.notifier")


def format_price(price: float) -> str:
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:,.6f}"
    return f"${price:,.8f}"


def _compact_usd(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"${value/1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"${value/1_000:.0f}K"
    return f"${value:,.0f}"


def format_timestamp_for_user(value) -> str:
    return format_for_user(value)


class TelegramTradeNotifier:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        state_path: str | Path,
        auto_approve_on_timeout: bool = True,
        poll_interval: float = 2.0,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else None
        self.state_path = Path(state_path)
        self.auto_approve_on_timeout = auto_approve_on_timeout
        self.poll_interval = poll_interval
        self._handlers: dict[str, Callable[[], Any]] = {}
        self._state = {
            "offset": 0,
            "approvals": {},
            "handled_callback_ids": [],
            "handled_update_ids": [],
        }
        self.network_available = True
        self.last_send_ok = True
        self._load_state()

    def configure_handlers(
        self,
        status_handler: Callable[[], dict] | None = None,
        stop_handler: Callable[[], str] | None = None,
        unhalt_handler: Callable[[], str] | None = None,
    ):
        if status_handler:
            self._handlers["status"] = status_handler
        if stop_handler:
            self._handlers["stop"] = stop_handler
        if unhalt_handler:
            self._handlers["unhalt"] = unhalt_handler

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state.setdefault("offset", 0)
            self._state.setdefault("approvals", {})
            self._state.setdefault("handled_callback_ids", [])
            self._state.setdefault("handled_update_ids", [])
        except Exception:
            pass

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _remember_limited(self, key: str, value: int | str, limit: int = 200):
        bucket = self._state.setdefault(key, [])
        if value in bucket:
            return
        bucket.append(value)
        if len(bucket) > limit:
            del bucket[:-limit]

    def _was_handled(self, key: str, value: int | str) -> bool:
        return value in self._state.get(key, [])

    def _api(
        self,
        method: str,
        payload: dict | None = None,
        *,
        timeout_seconds: float = 8.0,
        max_attempts: int = 3,
        retry_sleep_seconds: float = 1.0,
    ) -> dict:
        if not self._enabled():
            return {}
        payload = payload or {}
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        last_exc = None
        for attempt in range(max_attempts):
            try:
                response = requests.post(url, json=payload, timeout=timeout_seconds)
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError(data)
                self.network_available = True
                return data
            except Exception as exc:
                last_exc = exc
                self.network_available = False
                if attempt < max_attempts - 1:
                    time.sleep(retry_sleep_seconds + attempt)
        log.warning("telegram api %s failed: %s", method, last_exc)
        return {}

    def _send_message(self, text: str, reply_markup: dict | None = None):
        if not self._enabled():
            log.info("[DRY-RUN] Telegram disabled, message preview:\n%s", text)
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = self._api("sendMessage", payload)
        self.last_send_ok = bool(response)
        return response

    def _answer_callback(self, callback_query_id: str, text: str):
        if not self._enabled():
            return
        self._api(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:180]},
        )

    def _format_entry_text(self, signal: dict) -> str:
        chain = signal.get("chain", "?").upper()
        return (
            f"🎯 <b>ENTRY SIGNAL [{html.escape(chain)}]</b>\n"
            f"Symbol: <code>{html.escape(signal.get('symbol', '?'))}</code>\n"
            f"Price: {format_price(float(signal.get('price_usd', 0.0)))}\n"
            f"1h change: {float(signal.get('price_change_1h_pct', 0.0)):+.1f}%\n"
            f"Liquidity: {_compact_usd(float(signal.get('liquidity_usd', 0.0)))}\n"
            f"Trade plan:\n\n"
            f"Position: ${float(signal.get('position_size_usd', 0.0)):.0f}\n"
            f"Stop loss: {float(signal.get('stop_loss_pct', 0.0)):+.0f}%\n"
            f"Take profit: {float(signal.get('take_profit_pct', 0.0)):+.0f}% "
            f"({'Profit Lock' if signal.get('profit_lock', True) else 'Immediate Exit'})\n"
            f"Max hold: {int(signal.get('max_hold_hours', 0))}h\n\n"
            "⏱ 5분 후 자동 승인"
        )

    def send_entry_signal(self, signal: dict) -> str:
        signal_id = signal.get("signal_id") or uuid.uuid4().hex[:12]
        signal["signal_id"] = signal_id
        self._state["approvals"][signal_id] = {
            "decision": None,
            "signal": signal,
        }
        self._save_state()
        keyboard = {
            "inline_keyboard": [[
                {"text": "BUY", "callback_data": f"trade:buy:{signal_id}"},
                {"text": "SKIP", "callback_data": f"trade:skip:{signal_id}"},
            ]]
        }
        self._send_message(self._format_entry_text(signal), keyboard)
        return signal_id

    def get_decision(self, signal_id: str) -> str | None:
        return self._state.get("approvals", {}).get(signal_id, {}).get("decision")

    def mark_decision(self, signal_id: str, decision: str) -> str:
        approval = self._state["approvals"].setdefault(signal_id, {})
        approval["decision"] = decision
        self._save_state()
        return decision

    def resolve_timeout(self, signal_id: str) -> str:
        decision = "approved" if self.auto_approve_on_timeout else "timeout"
        return self.mark_decision(signal_id, decision)

    def _format_status_message(self, status: dict) -> str:
        return (
            "🛡 <b>TRADING STATUS</b>\n"
            f"Daily PnL: ${status.get('daily_pnl_usd', 0.0):+.2f}\n"
            f"Daily trades: {status.get('daily_trades', 0)}\n"
            f"Consecutive losses: {status.get('consecutive_losses', 0)}\n"
            f"Halt until: {status.get('halt_until') or '-'}\n"
            f"Reason: {status.get('halt_reason') or '-'}"
        )

    def _process_command(self, text: str):
        command = text.lstrip("/").split("@", 1)[0].split()[0].lower()
        handler = self._handlers.get(command)
        if not handler:
            return None
        result = handler()
        if isinstance(result, dict):
            return self._format_status_message(result)
        return str(result)

    def _consume_updates(
        self,
        *,
        long_poll_timeout: int = 10,
        request_timeout: float = 8.0,
        max_attempts: int = 3,
    ) -> list[dict]:
        if not self._enabled():
            return []
        payload = {"timeout": long_poll_timeout, "offset": int(self._state.get("offset", 0))}
        data = self._api(
            "getUpdates",
            payload,
            timeout_seconds=request_timeout,
            max_attempts=max_attempts,
            retry_sleep_seconds=0.25,
        )
        updates = data.get("result", [])
        for update in updates:
            self._state["offset"] = max(self._state.get("offset", 0), update["update_id"] + 1)
        if updates:
            self._save_state()
        return updates

    def process_pending_commands(
        self,
        *,
        long_poll_timeout: int = 10,
        request_timeout: float = 8.0,
        max_attempts: int = 3,
    ):
        for update in self._consume_updates(
            long_poll_timeout=long_poll_timeout,
            request_timeout=request_timeout,
            max_attempts=max_attempts,
        ):
            self._handle_update(update)

    def _handle_update(self, update: dict):
        update_id = update.get("update_id")
        if update_id is not None:
            if self._was_handled("handled_update_ids", update_id):
                return
            self._remember_limited("handled_update_ids", update_id)

        callback = update.get("callback_query")
        if callback:
            callback_id = callback.get("id")
            if callback_id and self._was_handled("handled_callback_ids", callback_id):
                return
            data = callback.get("data", "")
            parts = data.split(":")
            if len(parts) == 3 and parts[0] == "trade":
                decision = "approved" if parts[1] == "buy" else "rejected"
                signal_id = parts[2]
                approval = self._state["approvals"].setdefault(signal_id, {})
                if not approval.get("decision"):
                    approval["decision"] = decision
                self._save_state()
                if callback_id:
                    self._remember_limited("handled_callback_ids", callback_id)
                self._answer_callback(callback["id"], f"{decision.upper()} recorded")
            return

        message = update.get("message") or {}
        if str(message.get("chat", {}).get("id")) != self.chat_id:
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        reply = self._process_command(text)
        if reply:
            self._send_message(reply)

    def wait_for_approval(self, signal_id: str, timeout: int = 300) -> str:
        if not self._enabled():
            decision = "approved" if self.auto_approve_on_timeout else "timeout"
            if signal_id in self._state["approvals"]:
                self._state["approvals"][signal_id]["decision"] = decision
                self._save_state()
            return decision

        if not self.last_send_ok:
            if signal_id in self._state["approvals"]:
                self._state["approvals"][signal_id]["decision"] = "send_failed"
                self._save_state()
            return "send_failed"

        started = time.time()
        while time.time() - started < timeout:
            for update in self._consume_updates():
                self._handle_update(update)
            decision = self._state["approvals"].get(signal_id, {}).get("decision")
            if decision:
                return decision
            if not self.network_available:
                decision = "approved" if self.auto_approve_on_timeout else "timeout"
                if signal_id in self._state["approvals"]:
                    self._state["approvals"][signal_id]["decision"] = decision
                    self._save_state()
                return decision
            time.sleep(self.poll_interval)

        decision = "approved" if self.auto_approve_on_timeout else "timeout"
        if signal_id in self._state["approvals"]:
            self._state["approvals"][signal_id]["decision"] = decision
            self._save_state()
        return decision

    def send_exit_notification(self, position, reason: str, pnl_pct: float, pnl_usd: float):
        text = (
            f"🚨 <b>EXIT SIGNAL [{html.escape(position.chain.upper())}]</b>\n"
            f"Symbol: <code>{html.escape(position.symbol)}</code>\n"
            f"Reason: {html.escape(reason)}\n"
            f"Entry was: {format_price(position.entry_price)} ({position.hold_hours:.1f}h ago)\n"
            f"Current: {format_price(position.current_price)}\n"
            f"Realized PnL: {pnl_pct:+.1f}% (${pnl_usd:+.2f})\n"
            "Action: Dry-run simulated sell"
        )
        self._send_message(text)

    def send_emergency(self, reason: str):
        self._send_message(
            "🛑 <b>EMERGENCY HALT</b>\n"
            f"Reason: {html.escape(reason)}"
        )

    def send_daily_summary(self, summary: dict):
        text = (
            "📊 <b>DAILY SUMMARY</b>\n"
            f"Open positions: {summary.get('open_positions', 0)}\n"
            f"Total realized today: ${summary.get('realized_today_usd', 0.0):+.2f}\n"
            f"Cash available: ${summary.get('cash_available_usd', 0.0):,.2f}\n"
            f"Halt: {summary.get('halt_reason') or 'off'}"
        )
        self._send_message(text)
