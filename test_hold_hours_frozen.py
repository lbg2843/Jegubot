"""hold_hours 동결 버그 회귀 테스트.

버그: hold_hours 가 last_update 기준이었는데, base 처럼 fallback 호가가 없는
체인에서 포지션이 시세 피드를 잃으면 last_update 가 멈춰 hold_hours 가 동결되고,
update_all 의 snap=None fallback TIME_EXIT(`hold_hours >= max_hold`)이 영원히
발동하지 못해 PITCH(40.7h)/BNKR(51.8h)가 수십 시간 방치됐다.

수정: hold_hours 를 utc_now() 기준으로 계산 → 피드가 끊겨도 시간은 흐르므로
max_hold 도달 시 안전망이 정상 발동한다.
"""
import tempfile
from datetime import timedelta
from pathlib import Path

from trading.time_utils import utc_now
from mc_position_manager import (
    ChainConfig,
    ExitReason,
    MultichainPositionManager,
    PortfolioConfig,
    Position,
)


def _base_cfg(max_hold_hours: float) -> dict:
    return {
        "base": ChainConfig(
            chain="base",
            stop_loss_pct=-20.0,
            trailing_activation_pct=10.0,
            trailing_stop_pct=15.0,
            take_profit_pct=40.0,
            max_hold_hours=max_hold_hours,
            capital_per_position_pct=6.0,
            max_concurrent_positions=4,
            liquidity_crash_pct=-20.0,
            b_holders_crash_pct=-10.0,
        )
    }


def _frozen_position(hours_held: float) -> Position:
    """진입 직후 시세 피드를 잃은 포지션: last_update 가 entry 에 멈춰 있고
    가격도 진입가에 동결(pnl 0%)된 상태."""
    entry_dt = utc_now() - timedelta(hours=hours_held)
    entry_iso = entry_dt.isoformat()
    price = 1.0
    return Position(
        chain="base",
        symbol="FROZEN",
        contract_address="0xfrozen",
        entry_timestamp=entry_iso,
        entry_price=price,
        entry_liquidity=100_000.0,
        entry_b_holders=0,
        size_usd=50.0,
        current_price=price,        # 동결: 진입가 그대로
        peak_price=price,           # peak == entry (한 번도 갱신 안 됨)
        current_liquidity=100_000.0,
        last_update=entry_iso,      # 동결: 진입 시각에 멈춤
    )


def test_hold_hours_uses_now_not_last_update():
    # last_update 가 진입 시각에 멈춰 있어도 실제 경과시간을 반영해야 한다.
    pos = _frozen_position(hours_held=10.0)
    assert pos.hold_hours >= 9.9, f"expected ~10h, got {pos.hold_hours:.2f}h"


def test_hold_hours_robust_to_empty_last_update():
    pos = _frozen_position(hours_held=5.0)
    pos.last_update = ""  # last_update 없어도 entry 기준으로 계산
    assert pos.hold_hours >= 4.9, f"expected ~5h, got {pos.hold_hours:.2f}h"


def test_frozen_position_time_exits_at_max_hold():
    """핵심 회귀: snap 이 영영 안 오는 동결 포지션이 max_hold 도달 시
    fallback TIME_EXIT 으로 청산돼야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = MultichainPositionManager(
            _base_cfg(max_hold_hours=8.0),
            PortfolioConfig(),
            storage_path=str(Path(tmp) / "pos.jsonl"),
            state_path=str(Path(tmp) / "state.json"),
            mode="dry_run",
        )
        pos = _frozen_position(hours_held=10.0)  # 8h 한도 초과
        pm.positions["base:0xfrozen"] = pos

        # 빈 스냅샷 → snap=None fallback 경로
        exits = pm.update_all({"base": []})

        reasons = [r for (_p, r, _m) in exits]
        assert ExitReason.TIME_EXIT in reasons, (
            f"동결 포지션이 max_hold 에서 TIME_EXIT 되지 않음. exits={reasons}"
        )


def test_fresh_frozen_position_not_exited_before_max_hold():
    """아직 한도 전(2h < 8h)인 동결 포지션은 청산되지 않아야 한다(과청산 방지)."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = MultichainPositionManager(
            _base_cfg(max_hold_hours=8.0),
            PortfolioConfig(),
            storage_path=str(Path(tmp) / "pos.jsonl"),
            state_path=str(Path(tmp) / "state.json"),
            mode="dry_run",
        )
        pos = _frozen_position(hours_held=2.0)
        pm.positions["base:0xfrozen"] = pos
        exits = pm.update_all({"base": []})
        assert not exits, f"한도 전인데 청산됨: {[(r) for (_p, r, _m) in exits]}"


if __name__ == "__main__":
    test_hold_hours_uses_now_not_last_update()
    test_hold_hours_robust_to_empty_last_update()
    test_frozen_position_time_exits_at_max_hold()
    test_fresh_frozen_position_not_exited_before_max_hold()
    print("ok: hold_hours frozen-position regression tests passed")
