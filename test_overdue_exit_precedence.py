"""만기 초과 포지션의 청산 라벨 우선순위 테스트.

버그: evaluate() 에서 STOP_LOSS 가 TIME_EXIT 보다 먼저라, 동결 등으로 max_hold 를
넘겨 뒤늦게 평가되는 underwater 포지션이 STOP_LOSS 로 오라벨링되고 reentry
쿨다운도 과도하게 길어졌다(720min vs 240min). 수정: 만기 초과 시 STOP_LOSS 는
TIME_EXIT 에 양보한다. 단 트레일링/익절 라벨은 그대로 우선(더 정확).
"""
from datetime import timedelta

from trading.time_utils import utc_now
from mc_position_manager import ChainConfig, ExitReason, ExitSignalEngine, Position


def _cfg(max_hold_hours=8.0):
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


def _pos(hours_held, *, entry_price=1.0, current_price=1.0, peak_price=None):
    entry_iso = (utc_now() - timedelta(hours=hours_held)).isoformat()
    return Position(
        chain="base",
        symbol="TEST",
        contract_address="0xtest",
        entry_timestamp=entry_iso,
        entry_price=entry_price,
        entry_liquidity=100_000.0,
        entry_b_holders=0,
        size_usd=50.0,
        current_price=current_price,
        peak_price=peak_price if peak_price is not None else current_price,
        current_liquidity=100_000.0,
        last_update=utc_now().isoformat(),
    )


def test_overdue_underwater_is_time_exit_not_stop_loss():
    # PITCH/BNKR 케이스: 만기(8h) 초과 + 고점 없이 -25% (peak==entry).
    eng = ExitSignalEngine(_cfg())
    pos = _pos(hours_held=40.0, current_price=0.75)  # -25%, 동결이라 peak==entry
    should_exit, reason, _ = eng.evaluate(pos)
    assert should_exit
    assert reason == ExitReason.TIME_EXIT, f"expected TIME_EXIT, got {reason}"


def test_normal_underwater_still_stop_loss():
    # 만기 전(3h < 8h) -25% 는 기존대로 STOP_LOSS (거동 불변).
    eng = ExitSignalEngine(_cfg())
    pos = _pos(hours_held=3.0, current_price=0.75)
    should_exit, reason, _ = eng.evaluate(pos)
    assert should_exit
    assert reason == ExitReason.STOP_LOSS, f"expected STOP_LOSS, got {reason}"


def test_overdue_after_peak_is_trailing_stop():
    # 만기 초과지만 고점을 찍고 내려온 경우는 TRAILING_STOP 이 더 정확 → 보존.
    # peak +30% (활성화 10% 초과), 현재 +5% → 고점 대비 -19.2% drawdown (>15%).
    eng = ExitSignalEngine(_cfg())
    pos = _pos(hours_held=40.0, current_price=1.05, peak_price=1.30)
    should_exit, reason, _ = eng.evaluate(pos)
    assert should_exit
    assert reason == ExitReason.TRAILING_STOP, f"expected TRAILING_STOP, got {reason}"


def test_overdue_profitable_is_time_exit():
    # 만기 초과 + 소폭 수익(고점 미미) → TIME_EXIT.
    eng = ExitSignalEngine(_cfg())
    pos = _pos(hours_held=40.0, current_price=1.03, peak_price=1.03)
    should_exit, reason, _ = eng.evaluate(pos)
    assert should_exit
    assert reason == ExitReason.TIME_EXIT, f"expected TIME_EXIT, got {reason}"


if __name__ == "__main__":
    test_overdue_underwater_is_time_exit_not_stop_loss()
    test_normal_underwater_still_stop_loss()
    test_overdue_after_peak_is_trailing_stop()
    test_overdue_profitable_is_time_exit()
    print("ok: overdue exit precedence tests passed")
