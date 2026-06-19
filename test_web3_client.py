"""web3_client 토큰 enrich 테스트 (네트워크 목)."""
import web3_client


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _patch(monkeypatch, payload):
    monkeypatch.setattr(web3_client.requests, "get", lambda *a, **k: _Resp(payload))


def test_enrich_contract_match(monkeypatch):
    _patch(monkeypatch, {"data": [
        {"contractAddress": "0xOTHER", "holders": "1"},
        {"contractAddress": "0xAbC", "holders": "4958", "holdersTop10Percent": "69.1", "riskLevel": 1},
    ]})
    out = web3_client.fetch_token_enrich("bsc", "0xabc")
    assert out == {"holders": "4958", "holders_top10_pct": "69.1", "risk_level": 1}


def test_no_contract_match(monkeypatch):
    _patch(monkeypatch, {"data": [{"contractAddress": "0xother", "holders": "1"}]})
    assert web3_client.fetch_token_enrich("bsc", "0xabc") == {}


def test_bad_chain_or_empty():
    assert web3_client.fetch_token_enrich("ethereum", "0xabc") == {}   # CHAIN_ID 없음
    assert web3_client.fetch_token_enrich("bsc", "") == {}


def test_fetch_holders(monkeypatch):
    _patch(monkeypatch, {"data": [{"contractAddress": "0xabc", "holders": "4958"}]})
    assert web3_client.fetch_holders("bsc", "0xabc") == 4958.0
    _patch(monkeypatch, {"data": []})
    assert web3_client.fetch_holders("bsc", "0xabc") is None


if __name__ == "__main__":
    class _MP:
        def __init__(self): self._u = []
        def setattr(self, o, n, v): self._u.append((o, n, getattr(o, n))); setattr(o, n, v)
        def undo(self):
            for o, n, v in reversed(self._u): setattr(o, n, v)
    for fn in (test_enrich_contract_match, test_no_contract_match, test_fetch_holders):
        mp = _MP()
        try: fn(mp)
        finally: mp.undo()
    test_bad_chain_or_empty()
    print("ok: web3_client tests passed")
