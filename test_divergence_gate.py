# -*- coding: utf-8 -*-
"""divergence 라이브 게이트(2026-06-21 승격) 차단 동작 검증.

arm o(div<-0.25)를 passes_safety_gate 에 하드 박은 뒤:
  - 과열(div<floor) 진입은 차단,
  - 강세/평탄(div>=floor) 진입은 통과,
  - buys/sells 결측(BSC 등) → score None → 통과(현 동작 보존),
  - DIVERGENCE_BLOCK_BELOW="" → 차단 비활성(revert) 임을 확인.
"""
import os

import orchestrator


class _NoBlockEthFilter:
    def should_block_entry(self):
        return False, 'ok'


def _base_token(**over):
    # divergence 외 게이트 항목은 전부 통과하도록 구성한 base 후보.
    t = {
        'symbol': 'TEST',
        'entry_path': 'reflexivity',
        'liquidity_usd': 300_000,
        'market_cap_usd': 1_000_000,   # liq/mcap = 0.30 (>0.03)
        'txns_1h': 50,
        'price_change_1h_pct': 10.0,
        'buys_1h': 40, 'sells_1h': 60,  # buy_ratio 0.4 -> div = -0.1 - 10/20 = -0.60
    }
    t.update(over)
    return t


def _gate(monkeypatch, token, env='-0.25'):
    monkeypatch.setattr(orchestrator, 'get_eth_macro_filter', lambda: _NoBlockEthFilter())
    if env is None:
        monkeypatch.delenv('DIVERGENCE_BLOCK_BELOW', raising=False)
    else:
        monkeypatch.setenv('DIVERGENCE_BLOCK_BELOW', env)
    return orchestrator.passes_safety_gate(token, 'base')


def test_blocks_overheated(monkeypatch):
    ok, reason = _gate(monkeypatch, _base_token())  # div -0.60 < -0.25
    assert not ok and reason.startswith('divergence_below_')


def test_allows_bullish(monkeypatch):
    # buy 우위 + 평탄 → div = +0.10 - 0 = +0.10 (>= -0.25) → 통과
    ok, reason = _gate(monkeypatch, _base_token(buys_1h=60, sells_1h=40, price_change_1h_pct=0.0))
    assert ok and reason == 'ok'


def test_passthrough_when_buysell_missing(monkeypatch):
    # BSC 류: buys/sells 결측 → score None → 차단 안 함
    ok, reason = _gate(monkeypatch, _base_token(buys_1h=0, sells_1h=0))
    assert ok and reason == 'ok'


def test_revert_disables_block(monkeypatch):
    # DIVERGENCE_BLOCK_BELOW="" → float("") 실패 → 차단 비활성 → 과열도 통과
    ok, reason = _gate(monkeypatch, _base_token(), env='')
    assert ok and reason == 'ok'


def test_default_threshold_is_minus_025(monkeypatch):
    # env 미설정이어도 기본 -0.25 적용되어 과열 차단
    ok, reason = _gate(monkeypatch, _base_token(), env=None)
    assert not ok and reason.startswith('divergence_below_')
