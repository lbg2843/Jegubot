"""
GeckoTerminal API 공통 클라이언트
===================================
3개 체인(BSC, Base, Solana)에서 동일한 trending_pools 엔드포인트 사용.
API 키 불필요. Rate limit: 30 req/min.

공식 문서: https://api.geckoterminal.com/docs/index.html
"""

import requests
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("gecko_client")

API_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "Mozilla/5.0 (compatible; reflexivity-bot/1.0)",
}

# GeckoTerminal network slug 매핑
CHAIN_TO_NETWORK = {
    "bsc": "bsc",
    "base": "base",
    "solana": "solana",
}


@dataclass
class TrendingToken:
    """멀티체인 공통 토큰 스냅샷."""
    timestamp: str
    chain: str
    symbol: str
    name: str
    contract_address: str

    price_usd: float
    price_change_5m_pct: float
    price_change_15m_pct: float
    price_change_1h_pct: float
    price_change_24h_pct: Optional[float]
    market_cap_usd: Optional[float]

    liquidity_usd: float       # reserve_in_usd
    volume_1h_usd: float
    volume_24h_usd: float
    txns_1h: int               # h1 buys + sells

    holders_total: int         # 별도 API 필요, 기본 0
    holders_binance: int       # Binance 전용, 항상 0

    audit_flags: list
    lp_locked: Optional[bool]

    # DEX/풀 정보 (GeckoTerminal 추가 필드)
    pool_age_hours: Optional[float] = None
    pool_address: str = ""
    dex_id: str = ""

    # h1 매수/매도 건수 (txns_1h = buys_1h + sells_1h). buy_pressure 점수 입력.
    buys_1h: int = 0
    sells_1h: int = 0

    @property
    def avg_tx_size_usd(self) -> float:
        return self.volume_1h_usd / max(self.txns_1h, 1)

    @property
    def liq_to_mcap_ratio(self) -> Optional[float]:
        if not self.market_cap_usd or self.market_cap_usd == 0:
            return None
        return self.liquidity_usd / self.market_cap_usd

    @property
    def binance_holder_ratio(self) -> float:
        return self.holders_binance / max(self.holders_total, 1)


def fetch_token_price(chain: str, contract_address: str) -> Optional[float]:
    """단일 토큰의 현재 USD 가격을 컨트랙트 주소로 조회.

    trending_pools 피드에서 사라진 보유 포지션을 재호가하기 위한 폴백 소스.
    지갑/체결 없이 동작하므로 dry_run·live 모두에서 쓸 수 있다.
    (simple/token_price 엔드포인트, 키 불필요, 최대 30개 주소까지 지원하나 여기선 1개)

    Returns:
        가격(float) 또는 조회 실패 시 None.
    """
    network = CHAIN_TO_NETWORK.get(chain)
    if not network or not contract_address:
        return None
    url = f"{API_BASE}/simple/networks/{network}/token_price/{contract_address}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        prices = (
            resp.json()
            .get("data", {})
            .get("attributes", {})
            .get("token_prices", {})
        )
        # 주소 키는 대소문자가 응답마다 다를 수 있어 대소문자 무시 매칭.
        target = contract_address.lower()
        for addr, price in prices.items():
            if addr.lower() == target:
                value = float(price) if price not in (None, "") else 0.0
                return value if value > 0 else None
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning(f"fetch_token_price 실패 [{chain}] {contract_address[:12]}: {exc}")
    return None


def fetch_trending_pools(chain: str, page: int = 1, limit: int = 20) -> list[TrendingToken]:
    """
    GeckoTerminal trending_pools 엔드포인트 호출.

    Args:
        chain: "bsc", "base", "solana"
        page: 1~10 (페이지당 20개)
        limit: 최대 반환 수

    Returns:
        TrendingToken 리스트 (정규화 완료)
    """
    network = CHAIN_TO_NETWORK.get(chain)
    if not network:
        log.error(f"지원하지 않는 체인: {chain}")
        return []

    url = f"{API_BASE}/networks/{network}/trending_pools"
    params = {"include": "base_token", "page": page}

    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning(f"[{chain}] GeckoTerminal 요청 실패: {e}")
        return []

    data = r.json()
    pools = data.get("data", [])
    included = data.get("included", [])

    if not pools:
        log.warning(f"[{chain}] 응답에 풀 데이터 없음")
        return []

    # included 토큰 조회용 딕셔너리 (id → attributes)
    token_map: dict[str, dict] = {
        item["id"]: item["attributes"]
        for item in included
        if item.get("type") == "token"
    }

    timestamp = datetime.utcnow().isoformat()
    results = []

    for pool in pools[:limit]:
        try:
            token = _normalize(pool, token_map, chain, timestamp)
            results.append(token)
        except Exception as e:
            pool_id = pool.get("id", "?")
            log.debug(f"[{chain}] 풀 정규화 실패 {pool_id}: {e}")
            continue

    log.info(f"[{chain}] GeckoTerminal trending: {len(results)}개 수신")
    return results


def _normalize(pool: dict, token_map: dict, chain: str, timestamp: str) -> TrendingToken:
    """GeckoTerminal pool + token 데이터 → TrendingToken"""
    attr = pool["attributes"]

    # base_token 정보 (심볼, 이름, 컨트랙트)
    base_token_id = pool["relationships"]["base_token"]["data"]["id"]
    tok = token_map.get(base_token_id, {})

    # DEX 정보
    dex_rel = pool["relationships"].get("dex", {}).get("data", {})
    dex_id = dex_rel.get("id", "")

    # 가격 변동
    price_chg = attr.get("price_change_percentage", {})

    # 트랜잭션 수 (h1 buys / sells). 개별 값도 보존해야 buy_pressure 점수가 산다.
    txns_h1 = attr.get("transactions", {}).get("h1", {})
    buys_1h = int(txns_h1.get("buys") or 0)
    sells_1h = int(txns_h1.get("sells") or 0)
    txns_1h = buys_1h + sells_1h

    # 풀 나이 계산 (pool_created_at → pool_age_hours)
    pool_age_hours: Optional[float] = None
    pool_created_at = attr.get("pool_created_at")
    if pool_created_at:
        try:
            created_dt = datetime.fromisoformat(pool_created_at.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            pool_age_hours = (now_dt - created_dt).total_seconds() / 3600
        except Exception:
            pool_age_hours = None

    return TrendingToken(
        timestamp=timestamp,
        chain=chain,
        symbol=tok.get("symbol", ""),
        name=tok.get("name", ""),
        contract_address=tok.get("address", ""),
        price_usd=float(attr.get("base_token_price_usd") or 0),
        price_change_5m_pct=float(price_chg.get("m5") or 0),
        price_change_15m_pct=float(price_chg.get("m15") or 0),
        price_change_1h_pct=float(price_chg.get("h1") or 0),
        price_change_24h_pct=float(price_chg.get("h24") or 0) if price_chg.get("h24") else None,
        market_cap_usd=float(attr.get("market_cap_usd") or 0) or None,
        liquidity_usd=float(attr.get("reserve_in_usd") or 0),
        volume_1h_usd=float(attr.get("volume_usd", {}).get("h1") or 0),
        volume_24h_usd=float(attr.get("volume_usd", {}).get("h24") or 0),
        txns_1h=txns_1h,
        buys_1h=buys_1h,
        sells_1h=sells_1h,
        pool_age_hours=pool_age_hours,
        holders_total=0,
        holders_binance=0,
        audit_flags=[],
        lp_locked=None,
        pool_address=attr.get("address", ""),
        dex_id=dex_id,
    )
