"""base 포지션 fallback 시세(GeckoTerminal) 회귀 테스트.

버그: build_market_snapshot 이 bsc/solana 만 처리하고 mode=="live" 전용이라,
base 포지션이 trending 피드에서 빠지면 재호가 경로가 전혀 없어 가격이 동결됐다
(PITCH/BNKR). 수정: base 는 지갑 불필요한 GeckoTerminal 컨트랙트 가격으로 폴백,
dry_run 포함 전 모드에서 동작.
"""
from types import SimpleNamespace

import gecko_client
from trading.executor import TradeExecutor


def _pos(chain):
    return SimpleNamespace(
        chain=chain,
        symbol="FROZEN",
        contract_address="0xAbC123",
        current_liquidity=100_000.0,
        current_b_holders=42,
        current_price=1.0,
        token_amount=10.0,
        entry_price=1.0,
        size_usd=50.0,
    )


def _executor(mode="dry_run"):
    return TradeExecutor(None, None, None, mode=mode, state_path=None)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_gecko_fetch_token_price_parses(monkeypatch):
    payload = {"data": {"attributes": {"token_prices": {"0xabc123": "2.50"}}}}
    monkeypatch.setattr(gecko_client.requests, "get", lambda *a, **k: _FakeResp(payload))
    # 주소 대소문자가 응답과 달라도 매칭돼야 한다.
    assert gecko_client.fetch_token_price("base", "0xAbC123") == 2.50


def test_gecko_fetch_token_price_missing_returns_none(monkeypatch):
    monkeypatch.setattr(gecko_client.requests, "get",
                        lambda *a, **k: _FakeResp({"data": {"attributes": {"token_prices": {}}}}))
    assert gecko_client.fetch_token_price("base", "0xAbC123") is None


def test_base_fallback_snapshot_in_dry_run(monkeypatch):
    # 핵심 회귀: dry_run 에서도 base 포지션이 재호가돼야 한다(과거엔 mode!=live → None).
    monkeypatch.setattr(gecko_client, "fetch_token_price", lambda chain, addr: 2.5)
    snap = _executor(mode="dry_run").build_market_snapshot(_pos("base"))
    assert snap is not None, "dry_run base 폴백이 None 을 반환(동결 버그 미수정)"
    assert snap["price_usd"] == 2.5
    assert snap["contract_address"] == "0xAbC123"
    assert snap["liquidity_usd"] == 100_000.0
    assert snap["_fallback_disappearance"] is True


def test_base_fallback_none_when_price_unavailable(monkeypatch):
    monkeypatch.setattr(gecko_client, "fetch_token_price", lambda chain, addr: None)
    assert _executor(mode="dry_run").build_market_snapshot(_pos("base")) is None


def test_bsc_still_none_in_dry_run():
    # bsc/solana 는 live DEX 호가 전용 → dry_run 에선 기존대로 None(거동 불변).
    assert _executor(mode="dry_run").build_market_snapshot(_pos("bsc")) is None


if __name__ == "__main__":
    # pytest 없이도 돌도록 최소 러너 (monkeypatch 흉내).
    import gecko_client as gc

    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    for fn in (test_gecko_fetch_token_price_parses, test_gecko_fetch_token_price_missing_returns_none,
               test_base_fallback_snapshot_in_dry_run, test_base_fallback_none_when_price_unavailable):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    test_bsc_still_none_in_dry_run()
    print("ok: base fallback price tests passed")
