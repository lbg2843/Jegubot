"""
Multichain Position Manager
=============================
기존 position_manager.py를 멀티체인 지원으로 확장.

체인별 독립 파라미터:
- stop_loss_pct, trailing_stop_pct, take_profit_pct
- max_concurrent, position_pct
- max_hold_hours

포트폴리오 관리:
- 전체 MDD 기반 circuit breaker
- 체인 간 자본 배분
- 체인-카테고리 조합 상관관계 제한
"""

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from trading.time_utils import iso_utc_now, parse_iso_utc, utc_now


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mc_position_mgr")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _load_exit_config(chain: str) -> dict:
    chain_upper = chain.upper()
    defaults = {
        "bsc": {
            "stop_loss_pct": -20.0,
            "trailing_activation_pct": 10.0,
            "trailing_stop_pct": 15.0,
            "take_profit_pct": 40.0,
            "max_hold_hours": 24.0,
        },
        "solana": {
            "stop_loss_pct": -12.0,
            "trailing_activation_pct": 5.0,
            "trailing_stop_pct": 8.0,
            "take_profit_pct": 20.0,
            "max_hold_hours": 2.0,
        },
        "base": {
            "stop_loss_pct": -20.0,
            "trailing_activation_pct": 10.0,
            "trailing_stop_pct": 15.0,
            "take_profit_pct": 40.0,
            "max_hold_hours": 24.0,
        },
    }
    base = defaults[chain]
    return {
        "stop_loss_pct": float(os.getenv(f"{chain_upper}_STOP_LOSS_PCT", str(base["stop_loss_pct"]))),
        "trailing_activation_pct": float(
            os.getenv(f"{chain_upper}_TRAILING_ACTIVATION_PCT", str(base["trailing_activation_pct"]))
        ),
        "trailing_stop_pct": float(
            os.getenv(f"{chain_upper}_TRAILING_STOP_PCT", str(base["trailing_stop_pct"]))
        ),
        "take_profit_pct": float(os.getenv(f"{chain_upper}_TAKE_PROFIT_PCT", str(base["take_profit_pct"]))),
        "max_hold_hours": float(os.getenv(f"{chain_upper}_MAX_HOLD_HOURS", str(base["max_hold_hours"]))),
    }


# ============================================================
# 체인별 설정
# ============================================================

@dataclass
class ChainConfig:
    chain: str
    stop_loss_pct: float
    trailing_activation_pct: float
    trailing_stop_pct: float
    take_profit_pct: float
    max_hold_hours: int
    capital_per_position_pct: float
    max_concurrent_positions: int
    liquidity_crash_pct: float
    b_holders_crash_pct: float


# 시뮬레이션에서 도출한 최적 파라미터
CHAIN_CONFIGS = {
    "bsc": ChainConfig(
        chain="bsc",
        **_load_exit_config("bsc"),
        capital_per_position_pct=6.0,
        max_concurrent_positions=4,  # monitoring-only temporary increase (2026-05-04). Re-evaluate after validation.
        liquidity_crash_pct=-20.0,
        b_holders_crash_pct=-10.0,
    ),
    "solana": ChainConfig(
        chain="solana",
        **_load_exit_config("solana"),
        capital_per_position_pct=3.0,
        max_concurrent_positions=2,
        liquidity_crash_pct=-25.0,
        b_holders_crash_pct=-15.0,
    ),
    "base": ChainConfig(
        chain="base",
        **_load_exit_config("base"),
        capital_per_position_pct=6.0,
        max_concurrent_positions=4,
        liquidity_crash_pct=-20.0,
        b_holders_crash_pct=-10.0,
    ),
}

DISABLED_CHAIN_CONFIGS = {}


# ============================================================
# 포트폴리오 레벨 설정
# ============================================================

@dataclass
class PortfolioConfig:
    # 전체 자본 배분
    chain_allocation: dict = None  # {"bsc": 0.25, "solana": 0.20, "base": 0.55}

    # Circuit breaker
    portfolio_mdd_warn: float = -0.15    # -15% 도달 시 경고
    portfolio_mdd_halt: float = -0.25    # -25% 도달 시 전체 중단
    recovery_threshold: float = 0.85     # 85% 회복 시 재개

    # 전체 리스크 한도
    max_total_exposure_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "15.0"))
    )  # 동시 노출 자본의 15%까지만


# ============================================================
# 청산 사유 + 포지션
# ============================================================

class ExitReason(Enum):
    NONE = "NONE"
    STOP_LOSS = "STOP_LOSS"
    EARLY_STOP_LOSS = "EARLY_STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    PROFIT_LOCK_BREAK = "PROFIT_LOCK_BREAK"
    TIME_EXIT = "TIME_EXIT"
    LIQUIDITY_CRASH = "LIQUIDITY_CRASH"
    HOLDER_EXODUS = "HOLDER_EXODUS"
    PORTFOLIO_HALT = "PORTFOLIO_HALT"
    MANUAL = "MANUAL"


@dataclass
class Position:
    # 식별
    chain: str
    symbol: str
    contract_address: str

    # 진입
    entry_timestamp: str
    entry_price: float
    entry_liquidity: float
    entry_b_holders: int
    size_usd: float
    token_amount: float = 0.0
    entry_tx_hash: Optional[str] = None

    # 현재
    current_price: float = 0.0
    peak_price: float = 0.0
    current_liquidity: float = 0.0
    current_b_holders: int = 0

    last_update: str = ""
    trailing_active: bool = False
    profit_locked: bool = False

    # 청산
    is_closed: bool = False
    exit_reason: str = ExitReason.NONE.value
    exit_timestamp: Optional[str] = None
    exit_price: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    exit_tx_hash: Optional[str] = None

    # 진입 경로 (reflexivity / golden_zone)
    entry_path: str = "reflexivity"
    trending_lost_since: Optional[str] = None
    fallback_missing_since: Optional[str] = None

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price / self.entry_price - 1) * 100

    @property
    def peak_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.peak_price / self.entry_price - 1) * 100

    @property
    def drawdown_from_peak_pct(self) -> float:
        if self.peak_price == 0:
            return 0.0
        return (self.current_price / self.peak_price - 1) * 100

    @property
    def hold_hours(self) -> float:
        # 보유시간은 '지금' 기준으로 잰다. 과거엔 last_update 기준이었는데,
        # last_update 는 스냅샷이 매칭될 때만 갱신되므로(update_all 의 snap 경로),
        # base 처럼 fallback 호가가 없는 체인에서 포지션이 시세 피드를 잃으면
        # last_update 가 멈춰 hold_hours 가 동결 → max_hold TIME_EXIT 안전망이
        # 영원히 발동 못 하던 버그(PITCH 40.7h / BNKR 51.8h 방치)를 유발했다.
        if not self.entry_timestamp:
            return 0.0
        try:
            entry_dt = parse_iso_utc(self.entry_timestamp)
            return (utc_now() - entry_dt).total_seconds() / 3600
        except Exception:
            return 0.0

    @property
    def liquidity_change_pct(self) -> float:
        if self.entry_liquidity == 0:
            return 0.0
        return (self.current_liquidity / self.entry_liquidity - 1) * 100

    @property
    def b_holders_change_pct(self) -> float:
        if self.entry_b_holders == 0:
            return 0.0
        return (self.current_b_holders / self.entry_b_holders - 1) * 100


# ============================================================
# 청산 엔진
# ============================================================

class ExitSignalEngine:
    def __init__(self, chain_configs: dict):
        self.configs = chain_configs
        self.early_stop_window_min = int(os.getenv("EARLY_STOP_WINDOW_MIN", "30") or 30)
        self.early_stop_threshold_pct = float(os.getenv("EARLY_STOP_THRESHOLD_PCT", "-7") or -7)
        log.info(
            "early stop config loaded: window=%smin threshold=%s%%",
            self.early_stop_window_min,
            self.early_stop_threshold_pct,
        )

    def evaluate(self, pos: Position) -> tuple[bool, ExitReason, str]:
        cfg = self.configs[pos.chain]
        pnl = pos.unrealized_pnl_pct
        drawdown = pos.drawdown_from_peak_pct

        held_minutes = pos.hold_hours * 60.0

        # 1. early stop loss (protect weak entries in the first 30 minutes)
        if held_minutes <= self.early_stop_window_min and pnl <= self.early_stop_threshold_pct:
            return True, ExitReason.EARLY_STOP_LOSS, (
                f"[{pos.chain}] early stop hit: {pnl:+.2f}% <= {self.early_stop_threshold_pct:+.0f}% within {held_minutes:.1f}m"
            )

        # 만기를 이미 넘긴 포지션은 STOP_LOSS 가 아니라 TIME_EXIT 로 청산한다(아래 4번).
        # 동결 등으로 max_hold 를 초과해 뒤늦게 평가될 때, 실제로는 시간 만기인 청산이
        # STOP_LOSS 로 오라벨링되고 reentry 쿨다운(720min)도 과도하게 길어지던 문제 방지.
        # (트레일링/익절 라벨은 그대로 우선 — 고점 후 하락은 TRAILING_STOP 이 더 정확)
        overdue = pos.hold_hours >= cfg.max_hold_hours

        # 2. stop loss
        if pnl <= cfg.stop_loss_pct and not overdue:
            return True, ExitReason.STOP_LOSS, (
                f"[{pos.chain}] 스탑 발동: {pnl:+.2f}% ≤ {cfg.stop_loss_pct:+.0f}%"
            )

        # 2. 트레일링 / profit lock
        if pos.peak_pnl_pct >= cfg.trailing_activation_pct:
            pos.trailing_active = True

        if not pos.profit_locked and pos.peak_pnl_pct >= cfg.take_profit_pct:
            pos.profit_locked = True

        if pos.profit_locked and pnl < cfg.take_profit_pct:
            return True, ExitReason.PROFIT_LOCK_BREAK, (
                f"[{pos.chain}] profit lock break: {pnl:+.2f}% < {cfg.take_profit_pct:+.0f}%"
            )

        if pos.trailing_active and drawdown <= -cfg.trailing_stop_pct:
            return True, ExitReason.TRAILING_STOP, (
                f"[{pos.chain}] trailing stop hit: {drawdown:+.2f}% from peak (peak {pos.peak_pnl_pct:+.2f}%)"
            )

        # 3. 익절
        if pnl >= cfg.take_profit_pct and not pos.profit_locked:
            pos.profit_locked = True
            return True, ExitReason.TAKE_PROFIT, (
                f"[{pos.chain}] take profit reached: {pnl:+.2f}% >= {cfg.take_profit_pct:+.0f}%"
            )

        # 4. 시간
        if pos.hold_hours >= cfg.max_hold_hours:
            return True, ExitReason.TIME_EXIT, (
                f"[{pos.chain}] time exit: held {pos.hold_hours:.1f}h, current {pnl:+.2f}%"
            )

        # 5. 유동성 급락
        # PnL 가드 추가(5/22): 유동성만 빠지고 가격 변동 거의 없는 false positive 방지
        # AORA -0.01%, LFI +1.96%, BEAN -1.20% 같은 케이스에서 청산되던 문제 해결
        # 진짜 crash는 가격도 같이 빠지므로 (pnl <= -5%) 통과
        if pos.liquidity_change_pct <= cfg.liquidity_crash_pct and pnl <= -5.0:
            return True, ExitReason.LIQUIDITY_CRASH, (
                f"[{pos.chain}] liquidity crash: liq={pos.liquidity_change_pct:+.2f}% pnl={pnl:+.2f}%"
            )

        # 6. B.holders (지원 체인만)
        if pos.entry_b_holders > 0 and pos.b_holders_change_pct <= cfg.b_holders_crash_pct:
            return True, ExitReason.HOLDER_EXODUS, (
                f"[{pos.chain}] holder exodus: {pos.b_holders_change_pct:+.2f}%"
            )

        return False, ExitReason.NONE, f"[{pos.chain}] holding ({pnl:+.2f}%, peak {pos.peak_pnl_pct:+.2f}%)"


# ============================================================
# 멀티체인 포지션 매니저
# ============================================================

class MultichainPositionManager:
    def __init__(self, chain_configs: dict, portfolio_config: PortfolioConfig,
                 storage_path: str = "mc_positions.jsonl",
                 state_path: Optional[str] = None,
                 mode: str = "live"):
        self.chain_configs = chain_configs
        self.portfolio_cfg = portfolio_config
        self.engine = ExitSignalEngine(chain_configs)
        self.storage = Path(storage_path)
        self.state_path = Path(state_path) if state_path else self.storage.with_name("portfolio_state.json")
        self.mode = mode
        self.halt_duration_hours = float(os.getenv("PORTFOLIO_HALT_DURATION_HOURS", "24") or 24)
        self.fallback_quote_timeout_minutes = int(os.getenv("FALLBACK_QUOTE_TIMEOUT_MINUTES", "30") or 30)

        self.positions: dict[str, Position] = {}   # key: f"{chain}:{contract}"
        self.initial_capital: float = 0
        self.peak_equity: float = 0
        self.realized_pnl_total: float = 0.0
        self.halt_mode: bool = False
        self.halt_recovery_equity: float = 0
        self.halt_until: Optional[str] = None

        self._load()
        self._load_state()
        env_capital = os.getenv("TOTAL_CAPITAL_USD")
        if env_capital:
            try:
                env_capital_val = float(env_capital)
                if env_capital_val > 0:
                    if self.initial_capital and abs(self.initial_capital - env_capital_val) > 0.01:
                        log.warning(
                            f"initial_capital recalibrated from ${self.initial_capital:.2f} "
                            f"to ${env_capital_val:.2f} (from TOTAL_CAPITAL_USD)"
                        )
                    self.initial_capital = env_capital_val
                    if self.mode == "dry_run":
                        if self.peak_equity != env_capital_val:
                            log.warning(
                                f"[dry_run] peak_equity recalibrated from "
                                f"${self.peak_equity:.2f} to ${env_capital_val:.2f}"
                            )
                            self.peak_equity = env_capital_val
                    else:
                        if self.peak_equity <= 0 or self.peak_equity < env_capital_val:
                            self.peak_equity = env_capital_val
            except ValueError:
                log.warning(f"TOTAL_CAPITAL_USD invalid: {env_capital}, ignoring")
        self._refresh_halt_state(force=True)
        self._save_state()

    def _pos_key(self, chain: str, contract: str) -> str:
        return f"{chain}:{contract}"

    def get_open_token_addresses(self, chain: Optional[str] = None) -> set[str]:
        addresses: set[str] = set()
        for pos in self.positions.values():
            if chain and pos.chain != chain:
                continue
            contract = str(pos.contract_address or "").strip().lower()
            if contract:
                addresses.add(contract)
        return addresses

    def _load(self):
        if not self.storage.exists():
            return
        with self.storage.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    pos = Position(**{k: v for k, v in d.items()
                                      if k in Position.__dataclass_fields__})
                    key = self._pos_key(pos.chain, pos.contract_address)
                    if pos.is_closed:
                        self.positions.pop(key, None)
                        realized = pos.realized_pnl_usd
                        if realized is None and pos.realized_pnl_pct is not None:
                            realized = pos.size_usd * (pos.realized_pnl_pct / 100)
                        self.realized_pnl_total += float(realized or 0.0)
                    else:
                        self.positions[key] = pos
                except Exception as e:
                    log.debug(f"jsonl load skipped: {e}")
        log.info(f"restored {len(self.positions)} open positions")

    def _persist(self, pos: Position):
        with self.storage.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(pos), ensure_ascii=False) + "\n")

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"portfolio state load failed: {e}")
            return

        self.initial_capital = float(data.get("initial_capital", self.initial_capital) or 0.0)
        self.peak_equity = float(data.get("peak_equity", self.peak_equity) or 0.0)
        self.realized_pnl_total = float(data.get("realized_pnl_total", self.realized_pnl_total) or 0.0)
        self.halt_mode = bool(data.get("halt_mode", self.halt_mode))
        self.halt_recovery_equity = float(data.get("halt_recovery_equity", self.halt_recovery_equity) or 0.0)
        self.halt_until = data.get("halt_until") or None

    def _save_state(self):
        payload = {
            "initial_capital": self.initial_capital,
            "peak_equity": self.peak_equity,
            "realized_pnl_total": self.realized_pnl_total,
            "halt_mode": self.halt_mode,
            "halt_recovery_equity": self.halt_recovery_equity,
            "halt_until": self.halt_until,
            "mode": self.mode,
            "saved_at": iso_utc_now(),
        }
        _atomic_write_json(self.state_path, payload)

    def _clear_halt(self):
        self.halt_mode = False
        self.halt_recovery_equity = 0
        self.halt_until = None

    def _refresh_halt_state(self, force: bool = False):
        if self.mode == "dry_run":
            if self.halt_mode or self.halt_until or self.halt_recovery_equity:
                self._clear_halt()
                self._save_state()
            return

        changed = False
        current_equity = self.current_equity()

        if self.peak_equity <= 0 and current_equity > 0:
            self.peak_equity = current_equity
            changed = True

        if self.halt_mode:
            if self.halt_until:
                try:
                    halt_until_dt = parse_iso_utc(self.halt_until)
                    if datetime.now(halt_until_dt.tzinfo) >= halt_until_dt:
                        self._clear_halt()
                        changed = True
                except Exception:
                    self._clear_halt()
                    changed = True
            if self.halt_mode and self.halt_recovery_equity and current_equity >= self.halt_recovery_equity:
                self._clear_halt()
                changed = True

        if force or changed:
            self._save_state()

    def reset_portfolio_halt(self, current_equity: Optional[float] = None):
        equity = float(current_equity if current_equity is not None else (self.current_equity() or self.initial_capital or 0.0))
        self._clear_halt()
        if equity > 0:
            if self.initial_capital <= 0:
                self.initial_capital = equity
            self.peak_equity = equity
        self._save_state()

    # --------------------------------------------------------
    # 포트폴리오 체크
    # --------------------------------------------------------

    def current_equity(self) -> float:
        """오픈 포지션 평가 포함 현재 자본 (근사)"""
        if self.initial_capital == 0:
            return 0.0
        unrealized_pnl_total = sum(
            p.size_usd * (p.unrealized_pnl_pct / 100.0)
            for p in self.positions.values()
        )
        realized_component = 0.0 if self.mode == "dry_run" else self.realized_pnl_total
        return self.initial_capital + realized_component + unrealized_pnl_total

    def portfolio_drawdown(self) -> float:
        eq = self.current_equity()
        if self.peak_equity == 0:
            return 0
        return (eq - self.peak_equity) / self.peak_equity

    def can_open(self, chain: str, contract: str) -> tuple[bool, str]:
        """진입 가능 여부 + 거부 사유"""
        self._refresh_halt_state()
        if self.mode != "dry_run" and self.halt_mode:
            return False, "portfolio_halt"

        key = self._pos_key(chain, contract)
        if key in self.positions:
            return False, "duplicate"

        # 체인별 한도
        chain_count = sum(1 for p in self.positions.values() if p.chain == chain)
        if chain_count >= self.chain_configs[chain].max_concurrent_positions:
            return False, f"{chain}_concurrent_full"

        # 전체 노출 한도
        if self.initial_capital > 0:
            total_exposure_pct = sum(
                p.size_usd / self.initial_capital * 100
                for p in self.positions.values()
            )
        else:
            total_exposure_pct = 0.0
        new_position_pct = self.chain_configs[chain].capital_per_position_pct
        if total_exposure_pct + new_position_pct > self.portfolio_cfg.max_total_exposure_pct:
            return False, "total_exposure_full"

        # 포트폴리오 MDD 체크
        if self.mode != "dry_run":
            dd = self.portfolio_drawdown()
            if dd <= self.portfolio_cfg.portfolio_mdd_halt:
                self.halt_mode = True
                self.halt_recovery_equity = self.peak_equity * self.portfolio_cfg.recovery_threshold
                self.halt_until = (datetime.now().astimezone() + timedelta(hours=self.halt_duration_hours)).astimezone().isoformat()
                self._save_state()
                return False, "mdd_halt"

        return True, "ok"

    # --------------------------------------------------------
    # 진입
    # --------------------------------------------------------

    def open_position(self, chain: str, token_snapshot: dict,
                      total_capital_usd: float) -> Optional[Position]:
        if self.initial_capital == 0:
            env_capital = os.getenv("TOTAL_CAPITAL_USD")
            if not env_capital:
                self.initial_capital = total_capital_usd
                if self.mode != "dry_run":
                    self.peak_equity = total_capital_usd

        contract = token_snapshot.get("contract_address")
        can, reason = self.can_open(chain, contract)
        if not can:
            log.warning(f"[{chain}] 진입 거부 {token_snapshot.get('symbol', '?')}: {reason}")
            return None

        cfg = self.chain_configs[chain]
        dd = self.portfolio_drawdown()
        fixed_size_env = {
            "bsc": "BSC_POSITION_SIZE_USD",
            "solana": "SOLANA_POSITION_SIZE_USD",
            "base": "BASE_POSITION_SIZE_USD",
        }.get(chain)
        fixed_size_usd = None
        fixed_size_raw = os.getenv(fixed_size_env) if fixed_size_env else None
        if fixed_size_raw:
            try:
                fixed_size_usd = float(fixed_size_raw)
                if fixed_size_usd <= 0:
                    fixed_size_usd = None
            except ValueError:
                fixed_size_usd = None

        if fixed_size_usd is not None:
            size_usd = fixed_size_usd
            if dd <= self.portfolio_cfg.portfolio_mdd_warn:
                size_usd *= 0.5
                log.warning(f"MDD 경고 구간, 포지션 사이즈 축소 → ${size_usd:.2f}")
            size_pct_for_log = (size_usd / self.initial_capital * 100) if self.initial_capital > 0 else 0.0
        else:
            size_pct = cfg.capital_per_position_pct
            if dd <= self.portfolio_cfg.portfolio_mdd_warn:
                size_pct *= 0.5
                log.warning(f"MDD 경고 구간, 포지션 사이즈 축소 → {size_pct:.1f}%")
            size_usd = total_capital_usd * (size_pct / 100)
            size_pct_for_log = size_pct
        now = iso_utc_now()

        pos = Position(
            chain=chain,
            symbol=token_snapshot["symbol"],
            contract_address=contract,
            entry_timestamp=now,
            entry_price=token_snapshot["price_usd"],
            entry_liquidity=token_snapshot["liquidity_usd"],
            entry_b_holders=token_snapshot.get("holders_binance", 0),
            size_usd=size_usd,
            token_amount=float(token_snapshot.get("_token_amount") or 0.0),
            entry_tx_hash=token_snapshot.get("_entry_tx_hash"),
            entry_path=str(token_snapshot.get("entry_path") or "reflexivity"),
            current_price=token_snapshot["price_usd"],
            peak_price=token_snapshot["price_usd"],
            current_liquidity=token_snapshot["liquidity_usd"],
            current_b_holders=token_snapshot.get("holders_binance", 0),
            last_update=now,
        )
        self.positions[self._pos_key(chain, contract)] = pos
        self._persist(pos)
        self._save_state()
        log.info(
            f"[{chain}] entry via {pos.entry_path}: {pos.symbol} @ ${pos.entry_price:.6f} "
            f"(${size_usd:,.0f}, {size_pct_for_log:.1f}%, liq=${pos.entry_liquidity/1000:.0f}K)"
        )
        return pos

    # --------------------------------------------------------
    # 업데이트 + 청산
    # --------------------------------------------------------

    def update_all(self, snapshots_by_chain: dict) -> list[tuple[Position, ExitReason, str]]:
        """
        snapshots_by_chain: {"bsc": [...], "base": [...], "solana": [...]}
        """
        exits = []
        now = iso_utc_now()

        # Halt mode 회복 체크
        if self.halt_mode:
            if self.current_equity() >= self.halt_recovery_equity:
                self.halt_mode = False
                log.info("Portfolio halt 해제")

        for key, pos in list(self.positions.items()):
            if self.mode != "dry_run":
                self.peak_equity = max(self.peak_equity, self.current_equity())
            chain_snaps = snapshots_by_chain.get(pos.chain, [])
            snap = next(
                (s for s in chain_snaps if s.get("contract_address") == pos.contract_address),
                None,
            )
            if not snap:
                if pos.fallback_missing_since is None:
                    pos.fallback_missing_since = now
                try:
                    missing_minutes = max(0.0, (parse_iso_utc(now) - parse_iso_utc(pos.fallback_missing_since)).total_seconds() / 60.0)
                except Exception:
                    missing_minutes = 0.0

                # 데이터 부재(snap=None) 단독으로는 LIQUIDITY_CRASH 청산하지 않는다 (6/05).
                # 기존 버그: fallback timeout이 PnL/유동성 가드 없이 청산 → LFI(+9.70%),
                # aeon(+1.92%), hTEA(+2.48%) 같은 플러스 포지션이 강제 청산되던 문제.
                # 마지막 유효 관측 기준으로 (a) 실제 유동성 급감 + 손실일 때만 LIQUIDITY_CRASH,
                # (b) 그 외에는 보유 유지, max_hold_hours 도달 시 TIME_EXIT로 정상 청산.
                cfg = self.chain_configs[pos.chain]
                pnl = pos.unrealized_pnl_pct                 # 마지막 유효 가격 기준
                liq_change = pos.liquidity_change_pct        # 마지막 유효 snapshot 기준
                real_crash = (liq_change <= cfg.liquidity_crash_pct) and (pnl <= -5.0)

                if missing_minutes >= self.fallback_quote_timeout_minutes and real_crash:
                    exits.append((
                        pos,
                        ExitReason.LIQUIDITY_CRASH,
                        f"fallback_timeout_{self.fallback_quote_timeout_minutes}min_"
                        f"liq{liq_change:+.2f}%_pnl{pnl:+.2f}%",
                    ))
                elif pos.hold_hours >= cfg.max_hold_hours:
                    # quote 영구 미수신 대비: 보유시간 한도 도달 시 정상 TIME_EXIT
                    exits.append((
                        pos,
                        ExitReason.TIME_EXIT,
                        f"fallback_time_exit_{pos.hold_hours:.1f}h_pnl{pnl:+.2f}%",
                    ))
                else:
                    log.warning(
                        f"[{pos.chain}] {pos.symbol}: fallback quote missing for {missing_minutes:.0f}min "
                        f"(timeout {self.fallback_quote_timeout_minutes}min) — holding "
                        f"(pnl={pnl:+.2f}% liq={liq_change:+.2f}%)"
                    )
                continue

            pos.fallback_missing_since = None
            pos.current_price = snap["price_usd"]
            pos.peak_price = max(pos.peak_price, snap["price_usd"])
            pos.current_liquidity = snap["liquidity_usd"]
            pos.current_b_holders = snap.get("holders_binance", pos.current_b_holders)
            pos.last_update = now

            if snap.get("_fallback_disappearance"):
                if pos.trending_lost_since is None:
                    pos.trending_lost_since = now
                try:
                    lost_minutes = max(0.0, (parse_iso_utc(now) - parse_iso_utc(pos.trending_lost_since)).total_seconds() / 60.0)
                except Exception:
                    lost_minutes = 0.0
                log.warning(
                    f"[{pos.chain}] {pos.symbol}: trending_lost_fallback_active for {lost_minutes:.0f}min "
                    f"(price=${pos.current_price:.6f} liq=${pos.current_liquidity:,.0f})"
                )
            else:
                pos.trending_lost_since = None

            should_exit, reason, msg = self.engine.evaluate(pos)
            if should_exit:
                if reason == ExitReason.LIQUIDITY_CRASH:
                    exits.append((
                        pos,
                        reason,
                        f"real_liquidity_drop_{pos.liquidity_change_pct:+.2f}%",
                    ))
                else:
                    exits.append((pos, reason, msg))
            else:
                log.debug(msg)

        if self.mode != "dry_run":
            self._save_state()
        return exits

    def close_position(
        self,
        pos: Position,
        reason: ExitReason,
        msg: str,
        *,
        exit_price: Optional[float] = None,
        realized_pnl_pct: Optional[float] = None,
        realized_pnl_usd: Optional[float] = None,
        exit_tx_hash: Optional[str] = None,
    ):
        pos.is_closed = True
        pos.exit_reason = reason.value
        pos.exit_timestamp = iso_utc_now()
        pos.exit_price = exit_price if exit_price is not None else pos.current_price
        pos.current_price = pos.exit_price
        pos.realized_pnl_pct = realized_pnl_pct if realized_pnl_pct is not None else pos.unrealized_pnl_pct
        if realized_pnl_pct is not None:
            pos.realized_pnl_pct = realized_pnl_pct
        elif pos.entry_price and pos.exit_price is not None:
            pos.realized_pnl_pct = ((pos.exit_price - pos.entry_price) / pos.entry_price) * 100.0
        else:
            pos.realized_pnl_pct = pos.unrealized_pnl_pct

        if realized_pnl_usd is not None:
            pos.realized_pnl_usd = realized_pnl_usd
        elif pos.token_amount and pos.entry_price and pos.exit_price is not None:
            pos.realized_pnl_usd = float(pos.token_amount) * (float(pos.exit_price) - float(pos.entry_price))
        else:
            pos.realized_pnl_usd = pos.size_usd * (pos.realized_pnl_pct / 100.0)
        pos.exit_tx_hash = exit_tx_hash
        pos.exit_tx_hash = exit_tx_hash

        key = self._pos_key(pos.chain, pos.contract_address)
        del self.positions[key]
        self._persist(pos)

        self.realized_pnl_total += pos.realized_pnl_usd

        # Peak equity 업데이트
        new_equity = self.current_equity()
        self.peak_equity = max(self.peak_equity, new_equity)

        icon = 'WIN' if pos.realized_pnl_pct > 0 else 'LOSS'
        log.info(f"{icon} [{pos.chain}] closed: {pos.symbol} "
                 f"{pos.realized_pnl_pct:+.2f}% (${pos.realized_pnl_usd:+,.2f}) - {msg}")
        return pos

    # --------------------------------------------------------
    # 리포트
    # --------------------------------------------------------

    def summary(self) -> str:
        if not self.positions:
            return "No open positions"

        lines = [f"-- Open Positions: {len(self.positions)} --"]
        by_chain = {}
        for pos in self.positions.values():
            by_chain.setdefault(pos.chain, []).append(pos)

        for chain in ["bsc", "solana", "base"]:
            if chain not in by_chain:
                continue
            lines.append(f"\n[{chain.upper()}] {len(by_chain[chain])}")
            for pos in by_chain[chain]:
                pnl = pos.unrealized_pnl_pct
                peak = pos.peak_pnl_pct
                trail = "ON" if pos.trailing_active else "WAIT"
                lines.append(
                    f"  {pos.symbol:<8} "
                    f"{pnl:>+7.2f}% (peak {peak:>+6.2f}%) "
                    f"held {pos.hold_hours:.1f}h trail {trail}"
                )
        return "\n".join(lines)


# ============================================================
# 단위 테스트
# ============================================================

def test_multichain():
    print("=" * 75)
    print("  Multichain Position Manager Unit Test")
    print("=" * 75)

    pf_cfg = PortfolioConfig(
        chain_allocation={"bsc": 0.25, "solana": 0.20, "base": 0.55},
    )
    pm = MultichainPositionManager(
        CHAIN_CONFIGS, pf_cfg, storage_path="/tmp/test_mc_positions.jsonl"
    )

    # BSC 진입
    pm.open_position("bsc", {
        "symbol": "BTKA", "contract_address": "0xbsc1",
        "price_usd": 0.05, "liquidity_usd": 800_000, "holders_binance": 200,
    }, total_capital_usd=10_000)

    # Solana 진입
    pm.open_position("solana", {
        "symbol": "STKB", "contract_address": "sol1",
        "price_usd": 0.0001, "liquidity_usd": 300_000, "holders_binance": 0,
    }, total_capital_usd=10_000)

    # Base 진입
    pm.open_position("base", {
        "symbol": "BASE1", "contract_address": "0xbase1",
        "price_usd": 1.20, "liquidity_usd": 1_500_000, "holders_binance": 0,
    }, total_capital_usd=10_000)

    print("\n" + pm.summary())

    # 시간 경과 시뮬레이션
    for h, updates, desc in [
        (6, {
            "bsc":    [{"contract_address": "0xbsc1", "price_usd": 0.058, "liquidity_usd": 810_000, "holders_binance": 205}],
            "solana": [{"contract_address": "sol1",   "price_usd": 0.00012, "liquidity_usd": 320_000}],
            "base":   [{"contract_address": "0xbase1","price_usd": 1.28,  "liquidity_usd": 1_510_000}],
        }, "6시간 — 모두 소폭 상승"),
        (12, {
            "bsc":    [{"contract_address": "0xbsc1", "price_usd": 0.065, "liquidity_usd": 815_000, "holders_binance": 210}],
            "solana": [{"contract_address": "sol1",   "price_usd": 0.00007, "liquidity_usd": 305_000}],   # -30%
            "base":   [{"contract_address": "0xbase1","price_usd": 1.35,  "liquidity_usd": 1_520_000}],
        }, "12시간 — Solana 급락"),
        (20, {
            "bsc":    [{"contract_address": "0xbsc1", "price_usd": 0.055, "liquidity_usd": 805_000, "holders_binance": 208}],  # 고점 대비 -15.4%
            "solana": [],  # 사라짐
            "base":   [{"contract_address": "0xbase1","price_usd": 1.40,  "liquidity_usd": 1_550_000}],
        }, "20시간 — Solana 사라짐, BSC 트레일링"),
    ]:
        print(f"\n[t={h}h] {desc}")
        for pos in pm.positions.values():
            pos.last_update = (
                parse_iso_utc(list(pm.positions.values())[0].entry_timestamp)
                + timedelta(hours=h)
            ).isoformat()

        exits = pm.update_all(updates)
        for pos, reason, msg in exits:
            pm.close_position(pos, reason, msg)
        print(pm.summary())


if __name__ == "__main__":
    import os
    os.makedirs("/tmp", exist_ok=True)
    # 기존 테스트 파일 삭제
    Path("/tmp/test_mc_positions.jsonl").unlink(missing_ok=True)
    test_multichain()
