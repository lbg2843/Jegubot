import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List


@dataclass
class SafetyState:
    daily_pnl_usd: float = 0.0
    daily_trades: int = 0
    consecutive_losses: int = 0
    recent_losses: List[str] = None
    halt_count_today: int = 0
    halt_until: str | None = None
    halt_reason: str | None = None
    last_reset_date: str | None = None


class SafetyCircuitBreaker:
    def __init__(
        self,
        storage_path: str | Path,
        mode: str = "live",
        daily_loss_limit_usd: float = 50.0,
        max_consecutive_losses: int = 5,
        max_position_size_usd: float = 100.0,
        max_daily_trades: int = 30,
        halt_duration_hours_first: int = 1,
        halt_duration_hours_second: int = 4,
        halt_duration_hours_third: int = 24,
        loss_window_hours: float = 1.0,
    ):
        self.storage_path = Path(storage_path)
        self.mode = mode
        self.daily_loss_limit_usd = float(daily_loss_limit_usd)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.max_position_size_usd = float(max_position_size_usd)
        self.max_daily_trades = int(max_daily_trades)
        self.halt_duration_hours_first = int(halt_duration_hours_first)
        self.halt_duration_hours_second = int(halt_duration_hours_second)
        self.halt_duration_hours_third = int(halt_duration_hours_third)
        self.loss_window_hours = float(loss_window_hours)
        self.state = SafetyState()
        self._load()
        self.reset_daily_if_needed()

    def _load(self):
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except Exception:
            return
        data.setdefault("recent_losses", [])
        data.setdefault("halt_count_today", 0)
        self.state = SafetyState(**data)
        if self.state.recent_losses is None:
            self.state.recent_losses = []

    def _save(self):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps(asdict(self.state), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def reset_daily_if_needed(self):
        today = datetime.now().date().isoformat()
        if self.state.last_reset_date == today:
            return
        self.state.daily_pnl_usd = 0.0
        self.state.daily_trades = 0
        self.state.consecutive_losses = 0
        self.state.recent_losses = []
        self.state.halt_count_today = 0
        self.state.last_reset_date = today
        self._save()

    def _halt_duration_hours(self) -> int:
        next_count = int(self.state.halt_count_today or 0) + 1
        if next_count == 1:
            return self.halt_duration_hours_first
        if next_count == 2:
            return self.halt_duration_hours_second
        return self.halt_duration_hours_third

    def _apply_halt(self, reason: str):
        self.state.halt_count_today = int(self.state.halt_count_today or 0) + 1
        duration_hours = self._halt_duration_hours_for_count(self.state.halt_count_today)
        self.state.halt_until = (datetime.now() + timedelta(hours=duration_hours)).isoformat()
        self.state.halt_reason = reason
        self._save()

    def _halt_duration_hours_for_count(self, halt_count: int) -> int:
        if halt_count <= 1:
            return self.halt_duration_hours_first
        if halt_count == 2:
            return self.halt_duration_hours_second
        return self.halt_duration_hours_third

    def halt_for_24h(self, reason: str):
        self._apply_halt(reason)

    def manual_unhalt(self):
        self.state.halt_until = None
        self.state.halt_reason = None
        self.state.consecutive_losses = 0
        self.state.recent_losses = []
        self._save()

    def _recent_loss_datetimes(self) -> list[datetime]:
        items = []
        for value in self.state.recent_losses or []:
            try:
                items.append(datetime.fromisoformat(value))
            except ValueError:
                continue
        return items

    def _trim_recent_losses(self, now: datetime | None = None) -> list[datetime]:
        now = now or datetime.now()
        cutoff = now - timedelta(hours=self.loss_window_hours)
        kept = [ts for ts in self._recent_loss_datetimes() if ts > cutoff]
        self.state.recent_losses = [ts.isoformat() for ts in kept]
        return kept

    def can_execute(self, chain: str, amount_usd: float) -> tuple[bool, str]:
        self.reset_daily_if_needed()

        if self.mode == "dry_run":
            return True, "dry_run_mode"

        if self.state.halt_until:
            try:
                halt_until = datetime.fromisoformat(self.state.halt_until)
            except ValueError:
                halt_until = None
            if halt_until and halt_until > datetime.now():
                return False, f"halted_until:{halt_until.isoformat()}"
            self.manual_unhalt()

        if amount_usd > self.max_position_size_usd:
            return False, f"position_size_limit:{amount_usd:.2f}>{self.max_position_size_usd:.2f}"
        if self.state.daily_trades >= self.max_daily_trades:
            return False, "daily_trade_limit"
        if self.state.daily_pnl_usd <= -abs(self.daily_loss_limit_usd):
            self.halt_for_24h("daily_loss_limit")
            return False, "daily_loss_limit"
        if self.state.consecutive_losses >= self.max_consecutive_losses:
            self._apply_halt(f"losing_streak_x{self.state.halt_count_today + 1}")
            return False, "consecutive_losses"
        return True, f"ok:{chain}"

    def record_trade_executed(self, mode_at_execute: str | None = None):
        if mode_at_execute == "dry_run" or self.mode == "dry_run":
            return
        self.reset_daily_if_needed()
        self.state.daily_trades += 1
        self._save()

    def record_trade_closed(self, pnl_usd: float, mode_at_close: str | None = None):
        if mode_at_close == "dry_run" or self.mode == "dry_run":
            return

        self.reset_daily_if_needed()
        self.state.daily_pnl_usd += float(pnl_usd)
        now = datetime.now()
        recent_losses = self._trim_recent_losses(now)

        if pnl_usd < 0:
            self.state.consecutive_losses += 1
            recent_losses.append(now)
            self.state.recent_losses = [ts.isoformat() for ts in recent_losses]
        else:
            self.state.consecutive_losses = 0
            self.state.recent_losses = []

        if self.state.daily_pnl_usd <= -abs(self.daily_loss_limit_usd):
            self._apply_halt("daily_loss_limit")
        elif len(recent_losses) >= self.max_consecutive_losses:
            self._apply_halt(f"losing_streak_x{self.state.halt_count_today + 1}")
        else:
            self._save()

    def get_status(self) -> dict:
        self.reset_daily_if_needed()
        return {
            "daily_pnl_usd": round(self.state.daily_pnl_usd, 2),
            "daily_trades": self.state.daily_trades,
            "consecutive_losses": self.state.consecutive_losses,
            "recent_losses_in_window": len(self._trim_recent_losses()),
            "halt_count_today": self.state.halt_count_today,
            "halt_until": self.state.halt_until,
            "halt_reason": self.state.halt_reason,
            "mode": self.mode,
            "limits": {
                "daily_loss_limit_usd": self.daily_loss_limit_usd,
                "max_consecutive_losses": self.max_consecutive_losses,
                "max_position_size_usd": self.max_position_size_usd,
                "max_daily_trades": self.max_daily_trades,
                "halt_duration_hours_first": self.halt_duration_hours_first,
                "halt_duration_hours_second": self.halt_duration_hours_second,
                "halt_duration_hours_third": self.halt_duration_hours_third,
                "loss_window_hours": self.loss_window_hours,
            },
        }

