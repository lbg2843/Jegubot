"""
Solana Trending Scraper
=========================
GeckoTerminal /networks/solana/trending_pools 사용.
API 키 불필요.

Solana 특이사항:
- 컨트랙트 = 토큰 민트 주소 (Solana pubkey 형식)
- 홀더 수: Solana 전용 API 필요 (현재 미구현, 0 처리)
- DEX: Raydium, Orca, Meteora 주력
- 가스비 토큰: SOL
"""

import time
import json
import logging
from dataclasses import asdict
from typing import Optional
from pathlib import Path

from gecko_client import TrendingToken, fetch_trending_pools


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("solana_scraper")


# ============================================================
# 메인 스크래퍼
# ============================================================

def scrape_solana_trending(limit: int = 20) -> list[TrendingToken]:
    """
    GeckoTerminal에서 Solana 트렌딩 풀 수집.
    홀더 수는 현재 미지원 (holders_total=0).
    """
    tokens = fetch_trending_pools("solana", limit=limit)
    log.info(f"[solana] 홀더 조회 미구현 — holders_total=0 유지")
    return tokens


# ============================================================
# 스토리지
# ============================================================

class SnapshotStore:
    def __init__(self, path: str = "solana_snapshots.jsonl"):
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
    import argparse, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="solana_snapshots.jsonl")
    ap.add_argument("--interval", type=int, default=0, help="0=1회, N=N초마다")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    store = SnapshotStore(args.out)

    def once():
        tokens = scrape_solana_trending(args.limit)
        if tokens:
            store.save(tokens)
            print(f"\n── Solana 트렌딩 상위 {min(10, len(tokens))}개 ──")
            for t in tokens[:10]:
                liq_mc = f"{t.liq_to_mcap_ratio*100:.1f}%" if t.liq_to_mcap_ratio else "N/A"
                print(f"  {t.symbol:<12} ${t.price_usd:<13.6f} "
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
