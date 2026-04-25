import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class SafetyState:
    daily_pnl_usd: float = 0.0
    daily_trades: int = 0
    consecutive_losses: int = 0
    halt_until: str | None = None
    halt_reason: str | None = None
    last_reset_date: str | None = None


class SafetyCircuitBreaker:
    def __init__(
        self,
        storage_path: str | Path,
        daily_loss_limit_usd: float = 50.0,
        max_consecutive_losses: int = 5,
        max_position_size_usd: float = 100.0,
        max_daily_trades: int = 30,
    ):
        self.storage_path = Path(storage_path)
        self.daily_loss_limit_usd = float(daily_loss_limit_usd)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.max_position_size_usd = float(max_position_size_usd)
        self.max_daily_trades = int(max_daily_trades)
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
        self.state = SafetyState(**data)

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
        self.state.last_reset_date = today
        self._save()

    def halt_for_24h(self, reason: str):
        self.state.halt_until = (datetime.now() + timedelta(hours=24)).isoformat()
        self.state.halt_reason = reason
        self._save()

    def manual_unhalt(self):
        self.state.halt_until = None
        self.state.halt_reason = None
        self._save()

    def can_execute(self, chain: str, amount_usd: float) -> tuple[bool, str]:
        self.reset_daily_if_needed()

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
            self.halt_for_24h("consecutive_losses")
            return False, "consecutive_losses"
        return True, f"ok:{chain}"

    def record_trade_executed(self):
        self.reset_daily_if_needed()
        self.state.daily_trades += 1
        self._save()

    def record_trade_closed(self, pnl_usd: float):
        self.reset_daily_if_needed()
        self.state.daily_pnl_usd += float(pnl_usd)
        if pnl_usd < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        if self.state.daily_pnl_usd <= -abs(self.daily_loss_limit_usd):
            self.halt_for_24h("daily_loss_limit")
        elif self.state.consecutive_losses >= self.max_consecutive_losses:
            self.halt_for_24h("consecutive_losses")
        else:
            self._save()

    def get_status(self) -> dict:
        self.reset_daily_if_needed()
        return {
            "daily_pnl_usd": round(self.state.daily_pnl_usd, 2),
            "daily_trades": self.state.daily_trades,
            "consecutive_losses": self.state.consecutive_losses,
            "halt_until": self.state.halt_until,
            "halt_reason": self.state.halt_reason,
            "limits": {
                "daily_loss_limit_usd": self.daily_loss_limit_usd,
                "max_consecutive_losses": self.max_consecutive_losses,
                "max_position_size_usd": self.max_position_size_usd,
                "max_daily_trades": self.max_daily_trades,
            },
        }

