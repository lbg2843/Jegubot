"""shadow ETH 레짐 차단 실험 룰 테스트.

shadow_e_block_eth_up / shadow_f_block_eth_up_strong 가 ETH 4h 레짐에 따라
would_block 을 올바르게 기록하는지 검증. (라이브 거동엔 영향 없음 — 기록 전용)
"""
import shadow_rules


def _tok():
    return {"chain": "base", "entry_path": "sweet_spot", "symbol": "TEST",
            "contract_address": "0xtest"}


def _gate_pass(token, chain):
    return True, "passed"


def _gate_fail(token, chain):
    return False, "low_liquidity"


def _set_regime(monkeypatch, regime):
    monkeypatch.setattr(shadow_rules, "_current_eth_4h_regime", lambda: regime)


def test_up_regime_blocks_e_and_f(monkeypatch):
    _set_regime(monkeypatch, "up")
    res = shadow_rules.evaluate_shadow(_tok(), _gate_pass)
    assert res["shadow_e_block_eth_up"]["passed"] is False
    assert "blocked_up" in res["shadow_e_block_eth_up"]["reason"]
    assert res["shadow_f_block_eth_up_strong"]["passed"] is False


def test_flat_regime_blocks_neither(monkeypatch):
    _set_regime(monkeypatch, "flat")
    res = shadow_rules.evaluate_shadow(_tok(), _gate_pass)
    assert res["shadow_e_block_eth_up"]["passed"] is True
    assert res["shadow_f_block_eth_up_strong"]["passed"] is True


def test_strong_up_blocks_only_f(monkeypatch):
    _set_regime(monkeypatch, "strong_up")
    res = shadow_rules.evaluate_shadow(_tok(), _gate_pass)
    assert res["shadow_e_block_eth_up"]["passed"] is True          # up 만 차단
    assert res["shadow_f_block_eth_up_strong"]["passed"] is False  # up+strong_up 차단


def test_gate_fail_is_not_relabeled_as_regime_block(monkeypatch):
    # 현 게이트가 이미 막은 진입은 레짐 차단으로 둔갑하면 안 된다(현 게이트와 동일).
    _set_regime(monkeypatch, "up")
    res = shadow_rules.evaluate_shadow(_tok(), _gate_fail)
    assert res["shadow_e_block_eth_up"]["passed"] is False
    assert res["shadow_e_block_eth_up"]["reason"] == "low_liquidity"


def test_unknown_regime_is_safe_passthrough(monkeypatch):
    # 레짐 조회 실패('unknown')면 어떤 차단 리스트에도 없어 현 게이트와 동일.
    _set_regime(monkeypatch, "unknown")
    res = shadow_rules.evaluate_shadow(_tok(), _gate_pass)
    assert res["shadow_e_block_eth_up"]["passed"] is True
    assert res["shadow_f_block_eth_up_strong"]["passed"] is True


if __name__ == "__main__":
    class _MP:
        def __init__(self): self._u = []
        def setattr(self, obj, name, val):
            self._u.append((obj, name, getattr(obj, name))); setattr(obj, name, val)
        def undo(self):
            for o, n, v in reversed(self._u): setattr(o, n, v)
    for fn in (test_up_regime_blocks_e_and_f, test_flat_regime_blocks_neither,
               test_strong_up_blocks_only_f, test_gate_fail_is_not_relabeled_as_regime_block,
               test_unknown_regime_is_safe_passthrough):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("ok: shadow eth regime experiment tests passed")
