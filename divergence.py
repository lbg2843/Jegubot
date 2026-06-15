# -*- coding: utf-8 -*-
"""divergence.py - Module 2(재귀성) 신호 v1.

가설(divergence_probe 로 n=42k 검증): 인식(가격 모멘텀)이 실수요(매수압)를 앞서면
약세 괴리(forward 하락). 핵심 발견:
  - pc_1h 가 높을수록(이미 오를수록) forward 나쁨 — 견고(1h/4h 일관). 지배적 효과.
  - buy_ratio(매수 우위)는 약하지만 같은 방향(특히 1h). 보조.
  - forward 최선 구간: 가격 평탄/약하락(pc_1h<=~5) + 매수우위.

v1 score: 높을수록 '실수요가 가격을 받쳐줌 = 강세'. 낮을수록 '인식이 앞섬 = 약세'.
  buy_pressure = buy_ratio - 0.5            # -0.5..+0.5
  extension    = max(0, pc_1h)              # 위로 오른 만큼만 '인식 선행'
  score        = buy_pressure - extension/20  # extension 이 지배(20%에서 -1.0)

주의: buys/sells 가 채워진(935283e 이후) 데이터에서만 유효. None 이면 'unknown'.
이건 진입 결정에 안 쓰고 태깅만 — forward 검증 후 게이트화 결정.
"""
from __future__ import annotations

from typing import Optional


def buy_ratio(buys_1h, sells_1h) -> Optional[float]:
    """매수 비율 buys/(buys+sells). 데이터 없으면 None."""
    if not isinstance(buys_1h, (int, float)) or not isinstance(sells_1h, (int, float)):
        return None
    total = float(buys_1h) + float(sells_1h)
    if total <= 0:
        return None
    return float(buys_1h) / total


def divergence_score(pc_1h, buys_1h, sells_1h) -> Optional[float]:
    """높을수록 강세(실수요가 받침), 낮을수록 약세(인식 선행). 데이터 없으면 None."""
    br = buy_ratio(buys_1h, sells_1h)
    if br is None:
        return None
    pc = float(pc_1h) if isinstance(pc_1h, (int, float)) else 0.0
    buy_pressure = br - 0.5
    extension = max(0.0, pc)
    return round(buy_pressure - extension / 20.0, 4)


def divergence_label(pc_1h, buys_1h, sells_1h) -> str:
    """probe 사분면과 같은 축의 라벨: <ext>_<demand>. 예: extended_sell, flat_buy.
    forward 검증에서 그룹핑하기 쉽게. 데이터 없으면 'unknown'."""
    br = buy_ratio(buys_1h, sells_1h)
    if br is None:
        return "unknown"
    pc = float(pc_1h) if isinstance(pc_1h, (int, float)) else 0.0
    ext = "extended" if pc > 15 else ("mid" if pc > 5 else "flat")
    dem = "buy" if br >= 0.55 else ("sell" if br < 0.45 else "neutral")
    return f"{ext}_{dem}"


def compute(cand: dict) -> dict:
    """candidate dict 에서 divergence 태깅 묶음 반환(진입 로깅용)."""
    pc = cand.get("price_change_1h_pct")
    if pc is None:
        pc = cand.get("price_change_1h")
    b = cand.get("buys_1h")
    s = cand.get("sells_1h")
    return {
        "divergence_score": divergence_score(pc, b, s),
        "divergence_label": divergence_label(pc, b, s),
        "buy_ratio": buy_ratio(b, s),
    }
