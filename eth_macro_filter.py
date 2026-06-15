from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_BASE_DIR = Path(__file__).parent
if load_dotenv is not None:
    load_dotenv(_BASE_DIR / '.env', override=False)


@dataclass
class EthMacroSnapshot:
    current_price: Optional[float]
    price_4h_ago: Optional[float]
    change_4h_pct: Optional[float]
    fetched_at: Optional[datetime]
    source: str = 'binance'
    change_24h_pct: Optional[float] = None


# ETH 4h 변동률 레짐 밴드. 경계는 btc_correlation 분석 버킷과 동일하게 유지해야
# 진입시점 태깅과 사후 상관분석이 1:1로 맞물린다. (lo <= x < hi)
ETH_4H_REGIME_BANDS = (
    ('strong_down', float('-inf'), -2.0),
    ('down',        -2.0,          -0.5),
    ('flat',        -0.5,           0.5),
    ('up',           0.5,           2.0),
    ('strong_up',    2.0,  float('inf')),
)


def classify_eth_4h_regime(change_4h_pct: Optional[float]) -> str:
    """ETH 4h 변동률을 레짐 밴드명으로 분류. None이면 'unknown'."""
    if change_4h_pct is None:
        return 'unknown'
    for name, lo, hi in ETH_4H_REGIME_BANDS:
        if lo <= change_4h_pct < hi:
            return name
    return 'unknown'


class EthMacroFilter:
    def __init__(self) -> None:
        self._snapshot: Optional[EthMacroSnapshot] = None
        self._fetched_monotonic: float = 0.0
        self._cache_ttl_seconds = 300
        self._stale_ok_seconds = 1800

    @property
    def enabled(self) -> bool:
        return str(os.getenv('ETH_4H_FILTER_ENABLED', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}

    @property
    def threshold_pct(self) -> float:
        try:
            return float(os.getenv('ETH_4H_FILTER_THRESHOLD', '-1.5'))
        except ValueError:
            return -1.5

    def _fetch_snapshot(self) -> EthMacroSnapshot:
        # 25개(=24h+여유) 받아 4h/24h 둘 다 계산. 4h 게이트는 기존과 동일(closes[-5]).
        url = 'https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1h&limit=25'
        with urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        if not isinstance(payload, list) or len(payload) < 5:
            raise RuntimeError('unexpected_binance_payload')
        closes = [float(row[4]) for row in payload]
        current_price = closes[-1]
        price_4h_ago = closes[-5]
        change_4h_pct = ((current_price - price_4h_ago) / price_4h_ago * 100.0) if price_4h_ago > 0 else None
        change_24h_pct = None
        if len(closes) >= 25:
            price_24h_ago = closes[-25]
            if price_24h_ago > 0:
                change_24h_pct = (current_price - price_24h_ago) / price_24h_ago * 100.0
        return EthMacroSnapshot(
            current_price=current_price,
            price_4h_ago=price_4h_ago,
            change_4h_pct=change_4h_pct,
            fetched_at=datetime.now(timezone.utc),
            change_24h_pct=change_24h_pct,
        )

    def get_snapshot(self) -> EthMacroSnapshot:
        now_mono = time.monotonic()
        if self._snapshot and (now_mono - self._fetched_monotonic) < self._cache_ttl_seconds:
            return self._snapshot
        try:
            snap = self._fetch_snapshot()
            self._snapshot = snap
            self._fetched_monotonic = now_mono
            return snap
        except (URLError, HTTPError, TimeoutError, RuntimeError, ValueError, OSError):
            if self._snapshot and (now_mono - self._fetched_monotonic) < self._stale_ok_seconds:
                return self._snapshot
            return EthMacroSnapshot(None, None, None, None, source='unavailable')

    def current_regime(self) -> tuple[Optional[float], str]:
        """진입시점 ETH 4h 변동률과 레짐 밴드명. 게이트가 이미 받아둔 캐시
        스냅샷을 재사용하므로 추가 네트워크 호출이 없다(미가용 시 (None,'unknown'))."""
        snap = self.get_snapshot()
        return snap.change_4h_pct, classify_eth_4h_regime(snap.change_4h_pct)

    def current_eth_24h_pct(self) -> Optional[float]:
        """진입시점 ETH 24h 변동률(캐시 스냅샷 재사용). 미가용 시 None.
        base 가 ETH 하락에 약하다는 관측(ETH24h<=0 시 base 평균 -1.8% vs 상승 -0.5%)
        검증용 게이트(shadow)에서 사용."""
        return self.get_snapshot().change_24h_pct

    def should_block_entry(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, 'eth_4h_filter_disabled'
        snap = self.get_snapshot()
        if snap.change_4h_pct is None:
            return False, 'eth_4h_unavailable_fail_open'
        if snap.change_4h_pct < self.threshold_pct:
            return True, f'eth_4h_down ({snap.change_4h_pct:+.2f}% < {self.threshold_pct:+.2f}%)'
        return False, f'eth_4h_ok ({snap.change_4h_pct:+.2f}%)'


_FILTER: Optional[EthMacroFilter] = None


def get_eth_macro_filter() -> EthMacroFilter:
    global _FILTER
    if _FILTER is None:
        _FILTER = EthMacroFilter()
    return _FILTER


if __name__ == '__main__':
    eth_filter = get_eth_macro_filter()
    snap = eth_filter.get_snapshot()
    blocked, reason = eth_filter.should_block_entry()
    change_text = 'N/A' if snap.change_4h_pct is None else f'{snap.change_4h_pct:+.2f}%'
    current_text = 'N/A' if snap.current_price is None else f'${snap.current_price:,.2f}'
    ago_text = 'N/A' if snap.price_4h_ago is None else f'${snap.price_4h_ago:,.2f}'
    print(f'ETH 4h change: {change_text}')
    print(f'  current: {current_text}')
    print(f'  4h ago:  {ago_text}')
    print(f'Block entry: {blocked} ({reason})')
