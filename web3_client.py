# -*- coding: utf-8 -*-
"""web3_client.py - Binance Web3 API 토큰 enrich (holders/집중도/risk).

스크래퍼(GeckoTerminal)가 못 주던 holders 를 Web3 공개 API(무인증)로 보충.
divergence(Module 2)의 빠진 조각 = 홀더. 진입마다 enrich·태깅 → 시간차로 홀더
증가율 계산 가능(= 가격 vs 실수요 괴리, 진짜 reflexivity).

엔드포인트(탐침 확인): contract 정확매칭으로 holders/holdersTop10Percent/riskLevel.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("web3_client")

# enrich 캐시: 게이트가 후보·shadow arm 마다 호출해도 TTL 안엔 API 1회.
# holder_tracker 는 20분 간격 폴이라 TTL(기본 300s)보다 길어 항상 신선값.
_ENRICH_CACHE: dict[tuple, tuple] = {}
_CACHE_TTL = float(os.getenv("WEB3_ENRICH_CACHE_TTL_SEC", "300") or 300)

W3_SEARCH = "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search"
W3_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://web3.binance.com",
    "Referer": "https://web3.binance.com/",
}
CHAIN_ID = {"bsc": "56", "bnb": "56", "base": "8453", "solana": "CT_501"}


def _num(v, as_int: bool = False):
    """API 가 문자열로 주는 숫자('3730', '72.24')를 float/int 로. 실패 시 None."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if as_int else f


def fetch_token_enrich(chain: str, contract: str, timeout: int = 10) -> dict:
    """Web3 API 로 holders/집중도/risk 조회(TTL 캐시). 실패/없음 시 {}.
    반환: {holders, holders_top10_pct, risk_level} (있는 키만, 숫자형)."""
    key = (str(chain).lower(), str(contract).lower())
    now = time.monotonic()
    hit = _ENRICH_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    res = _fetch_token_enrich_uncached(chain, contract, timeout)
    if len(_ENRICH_CACHE) > 2000:  # 무한증식 방지: 만료분 정리
        for k in [k for k, v in _ENRICH_CACHE.items() if (now - v[0]) >= _CACHE_TTL]:
            _ENRICH_CACHE.pop(k, None)
    _ENRICH_CACHE[key] = (now, res)
    return res


def _fetch_token_enrich_uncached(chain: str, contract: str, timeout: int = 10) -> dict:
    cid = CHAIN_ID.get(str(chain).lower())
    if not cid or not contract:
        return {}
    try:
        r = requests.get(W3_SEARCH, params={"keyword": contract, "chainIds": cid},
                         headers=W3_HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data")
        rows = data if isinstance(data, list) else (
            (data.get("list") or data.get("rows") or []) if isinstance(data, dict) else [])
        tgt = contract.lower()
        for t in rows:
            if str(t.get("contractAddress", "")).lower() == tgt:
                out = {}
                # API 가 holders/holdersTop10Percent 를 문자열로 줘서 숫자 분석이 죄다
                # 누락됐었음(2026-06-23 발견). 여기서 숫자로 강제 캐스팅해 저장.
                if t.get("holders") is not None:
                    out["holders"] = _num(t.get("holders"), as_int=True)
                if t.get("holdersTop10Percent") is not None:
                    out["holders_top10_pct"] = _num(t.get("holdersTop10Percent"))
                if t.get("riskLevel") is not None:
                    out["risk_level"] = _num(t.get("riskLevel"), as_int=True)
                return {k: v for k, v in out.items() if v is not None}
    except Exception as exc:  # noqa: BLE001
        log.debug("web3 enrich 실패 %s: %s", str(contract)[:12], exc)
    return {}


def fetch_holders(chain: str, contract: str) -> Optional[float]:
    """홀더 수만 (시간차 폴링으로 증가율 계산용)."""
    h = fetch_token_enrich(chain, contract).get("holders")
    try:
        return float(h) if h is not None else None
    except Exception:
        return None
