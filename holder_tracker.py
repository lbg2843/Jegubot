# -*- coding: utf-8 -*-
"""holder_tracker.py — 오픈 포지션의 홀더 수를 시계열로 기록(홀더 증가율 분석용, C).

가설(orchestrator 주석): "시간차 홀더 증가율 = 진짜 reflexivity divergence".
스냅샷(GeckoTerminal)은 holders 를 안 줘서(=0) Web3 enrich 가 유일한 소스인데,
지금은 진입 시점 1회만 찍는다. 보유 중 홀더가 늘면 실수요가 따라붙는 것(강세),
빠지면 이탈(약세) — 이걸 보려면 보유 기간 동안 주기적으로 다시 찍어야 한다.

라이브 거래엔 0 영향. 토큰당 interval_min 마다만 호출(API 과호출 방지).
HOLDER_TRACK_ENABLED=0 으로 끔. 분석은 나중에 data/holder_timeseries.jsonl 로.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from trading.time_utils import iso_utc_now, parse_iso_utc, utc_now

log = logging.getLogger("holder_tracker")
OUT = Path(__file__).resolve().parent / "data" / "holder_timeseries.jsonl"


class HolderTracker:
    def __init__(self, interval_min: int | None = None, out_path: str | Path = OUT):
        self.interval_min = interval_min if interval_min is not None else int(
            os.getenv("HOLDER_TRACK_INTERVAL_MIN", "20") or 20)
        self.out = Path(out_path)
        self._last: dict[str, float] = {}  # contract -> 마지막 기록 epoch(분)

    def track(self, positions) -> int:
        """오픈 포지션들의 현재 홀더를 (토큰당 interval 마다) 기록. 기록 건수 반환."""
        try:
            from web3_client import fetch_token_enrich
        except Exception:
            return 0
        now = utc_now()
        now_min = now.timestamp() / 60.0
        wrote = 0
        for pos in list(positions):
            if getattr(pos, "is_closed", False):
                continue
            contract = str(getattr(pos, "contract_address", "") or "").lower()
            chain = str(getattr(pos, "chain", "") or "").lower()
            if not contract or not chain:
                continue
            last = self._last.get(contract)
            if last is not None and (now_min - last) < self.interval_min:
                continue
            try:
                enr = fetch_token_enrich(chain, contract)
            except Exception as e:
                log.debug("holder fetch fail %s: %s", contract[:10], e)
                continue
            if not enr or enr.get("holders") is None:
                self._last[contract] = now_min  # 실패도 쿨다운(재시도 폭주 방지)
                continue
            hold_hours = 0.0
            try:
                et = getattr(pos, "entry_timestamp", None)
                if et:
                    hold_hours = (now - parse_iso_utc(et)).total_seconds() / 3600
            except Exception:
                pass
            rec = {
                "ts": iso_utc_now(), "chain": chain, "contract": contract,
                "symbol": getattr(pos, "symbol", "?"),
                "holders": enr.get("holders"),
                "holders_top10_pct": enr.get("holders_top10_pct"),
                "risk_level": enr.get("risk_level"),
                "hold_hours": round(hold_hours, 2),
                "entry_timestamp": getattr(pos, "entry_timestamp", None),
            }
            with self.out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._last[contract] = now_min
            wrote += 1
        return wrote
