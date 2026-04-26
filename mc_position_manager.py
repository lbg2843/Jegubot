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
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from trading.time_utils import iso_utc_now, parse_iso_utc


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mc_position_mgr")


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
        stop_loss_pct=-20.0,
        trailing_activation_pct=10.0,
        trailing_stop_pct=15.0,
        take_profit_pct=80.0,
        max_hold_hours=72,
        capital_per_position_pct=4.0,
        max_concurrent_positions=3,
        liquidity_crash_pct=-20.0,
        b_holders_crash_pct=-10.0,
    ),
    "solana": ChainConfig(
        chain="solana",
        stop_loss_pct=-25.0,
        trailing_activation_pct=12.0,
        trailing_stop_pct=18.0,
        take_profit_pct=100.0,
        max_hold_hours=48,
        capital_per_position_pct=1.5,
        max_concurrent_positions=4,
        liquidity_crash_pct=-25.0,
        b_holders_crash_pct=-15.0,
    ),
    # "base": ChainConfig(
    #     chain="base",
    #     stop_loss_pct=-15.0,
    #     trailing_activation_pct=8.0,
    #     trailing_stop_pct=12.0,
    #     take_profit_pct=60.0,
    #     max_hold_hours=96,
    #     capital_per_position_pct=6.0,
    #     max_concurrent_positions=2,
    #     liquidity_crash_pct=-18.0,
    #     b_holders_crash_pct=-10.0,
    # ),
}

# Disabled 2026-04-25 due to consistent losses (-$89 over 8 trades).
# Kept separately so historical analysis and legacy open positions can
# still resolve Base parameters without allowing new Base allocations.
DISABLED_CHAIN_CONFIGS = {
    "base": ChainConfig(
        chain="base",
        stop_loss_pct=-15.0,
        trailing_activation_pct=8.0,
        trailing_stop_pct=12.0,
        take_profit_pct=60.0,
        max_hold_hours=96,
        capital_per_position_pct=6.0,
        max_concurrent_positions=2,
        liquidity_crash_pct=-18.0,
        b_holders_crash_pct=-10.0,
    ),
}


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
    max_total_exposure_pct: float = 15.0  # 동시 노출 자본의 15%까지만


# ============================================================
# 청산 사유 + 포지션
# ============================================================

class ExitReason(Enum):
    NONE = "NONE"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
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

    # 청산
    is_closed: bool = False
    exit_reason: str = ExitReason.NONE.value
    exit_timestamp: Optional[str] = None
    exit_price: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    exit_tx_hash: Optional[str] = None

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
        if not self.entry_timestamp or not self.last_update:
            return 0.0
        try:
            entry_dt = parse_iso_utc(self.entry_timestamp)
            now_dt = parse_iso_utc(self.last_update)
            return (now_dt - entry_dt).total_seconds() / 3600
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

    def evaluate(self, pos: Position) -> tuple[bool, ExitReason, str]:
        cfg = self.configs[pos.chain]
        pnl = pos.unrealized_pnl_pct
        drawdown = pos.drawdown_from_peak_pct

        # 1. 스탑로스
        if pnl <= cfg.stop_loss_pct:
            return True, ExitReason.STOP_LOSS, (
                f"[{pos.chain}] 스탑 발동: {pnl:+.2f}% ≤ {cfg.stop_loss_pct:+.0f}%"
            )

        # 2. 트레일링
        if pnl > cfg.trailing_activation_pct:
            pos.trailing_active = True
        if pos.trailing_active and drawdown <= -cfg.trailing_stop_pct:
            return True, ExitReason.TRAILING_STOP, (
                f"[{pos.chain}] 트레일링: 고점 {drawdown:+.2f}% (peak {pos.peak_pnl_pct:+.2f}%)"
            )

        # 3. 익절
        if pnl >= cfg.take_profit_pct:
            return True, ExitReason.TAKE_PROFIT, (
                f"[{pos.chain}] 익절: {pnl:+.2f}% ≥ {cfg.take_profit_pct:+.0f}%"
            )

        # 4. 시간
        if pos.hold_hours >= cfg.max_hold_hours:
            return True, ExitReason.TIME_EXIT, (
                f"[{pos.chain}] 시간 청산: {pos.hold_hours:.1f}h, 현재 {pnl:+.2f}%"
            )

        # 5. 유동성 급락
        if pos.liquidity_change_pct <= cfg.liquidity_crash_pct:
            return True, ExitReason.LIQUIDITY_CRASH, (
                f"[{pos.chain}] 유동성 급락: {pos.liquidity_change_pct:+.2f}%"
            )

        # 6. B.holders (지원 체인만)
        if pos.entry_b_holders > 0 and pos.b_holders_change_pct <= cfg.b_holders_crash_pct:
            return True, ExitReason.HOLDER_EXODUS, (
                f"[{pos.chain}] 홀더 이탈: {pos.b_holders_change_pct:+.2f}%"
            )

        return False, ExitReason.NONE, f"[{pos.chain}] 보유 ({pnl:+.2f}%, peak {pos.peak_pnl_pct:+.2f}%)"


# ============================================================
# 멀티체인 포지션 매니저
# ============================================================

class MultichainPositionManager:
    def __init__(self, chain_configs: dict, portfolio_config: PortfolioConfig,
                 storage_path: str = "mc_positions.jsonl"):
        self.chain_configs = chain_configs
        self.portfolio_cfg = portfolio_config
        self.engine = ExitSignalEngine(chain_configs)
        self.storage = Path(storage_path)

        self.positions: dict[str, Position] = {}   # key: f"{chain}:{contract}"
        self.initial_capital: float = 0
        self.peak_equity: float = 0
        self.halt_mode: bool = False
        self.halt_recovery_equity: float = 0

        self._load()

    def _pos_key(self, chain: str, contract: str) -> str:
        return f"{chain}:{contract}"

    def _load(self):
        if not self.storage.exists():
            return
        with self.storage.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    pos = Position(**{k: v for k, v in d.items()
                                      if k in Position.__dataclass_fields__})
                    if not pos.is_closed:
                        key = self._pos_key(pos.chain, pos.contract_address)
                        self.positions[key] = pos
                except Exception as e:
                    log.debug(f"라인 스킵: {e}")
        log.info(f"오픈 포지션 {len(self.positions)}개 복구")

    def _persist(self, pos: Position):
        with self.storage.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(pos), ensure_ascii=False) + "\n")

    # --------------------------------------------------------
    # 포트폴리오 체크
    # --------------------------------------------------------

    def current_equity(self) -> float:
        """오픈 포지션 평가 포함 현재 자본 (근사)"""
        if self.initial_capital == 0:
            return 0
        # 이 구현은 단순화 — 실제론 실현 손익만 반영
        # 미실현 손익은 별도 계산 필요
        return self.initial_capital  # placeholder

    def portfolio_drawdown(self) -> float:
        eq = self.current_equity()
        if self.peak_equity == 0:
            return 0
        return (eq - self.peak_equity) / self.peak_equity

    def can_open(self, chain: str, contract: str) -> tuple[bool, str]:
        """진입 가능 여부 + 거부 사유"""
        if self.halt_mode:
            return False, "portfolio_halt"

        key = self._pos_key(chain, contract)
        if key in self.positions:
            return False, "duplicate"

        # 체인별 한도
        chain_count = sum(1 for p in self.positions.values() if p.chain == chain)
        if chain_count >= self.chain_configs[chain].max_concurrent_positions:
            return False, f"{chain}_concurrent_full"

        # 전체 노출 한도
        total_exposure_pct = sum(
            p.size_usd / self.initial_capital * 100
            for p in self.positions.values()
        )
        new_position_pct = self.chain_configs[chain].capital_per_position_pct
        if total_exposure_pct + new_position_pct > self.portfolio_cfg.max_total_exposure_pct:
            return False, "total_exposure_full"

        # 포트폴리오 MDD 체크
        dd = self.portfolio_drawdown()
        if dd <= self.portfolio_cfg.portfolio_mdd_halt:
            self.halt_mode = True
            self.halt_recovery_equity = self.peak_equity * self.portfolio_cfg.recovery_threshold
            return False, "mdd_halt"

        return True, "ok"

    # --------------------------------------------------------
    # 진입
    # --------------------------------------------------------

    def open_position(self, chain: str, token_snapshot: dict,
                      total_capital_usd: float) -> Optional[Position]:
        if self.initial_capital == 0:
            self.initial_capital = total_capital_usd
            self.peak_equity = total_capital_usd

        contract = token_snapshot.get("contract_address")
        can, reason = self.can_open(chain, contract)
        if not can:
            log.warning(f"[{chain}] 진입 거부 {token_snapshot.get('symbol', '?')}: {reason}")
            return None

        cfg = self.chain_configs[chain]
        dd = self.portfolio_drawdown()
        # Circuit breaker: MDD 경고 시 포지션 크기 절반
        size_pct = cfg.capital_per_position_pct
        if dd <= self.portfolio_cfg.portfolio_mdd_warn:
            size_pct *= 0.5
            log.warning(f"MDD 경고 구간, 포지션 사이즈 축소 → {size_pct:.1f}%")

        size_usd = total_capital_usd * (size_pct / 100)
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
            current_price=token_snapshot["price_usd"],
            peak_price=token_snapshot["price_usd"],
            current_liquidity=token_snapshot["liquidity_usd"],
            current_b_holders=token_snapshot.get("holders_binance", 0),
            last_update=now,
        )
        self.positions[self._pos_key(chain, contract)] = pos
        self._persist(pos)
        log.info(f"▶ [{chain}] 진입: {pos.symbol} @ ${pos.entry_price:.6f} "
                 f"(${size_usd:,.0f}, {size_pct:.1f}%, 유동성 ${pos.entry_liquidity/1000:.0f}K)")
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
            chain_snaps = snapshots_by_chain.get(pos.chain, [])
            snap = next(
                (s for s in chain_snaps if s.get("contract_address") == pos.contract_address),
                None,
            )
            if not snap:
                log.warning(f"[{pos.chain}] {pos.symbol}: Trending에서 사라짐 → 긴급 청산")
                exits.append((pos, ExitReason.LIQUIDITY_CRASH,
                             "Trending 페이지에서 제거됨"))
                continue

            pos.current_price = snap["price_usd"]
            pos.peak_price = max(pos.peak_price, snap["price_usd"])
            pos.current_liquidity = snap["liquidity_usd"]
            pos.current_b_holders = snap.get("holders_binance", pos.current_b_holders)
            pos.last_update = now

            should_exit, reason, msg = self.engine.evaluate(pos)
            if should_exit:
                exits.append((pos, reason, msg))
            else:
                log.debug(msg)

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
        pos.realized_pnl_usd = (
            realized_pnl_usd
            if realized_pnl_usd is not None
            else pos.size_usd * (pos.realized_pnl_pct / 100)
        )
        pos.exit_tx_hash = exit_tx_hash

        key = self._pos_key(pos.chain, pos.contract_address)
        del self.positions[key]
        self._persist(pos)

        # Peak equity 업데이트
        new_equity = self.current_equity() + pos.realized_pnl_usd
        self.peak_equity = max(self.peak_equity, new_equity)

        icon = "✓" if pos.realized_pnl_pct > 0 else "✗"
        log.info(f"{icon} [{pos.chain}] 청산: {pos.symbol} "
                 f"{pos.realized_pnl_pct:+.2f}% (${pos.realized_pnl_usd:+,.2f}) — {msg}")
        return pos

    # --------------------------------------------------------
    # 리포트
    # --------------------------------------------------------

    def summary(self) -> str:
        if not self.positions:
            return "오픈 포지션 없음"

        lines = [f"── 오픈 포지션 {len(self.positions)}개 ──"]
        by_chain = {}
        for pos in self.positions.values():
            by_chain.setdefault(pos.chain, []).append(pos)

        for chain in ["bsc", "solana", "base"]:
            if chain not in by_chain:
                continue
            lines.append(f"\n[{chain.upper()}] {len(by_chain[chain])}개")
            for pos in by_chain[chain]:
                pnl = pos.unrealized_pnl_pct
                peak = pos.peak_pnl_pct
                trail = "ON" if pos.trailing_active else "대기"
                lines.append(
                    f"  {pos.symbol:<8} "
                    f"{pnl:>+7.2f}% (peak {peak:>+6.2f}%) "
                    f"보유 {pos.hold_hours:.1f}h 트레일 {trail}"
                )
        return "\n".join(lines)


# ============================================================
# 단위 테스트
# ============================================================

def test_multichain():
    print("=" * 75)
    print("  멀티체인 포지션 매니저 단위 테스트")
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
