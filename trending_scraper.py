"""
Binance Web3 Trending (BSC) 스크래핑 레이어
==============================================

경로 우선순위:
  A. Binance 내부 JSON API (엔드포인트 확인 시 활성화)
  B. GeckoTerminal /networks/bsc/trending_pools (현재 메인)

주의사항:
  - Binance 내부 API는 공식 문서화 안 됨. 언제든 바뀔 수 있음.
  - GeckoTerminal rate limit: 30 req/min
  - 1시간 주기면 rate limit 이슈 없음.
"""

import requests
import time
import json
import logging
from dataclasses import asdict
from typing import Optional
from datetime import datetime
from pathlib import Path

from gecko_client import TrendingToken, fetch_trending_pools


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trending_scraper")

# Binance Web3 내부 API (엔드포인트 확인 필요 — placeholder)
BINANCE_W3_TRENDING = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/growth-fe/token/trending"
BINANCE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://web3.binance.com/en/markets/trending?chain=bsc",
    "Origin": "https://web3.binance.com",
}

BSCSCAN_API = "https://api.bscscan.com/api"


# ============================================================
# 경로 A: Binance 내부 API (발견 시 활성화)
# ============================================================

def fetch_binance_api(chain: str = "bsc", limit: int = 50) -> Optional[list]:
    """
    Binance Web3 내부 JSON API 호출.
    실제 엔드포인트는 브라우저 DevTools Network 탭에서 확인 필요.
    현재는 placeholder — 성공하면 normalize_binance() 구현 후 연결.
    """
    params = {"chain": chain, "limit": limit, "sortBy": "volume", "direction": "desc"}
    try:
        r = requests.get(BINANCE_W3_TRENDING, params=params,
                         headers=BINANCE_HEADERS, timeout=10)
        if r.status_code == 200:
            tokens = r.json().get("data", {}).get("tokens", [])
            if tokens:
                log.info(f"[경로A] Binance API: {len(tokens)}개 토큰 수신")
                return tokens
        log.debug(f"[경로A] Binance API HTTP {r.status_code} — GeckoTerminal로 폴백")
        return None
    except Exception as e:
        log.debug(f"[경로A] 실패: {e}")
        return None


# ============================================================
# BscScan 홀더 정보 보완
# ============================================================

def fetch_holder_count(contract: str, api_key: str) -> Optional[int]:
    """BscScan V1 API로 BSC 토큰 홀더 수 조회. rate limit: 5 req/sec"""
    if not api_key:
        return None
    params = {
        "module": "token",
        "action": "tokenholdercount",
        "contractaddress": contract,
        "apikey": api_key,
    }
    try:
        r = requests.get(BSCSCAN_API, params=params, timeout=10)
        data = r.json()
        if data.get("status") == "1":
            return int(data.get("result", 0))
    except Exception as e:
        log.debug(f"BscScan holder 조회 실패 {contract}: {e}")
    return None


# ============================================================
# 메인 스크래퍼
# ============================================================

def scrape_trending(chain: str = "bsc", bscscan_key: Optional[str] = None) -> list[TrendingToken]:
    """
    메인 진입점.
    경로 A (Binance 내부 API) → 실패 시 경로 B (GeckoTerminal).
    bscscan_key 있으면 상위 20개 홀더 수 보완.
    """
    tokens: list[TrendingToken] = []

    # 경로 A: Binance 내부 API
    raw = fetch_binance_api(chain)
    if raw:
        # TODO: normalize_binance(raw) 구현 후 연결
        # tokens = [normalize_binance(t) for t in raw]
        pass

    # 경로 B: GeckoTerminal (현재 메인 경로)
    if not tokens:
        log.info(f"[{chain}] GeckoTerminal trending_pools 사용")
        tokens = fetch_trending_pools(chain, limit=20)

    # BscScan 홀더 정보 보완
    if bscscan_key and tokens:
        log.info(f"BscScan 홀더 정보 보완 ({min(20, len(tokens))}개)...")
        for t in tokens[:20]:
            count = fetch_holder_count(t.contract_address, bscscan_key)
            if count:
                t.holders_total = count
            time.sleep(0.25)

    return tokens


# ============================================================
# 스토리지
# ============================================================

class SnapshotStore:
    """JSONL append-only 시계열 저장소. 재귀성 분석의 입력."""
    def __init__(self, path: str = "trending_snapshots.jsonl"):
        self.path = Path(path)

    def save(self, tokens: list[TrendingToken]):
        with self.path.open("a", encoding="utf-8") as f:
            for t in tokens:
                d = asdict(t)
                d["avg_tx_size_usd"] = t.avg_tx_size_usd
                d["liq_to_mcap_ratio"] = t.liq_to_mcap_ratio
                d["binance_holder_ratio"] = t.binance_holder_ratio
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        log.info(f"저장: {len(tokens)}건 → {self.path}")

    def load_recent(self, hours: int = 24) -> list[dict]:
        if not self.path.exists():
            return []
        cutoff = datetime.utcnow().timestamp() - hours * 3600
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                ts = datetime.fromisoformat(d["timestamp"]).timestamp()
                if ts >= cutoff:
                    out.append(d)
        return out

    def get_token_series(self, symbol: str, hours: int = 24) -> list[dict]:
        rows = self.load_recent(hours)
        return [r for r in rows if r.get("symbol") == symbol]


# ============================================================
# CLI
# ============================================================

def main():
    import argparse, os, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="bsc")
    ap.add_argument("--bscscan-key", default=os.getenv("BSCSCAN_API_KEY"))
    ap.add_argument("--out", default="trending_snapshots.jsonl")
    ap.add_argument("--interval", type=int, default=0, help="0=1회, N=N초마다")
    args = ap.parse_args()

    store = SnapshotStore(args.out)

    def once():
        tokens = scrape_trending(args.chain, args.bscscan_key)
        if tokens:
            store.save(tokens)
            print(f"\n── BSC 트렌딩 상위 {min(10, len(tokens))}개 ──")
            for t in tokens[:10]:
                liq_mc = f"{t.liq_to_mcap_ratio*100:.1f}%" if t.liq_to_mcap_ratio else "N/A"
                print(f"  {t.symbol:<10} ${t.price_usd:<13.6f} "
                      f"1h={t.price_change_1h_pct:>+7.2f}% "
                      f"liq=${t.liquidity_usd/1000:>8.1f}K "
                      f"vol1h=${t.volume_1h_usd/1000:>8.1f}K "
                      f"txns={t.txns_1h:>5}  [{t.dex_id}]")

    if args.interval == 0:
        once()
    else:
        while True:
            try:
                once()
            except Exception as e:
                log.error(f"루프 에러: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
