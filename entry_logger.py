"""
entry_logger.py — 진입 스냅샷 별도 기록 (분석 전용, 진입 결정에 영향 0)

목적:
  진입 확정 시점의 cand(토큰 스냅샷)를 별도 jsonl 에 append.
  진입 로직/사이징/기존 저장 코드는 한 글자도 안 건드림.
  로깅 실패가 절대 진입을 막지 못하도록 try/except 로 격리.

월요일 분석용 설계:
  - 자주 볼 필드는 평탄화해서 꺼내 박음 (jsonl 열면 바로 눈에 들어오게)
  - 원본 cand 는 통째로 보관 (보험: 빠뜨린 필드도 사후 재계산 가능)
  - 조인키 contract_address + logged_at 로 closed_positions 와 1:1 연결

주의:
  - 첫 진입 1~2건 쌓이면 jsonl 한 줄 까서 cand 실제 키 이름 확인 후
    아래 _g(...) fallback 키를 다듬을 것. 안 하면 pc_5m 등이 None 일 수 있음.
    (원본 cand 를 통째 박으므로 키가 틀려도 데이터 손실은 0)

사용:
  from entry_logger import log_entry_snapshot
  log_entry_snapshot(cand, chain, mode="live")   # 또는 mode="dry"
"""
from __future__ import annotations

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("entry_log")

# 기록 위치: data/ 폴더 아래. score_shadow 등과 같은 자리.
ENTRY_LOG = Path("data/entry_snapshots.jsonl")


def _g(cand: dict, *keys):
    """여러 후보 키 중 처음 존재하는(None 아닌) 값을 반환.

    cand 의 실제 키 이름이 불확실하므로 후보를 여러 개 던져서 잡는다.
    첫 진입 1건 쌓이면 원본 까서 진짜 키 이름으로 정리할 것.
    """
    for k in keys:
        v = cand.get(k)
        if v is not None:
            return v
    return None


def log_entry_snapshot(cand: dict, chain: str, mode: str, extra: dict | None = None):
    """진입 확정 시점 스냅샷 1건을 jsonl 에 append.

    Args:
        cand:  진입 후보 dict (orchestrator 의 진입 스냅샷 그 자체)
        chain: "bsc" / "base" / "solana" 등
        mode:  "live" (실거래 진입 성공) / "dry" (드라이런 프리뷰)
        extra: 추가로 박고 싶은 값 (예: 매크로 필터가 이미 계산한 BTC/ETH 변동)
    """
    try:
        now = datetime.now(timezone.utc)
        rec = {
            # --- 조인키 / 메타 ---
            "logged_at": now.isoformat(),
            "chain": chain,
            "mode": mode,                              # "live" / "dry"
            "contract_address": _g(cand, "contract_address", "token_address", "address"),
            "symbol": _g(cand, "symbol"),
            "entry_path": _g(cand, "entry_path") or "reflexivity",

            # --- 타이밍 (이르냐/늦냐: 다중 타임프레임 같이 봐야 의미 있음) ---
            "price_usd": _g(cand, "price_usd"),
            "pc_5m":  _g(cand, "price_change_5m_pct",  "price_change_5m"),
            "pc_15m": _g(cand, "price_change_15m_pct", "price_change_15m"),
            "pc_1h":  _g(cand, "price_change_1h_pct",  "price_change_1h"),
            "pc_6h":  _g(cand, "price_change_6h_pct",  "price_change_6h"),
            "pc_24h": _g(cand, "price_change_24h_pct", "price_change_24h"),

            # --- 유동성 / 사이즈 ---
            "liquidity_usd":  _g(cand, "liquidity_usd"),
            "mcap_usd":       _g(cand, "mcap_usd", "market_cap_usd", "fdv_usd"),
            "pool_age_hours": _g(cand, "pool_age_hours", "pool_age_h"),

            # --- 거래 건강도 (덤프 직전 잡았는지) ---
            "txns_1h":    _g(cand, "txns_1h"),
            "buys_1h":    _g(cand, "buys_1h"),
            "sells_1h":   _g(cand, "sells_1h"),
            "vol_1h_usd": _g(cand, "volume_1h_usd", "volume_1h", "vol_1h"),

            # --- 점수 / 게이트 ---
            "final_score":        _g(cand, "final_score", "score"),
            "reflexivity_passed": _g(cand, "reflexivity_passed"),

            # --- 매크로 / ENV (진입시점 조건: -12 효과 갈라보기용) ---
            "stop_loss_pct": os.getenv(f"{chain.upper()}_STOP_LOSS_PCT"),
            "utc_hour": now.hour,

            # --- 보험: 원본 통째 (빠뜨린 필드 사후 재계산) ---
            "cand": cand,
        }
        if extra:
            rec.update(extra)

        ENTRY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ENTRY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        # 기록 실패가 절대 진입을 막지 않도록 삼키고 경고만.
        log.warning(f"[entry_log] 기록 실패(무시): {e}")
