# -*- coding: utf-8 -*-
"""surf_client.py — Surf onchain SQL 로 토큰 '붐빔도'(매수자 증가율) 측정.

가설(2026-06-29 프로브 n24): 패자 = 신생인데 매수자 폭발(빠른 펌프 추격),
승자 = 오래됐는데 매수자 적음(잠수함). buyers_per_day(붐비는 속도)가 vol_1h/pool_age
프록시보다 선명하게 갈랐다(패자 중앙 ~18/일 vs 승자 ~4/일). 메모 edge-is-entry-not-exit
"안 쫓기" 의 정밀판. 진입 시점에 측정만 → shadow 로 포워드 검증(하드 게이트 X).

비용/안전: onchain/sql = 쿼리당 ~5크레딧(~$0.02). 진입한 토큰만 호출 + TTL 캐시 +
일일 호출 캡(SURF_DAILY_CALL_CAP). 실패/캡/잔액0 → {} 반환(진입 로깅만 건너뜀, 거래 무영향).
SURF_FEATURE_ENABLED=0 으로 끔.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("surf")

BASE = "https://api.asksurf.ai/gateway"
_TABLE = {"base": "agent.base_dex_trades", "bsc": "agent.bsc_dex_trades"}
_TTL = float(os.getenv("SURF_CACHE_TTL_SEC", "3600") or 3600)
_DAILY_CAP = int(os.getenv("SURF_DAILY_CALL_CAP", "60") or 60)

_KEY: str | None = None
_CACHE: dict[tuple, tuple] = {}     # (chain,contract) -> (epoch, result)
_CALLS: list[datetime] = []         # 호출 시각(일일캡용)


def _key() -> str | None:
    global _KEY
    if _KEY is not None:
        return _KEY or None
    k = os.getenv("SURF_API_KEY", "")
    if not k:  # dotenv 미로드 환경 대비 .env 직접 파싱
        envf = Path(__file__).resolve().parent / ".env"
        if envf.exists():
            for line in envf.open(encoding="utf-8"):
                if line.strip().startswith("SURF_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    _KEY = k or ""
    return _KEY or None


def _under_daily_cap() -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    _CALLS[:] = [t for t in _CALLS if t > cutoff]
    return len(_CALLS) < _DAILY_CAP


def fetch_token_crowding(chain: str, contract: str, entry_dt: datetime | None = None) -> dict:
    """진입 시점까지의 누적 매수자/일평균 매수자/토큰나이. 측정 전용. 실패 시 {}."""
    chain = str(chain).lower()
    table = _TABLE.get(chain)
    contract = str(contract or "").lower()
    if not table or not contract:
        return {}
    if os.getenv("SURF_FEATURE_ENABLED", "1") != "1":
        return {}
    key = _key()
    if not key:
        return {}

    now = datetime.now(timezone.utc)
    ck = (chain, contract)
    hit = _CACHE.get(ck)
    if hit is not None and (now.timestamp() - hit[0]) < _TTL:
        return hit[1]
    if not _under_daily_cap():
        log.debug("surf daily cap reached (%d)", _DAILY_CAP)
        return {}

    ed = entry_dt or now
    e = ed.strftime("%Y-%m-%d %H:%M:%S")
    lookback = (ed - timedelta(days=240)).strftime("%Y-%m-%d")
    sql = (
        f"SELECT min(block_time) AS first_buy, countDistinct(taker) AS buyers_total "
        f"FROM {table} WHERE token_bought_address='{contract}' "
        f"AND block_date>='{lookback}' AND block_time<=toDateTime('{e}')"
    )
    try:
        _CALLS.append(now)
        r = requests.post(
            BASE + "/v1/onchain/sql",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                     "Content-Type": "application/json"},
            json={"sql": sql}, timeout=60,
        )
        if r.status_code != 200:
            log.debug("surf sql %s: %s", r.status_code, r.text[:120])
            return {}
        rows = r.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        fb = row.get("first_buy")
        bt = row.get("buyers_total")
        out: dict = {}
        if isinstance(bt, (int, float)):
            out["surf_buyers_total"] = int(bt)
        if fb and isinstance(bt, (int, float)) and bt > 0:
            age_days = max((ed.timestamp() - float(fb)) / 86400.0, 0.01)
            out["surf_token_age_days"] = round(age_days, 2)
            out["surf_buyers_per_day"] = round(bt / age_days, 2)
        _CACHE[ck] = (now.timestamp(), out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("surf fetch fail %s: %s", contract[:10], exc)
        return {}


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "0x4b5d32a07b8d3ec5d6928caa30196f8dd6a7c5a9"
    print(fetch_token_crowding("base", c))
