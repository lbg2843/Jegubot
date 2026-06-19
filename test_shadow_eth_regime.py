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


def test_disable_sweet_spot_blocks_sweet_spot(monkeypatch):
    _set_regime(monkeypatch, "flat")  # 레짐 무관, 경로만
    sweet = dict(_tok(), entry_path="sweet_spot")
    gz = dict(_tok(), entry_path="golden_zone")
    res_sweet = shadow_rules.evaluate_shadow(sweet, _gate_pass)
    res_gz = shadow_rules.evaluate_shadow(gz, _gate_pass)
    assert res_sweet["shadow_g_disable_sweet_spot"]["passed"] is False
    assert res_gz["shadow_g_disable_sweet_spot"]["passed"] is True


def test_block_up_sweet_spot_combo(monkeypatch):
    # up x sweet_spot 만 차단. up x golden_zone, flat x sweet_spot 는 통과.
    _set_regime(monkeypatch, "up")
    sweet = dict(_tok(), entry_path="sweet_spot")
    gz = dict(_tok(), entry_path="golden_zone")
    assert shadow_rules.evaluate_shadow(sweet, _gate_pass)["shadow_h_block_up_sweet_spot"]["passed"] is False
    assert shadow_rules.evaluate_shadow(gz, _gate_pass)["shadow_h_block_up_sweet_spot"]["passed"] is True
    _set_regime(monkeypatch, "flat")
    assert shadow_rules.evaluate_shadow(sweet, _gate_pass)["shadow_h_block_up_sweet_spot"]["passed"] is True


def test_block_pump_chase(monkeypatch):
    hi = dict(_tok(), price_change_1h_pct=20.0)
    ok = dict(_tok(), price_change_1h_pct=10.0)
    assert shadow_rules.evaluate_shadow(hi, _gate_pass)["shadow_i_block_pump_chase"]["passed"] is False
    assert shadow_rules.evaluate_shadow(ok, _gate_pass)["shadow_i_block_pump_chase"]["passed"] is True


def test_pullback_only_range(monkeypatch):
    inr = dict(_tok(), price_change_1h_pct=3.0)
    above = dict(_tok(), price_change_1h_pct=10.0)
    below = dict(_tok(), price_change_1h_pct=-5.0)
    assert shadow_rules.evaluate_shadow(inr, _gate_pass)["shadow_j_pullback_only"]["passed"] is True
    assert shadow_rules.evaluate_shadow(above, _gate_pass)["shadow_j_pullback_only"]["passed"] is False
    assert shadow_rules.evaluate_shadow(below, _gate_pass)["shadow_j_pullback_only"]["passed"] is False


def test_pc1h_missing_is_safe_passthrough(monkeypatch):
    # pc_1h 값이 없으면 pc 필터는 건너뜀(차단 안 함).
    tok = _tok()  # price_change_1h_pct 없음
    assert shadow_rules.evaluate_shadow(tok, _gate_pass)["shadow_i_block_pump_chase"]["passed"] is True
    assert shadow_rules.evaluate_shadow(tok, _gate_pass)["shadow_j_pullback_only"]["passed"] is True


def test_block_divergence_below(monkeypatch):
    # divergence_score = (buy_ratio-0.5) - max(0,pc_1h)/20
    R = "shadow_n_block_div_below"  # 임계 -0.5
    # 과열+매도우위: pc_1h=15, buys/sells 30/70 -> (0.3-0.5) - 15/20 = -0.95 < -0.5 -> 차단
    bad = dict(_tok(), price_change_1h_pct=15.0, buys_1h=30, sells_1h=70)
    # 평탄+매수우위: pc_1h=0, 70/30 -> +0.2 -> 통과
    good = dict(_tok(), price_change_1h_pct=0.0, buys_1h=70, sells_1h=30)
    miss = dict(_tok(), price_change_1h_pct=15.0)  # buys/sells 없음 -> 패스스루
    assert shadow_rules.evaluate_shadow(bad, _gate_pass)[R]["passed"] is False
    assert shadow_rules.evaluate_shadow(good, _gate_pass)[R]["passed"] is True
    assert shadow_rules.evaluate_shadow(miss, _gate_pass)[R]["passed"] is True


def test_base_eth24h_down(monkeypatch):
    R = "shadow_m_base_eth24h_down"
    base = dict(_tok(), chain="base")
    bsc = dict(_tok(), chain="bsc")
    # ETH 24h 하락 -> base 차단, bsc 통과(eth_chains=base)
    monkeypatch.setattr(shadow_rules, "_current_eth_24h_pct", lambda: -1.0)
    assert shadow_rules.evaluate_shadow(base, _gate_pass)[R]["passed"] is False
    assert shadow_rules.evaluate_shadow(bsc, _gate_pass)[R]["passed"] is True
    # ETH 24h 상승 -> base 통과
    monkeypatch.setattr(shadow_rules, "_current_eth_24h_pct", lambda: +1.0)
    assert shadow_rules.evaluate_shadow(base, _gate_pass)[R]["passed"] is True
    # 미가용(None) -> 차단 안 함
    monkeypatch.setattr(shadow_rules, "_current_eth_24h_pct", lambda: None)
    assert shadow_rules.evaluate_shadow(base, _gate_pass)[R]["passed"] is True


def test_hours_6_12(monkeypatch):
    from types import SimpleNamespace

    class _DT:
        @staticmethod
        def now(tz=None):
            return SimpleNamespace(hour=_DT._h)
    _DT._h = 8
    monkeypatch.setattr(shadow_rules, "datetime", _DT)
    assert shadow_rules.evaluate_shadow(_tok(), _gate_pass)["shadow_k_hours_6_12"]["passed"] is True
    _DT._h = 20
    assert shadow_rules.evaluate_shadow(_tok(), _gate_pass)["shadow_k_hours_6_12"]["passed"] is False


def test_score_below_floor(monkeypatch):
    junk = dict(_tok(), chain="base", final_score=0.10)
    golden = dict(_tok(), chain="base", final_score=0.20)
    bsc = dict(_tok(), chain="bsc", final_score=0.10)   # score_chains=base 만
    missing = dict(_tok(), chain="base")                 # final_score 없음 → 패스스루
    R = "shadow_l_score_below_floor"
    assert shadow_rules.evaluate_shadow(junk, _gate_pass)[R]["passed"] is False
    assert shadow_rules.evaluate_shadow(golden, _gate_pass)[R]["passed"] is True
    assert shadow_rules.evaluate_shadow(bsc, _gate_pass)[R]["passed"] is True
    assert shadow_rules.evaluate_shadow(missing, _gate_pass)[R]["passed"] is True


if __name__ == "__main__":
    class _MP:
        def __init__(self): self._u = []
        def setattr(self, obj, name, val):
            self._u.append((obj, name, getattr(obj, name))); setattr(obj, name, val)
        def undo(self):
            for o, n, v in reversed(self._u): setattr(o, n, v)
    for fn in (test_up_regime_blocks_e_and_f, test_flat_regime_blocks_neither,
               test_strong_up_blocks_only_f, test_gate_fail_is_not_relabeled_as_regime_block,
               test_unknown_regime_is_safe_passthrough, test_disable_sweet_spot_blocks_sweet_spot,
               test_block_up_sweet_spot_combo, test_block_pump_chase, test_pullback_only_range,
               test_pc1h_missing_is_safe_passthrough, test_hours_6_12, test_score_below_floor,
               test_base_eth24h_down, test_block_divergence_below):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("ok: shadow eth regime experiment tests passed")
