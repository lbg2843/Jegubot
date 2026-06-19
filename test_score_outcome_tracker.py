"""score_outcome_tracker: append-only 유입 + 만료 prune 회귀 테스트.

핵심 버그 수정 검증:
 1) add_pending 은 전체 재기록이 아니라 append (O(1)).
 2) process_pending 은 24h+유예 지난 행을 조용히 prune (큐 무한증식 차단).
 3) 정상 만기 행은 현재가로 outcome 해소.
"""
import datetime as dt
import json

import score_outcome_tracker as sot

NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _iso(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


def _setup(tmp):
    sot.PENDING_PATH = tmp / "pending.jsonl"
    sot.OUTCOMES_PATH = tmp / "outcomes.jsonl"


def _rec(ts, token="0xabc", chain="bsc", price=1.0):
    return {"timestamp": ts, "chain": chain, "symbol": "T",
            "token_address": token, "entry_price": price,
            "final_score": 0.3, "reflexivity_passed": True}


def _lines(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_add_pending_is_append(tmp):
    _setup(tmp)
    sot.add_pending(_rec(_iso(0), token="0xa"))
    sot.add_pending(_rec(_iso(0), token="0xb"))
    rows = [json.loads(l) for l in _lines(sot.PENDING_PATH)]
    assert len(rows) == 2
    assert {r["token_address"] for r in rows} == {"0xa", "0xb"}
    assert rows[0]["checks_remaining"] == ["1h", "4h", "24h"]


def test_process_prunes_expired(tmp, monkeypatch):
    _setup(tmp)
    monkeypatch.setattr(sot, "utc_now", lambda: NOW)
    sot.add_pending(_rec(_iso(30), token="0xold"))   # 30h 전 → 만료
    sot.process_pending({})                          # 스냅샷 없음
    assert _lines(sot.PENDING_PATH) == []            # 큐 비워짐
    assert not sot.OUTCOMES_PATH.exists() or _lines(sot.OUTCOMES_PATH) == []  # 만료는 outcome 안 씀


def test_process_resolves_due_keeps_24h(tmp, monkeypatch):
    _setup(tmp)
    monkeypatch.setattr(sot, "utc_now", lambda: NOW)
    sot.add_pending(_rec(_iso(5), token="0xz", price=1.0))   # 5h 전 → 1h·4h 만기, 24h 미만
    snaps = {"bsc": [{"contract_address": "0xz", "price_usd": 1.5}]}
    sot.process_pending(snaps)
    outs = [json.loads(l) for l in _lines(sot.OUTCOMES_PATH)]
    horizons = sorted(o["horizon"] for o in outs)
    assert horizons == ["1h", "4h"]                  # 만기 2개 해소
    assert all(abs(o["price_change_pct"] - 50.0) < 1e-6 for o in outs)
    rem = [json.loads(l) for l in _lines(sot.PENDING_PATH)]
    assert len(rem) == 1 and rem[0]["checks_remaining"] == ["24h"]  # 24h 만 남음


def test_fresh_row_untouched(tmp, monkeypatch):
    _setup(tmp)
    monkeypatch.setattr(sot, "utc_now", lambda: NOW)
    sot.add_pending(_rec(_iso(0.5), token="0xf"))    # 30분 전 → 아무것도 만기 아님
    sot.process_pending({})
    rem = [json.loads(l) for l in _lines(sot.PENDING_PATH)]
    assert len(rem) == 1 and rem[0]["checks_remaining"] == ["1h", "4h", "24h"]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    class _MP:
        def __init__(self): self._u = []
        def setattr(self, o, n, v): self._u.append((o, n, getattr(o, n))); setattr(o, n, v)
        def undo(self):
            for o, n, v in reversed(self._u): setattr(o, n, v)

    _orig = (sot.PENDING_PATH, sot.OUTCOMES_PATH)
    for fn in (test_add_pending_is_append, test_process_prunes_expired,
               test_process_resolves_due_keeps_24h, test_fresh_row_untouched):
        with tempfile.TemporaryDirectory() as d:
            mp = _MP()
            try:
                import inspect
                if "monkeypatch" in inspect.signature(fn).parameters:
                    fn(Path(d), mp)
                else:
                    fn(Path(d))
            finally:
                mp.undo()
    sot.PENDING_PATH, sot.OUTCOMES_PATH = _orig
    print("ok: score_outcome_tracker tests passed")
