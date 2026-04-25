"""
Base Chain Trending Scraper
=============================
GeckoTerminal /networks/base/trending_pools 사용.
API 키 불필요. BaseScan 키 있으면 홀더 수 보완.

BSC 버전 대비 주요 차이:
- DEX: Aerodrome(주력), Uniswap v3
- 가스비 토큰: ETH
- Safety Gate: 최소 유동성 $150K (BSC보다 높음)
"""

import requests
import time
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from gecko_client import TrendingToken, fetch_trending_pools

# Base chain scraper is disabled as of 2026-04-25 for new entries.
# Reason: -$89 PnL over 8 trades, and the chain's lower-volatility
# profile did not fit the reflexivity strategy. The file remains as a
# reference implementation and for any legacy Base monitoring needs.


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("base_scraper")

BASESCAN_API = "https://api.basescan.org/api"


# ============================================================
# BaseScan 홀더 정보 보완 (키 있을 때만)
# ============================================================

def fetch_holder_count(contract: str, api_key: str) -> Optional[int]:
    """
    BaseScan V1 API로 Base 체인 토큰 홀더 수 조회.
    무료 API 키: https://basescan.org/myapikey
    """
    if not api_key:
        return None
    params = {
        "module": "token",
        "action": "tokenholdercount",
        "contractaddress": contract,
        "apikey": api_key,
    }
    try:
        r = requests.get(BASESCAN_API, params=params, timeout=10)
        data = r.json()
        if data.get("status") == "1":
            return int(data.get("result", 0))
    except Exception as e:
        log.debug(f"BaseScan holder 조회 실패 {contract}: {e}")
    return None


# ============================================================
# 메인 스크래퍼
# ============================================================

def scrape_base_trending(basescan_key: Optional[str] = None, limit: int = 20) -> list[TrendingToken]:
    """
    GeckoTerminal에서 Base 트렌딩 풀 수집.
    basescan_key 있으면 상위 20개 홀더 수 보완.
    """
    tokens = fetch_trending_pools("base", limit=limit)

    if basescan_key and tokens:
        log.info(f"BaseScan 홀더 정보 보완 ({min(20, len(tokens))}개)...")
        for t in tokens[:20]:
            count = fetch_holder_count(t.contract_address, basescan_key)
            if count:
                t.holders_total = count
            time.sleep(0.25)
    else:
        log.info("BaseScan 키 없음 — 홀더 조회 스킵 (GeckoTerminal 데이터만 사용)")

    return tokens


# ============================================================
# 스토리지
# ============================================================

class SnapshotStore:
    def __init__(self, path: str = "base_snapshots.jsonl"):
        self.path = Path(path)

    def save(self, tokens: list[TrendingToken]):
        with self.path.open("a", encoding="utf-8") as f:
            for t in tokens:
                d = asdict(t)
                d["avg_tx_size_usd"] = t.avg_tx_size_usd
                d["liq_to_mcap_ratio"] = t.liq_to_mcap_ratio
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        log.info(f"저장: {len(tokens)}건 → {self.path}")


# ============================================================
# CLI
# ============================================================

def main():
    import argparse, os, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--basescan-key", default=os.getenv("BASESCAN_API_KEY"))
    ap.add_argument("--out", default="base_snapshots.jsonl")
    ap.add_argument("--interval", type=int, default=0, help="0=1회, N=N초마다")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    store = SnapshotStore(args.out)

    def once():
        tokens = scrape_base_trending(args.basescan_key, args.limit)
        if tokens:
            store.save(tokens)
            print(f"\n── Base 트렌딩 상위 {min(10, len(tokens))}개 ──")
            for t in tokens[:10]:
                liq_mc = f"{t.liq_to_mcap_ratio*100:.1f}%" if t.liq_to_mcap_ratio else "N/A"
                print(f"  {t.symbol:<10} ${t.price_usd:<13.6f} "
                      f"1h={t.price_change_1h_pct:>+7.2f}% "
                      f"liq=${t.liquidity_usd/1000:>8.1f}K "
                      f"vol1h=${t.volume_1h_usd/1000:>8.1f}K "
                      f"txns={t.txns_1h:>5} "
                      f"liq/MC={liq_mc:>6}  [{t.dex_id}]")

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
