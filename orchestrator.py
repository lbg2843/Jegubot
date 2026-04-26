"""
Reflexivity Trading System — Main Orchestrator
=================================================
매 사이클마다:
1. 각 체인 스크래퍼 실행 (주기는 체인별 다름)
2. 스냅샷을 DB에 저장
3. 재귀성 분석 (Divergence 계산) → 진입 후보 도출
4. 안전성 게이트 통과한 후보만 포지션 매니저로
5. 기존 포지션 업데이트 및 청산 처리
6. Telegram 알림 전송
7. 로그 기록

실제 돌릴 때는:
- 각 체인 스크래퍼는 systemd timer로 별도 실행
- 이 오케스트레이터는 10분 주기로 돌면서 스크래퍼 결과 읽고 액션 결정
- 현재는 통합 버전으로 1h 주기 기본
"""

import time
import logging
import json
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from typing import Optional

# 기존 모듈 import
import sys
sys.path.insert(0, str(Path(__file__).parent))

from trending_scraper import scrape_trending, SnapshotStore
from base_scraper import scrape_base_trending
from solana_scraper import scrape_solana_trending
from mc_position_manager import (
    MultichainPositionManager,
    CHAIN_CONFIGS,
    DISABLED_CHAIN_CONFIGS,
    PortfolioConfig,
    ExitReason,
)
from notifier import format_entry_alert, format_exit_alert, format_summary_alert
from trading.executor import TradeExecutor
from trading.notifier import TelegramTradeNotifier
from trading.safety import SafetyCircuitBreaker


_BASE_DIR = Path(__file__).parent
ACTIVE_ENTRY_CHAINS = ("bsc", "solana")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "reflexivity.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,  # 스크래퍼 모듈이 먼저 basicConfig 잡아도 덮어씀
)
log = logging.getLogger("orchestrator")


# ============================================================
# 설정
# ============================================================

CONFIG = {
    "total_capital_usd": 10_000,
    "chain_allocation": {"bsc": 0.55, "solana": 0.45, "base": 0.00},
    "api_keys": {
        "bscscan": None,   # os.getenv("BSCSCAN_API_KEY")
        "basescan": None,  # os.getenv("BASESCAN_API_KEY") — 없으면 홀더 조회 스킵
    },
    "telegram": {
        "bot_token": None,  # os.getenv("TELEGRAM_BOT_TOKEN")
        "chat_id": None,    # os.getenv("TELEGRAM_CHAT_ID")
        "trading_bot_token": None,  # os.getenv("TELEGRAM_TRADING_BOT_TOKEN")
        "trading_chat_id": None,    # os.getenv("TELEGRAM_TRADING_CHAT_ID")
        "alert_entry": True,
        "alert_exit": True,
        "alert_summary_interval": 4,
    },
    "trading": {
        "mode": "disabled",
        "auto_approve_on_timeout": True,
        "daily_loss_limit_usd": 50.0,
        "max_consecutive_losses": 5,
        "max_position_size_usd": 100.0,
        "max_daily_trades": 30,
        "mock_entry_signal": False,
        "mock_entry_chain": "bsc",
        "approval_timeout_sec": 300,
        "max_signals_per_cycle": 1,
    },
    "data_dir": str(_BASE_DIR / "data"),
    "scraper_enrich_holders": False,
    "scraper_schedule": {
        "bsc": 3600,       # 1h
        "solana": 900,     # 15min (미구현, placeholder)
        "base": 7200,      # 2h
    },
}


# ============================================================
# Telegram 알림
# ============================================================

def send_telegram(msg: str, config: dict):
    """Telegram Bot API로 알림 전송."""
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    if not token or not chat_id:
        log.debug("Telegram 미설정, 알림 스킵")
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram 전송 실패: {e}")
        return False


# ============================================================
# Safety Gate (체인 공통)
# ============================================================

def passes_safety_gate(token_dict: dict, chain: str) -> tuple[bool, str]:
    """
    진입 전 안전성 체크. 체인별로 임계값 다를 수 있음.
    """
    symbol = token_dict.get("symbol", "?")
    liq = token_dict.get("liquidity_usd", 0)

    # 최소 유동성
    min_liq = {"bsc": 100_000, "solana": 50_000, "base": 150_000}
    if liq < min_liq.get(chain, 100_000):
        log.debug(f"[{chain}] GATE FAIL {symbol}: liq=${liq/1000:.0f}K < ${min_liq.get(chain)/1000:.0f}K")
        return False, "low_liquidity"

    # Liquidity / MCap 비율 (MCap 알 수 있을 때만)
    mcap = token_dict.get("market_cap_usd")
    if mcap and mcap > 0:
        ratio = liq / mcap
        if ratio < 0.03:
            log.debug(f"[{chain}] GATE FAIL {symbol}: liq/mcap={ratio:.2%} (<3%)")
            return False, "low_liq_ratio"

    # 최소 거래 건수
    if token_dict.get("txns_1h", 0) < 20:
        log.debug(f"[{chain}] GATE FAIL {symbol}: txns={token_dict.get('txns_1h')} (<20)")
        return False, "low_tx_count"

    # 극단적 가격 변동
    if token_dict.get("price_change_1h_pct", 0) > 50:
        log.debug(f"[{chain}] GATE FAIL {symbol}: 1h={token_dict.get('price_change_1h_pct'):+.1f}% (>50%)")
        return False, "already_pumped"

    # Audit flags
    if token_dict.get("audit_flags") and len(token_dict["audit_flags"]) > 0:
        log.debug(f"[{chain}] GATE FAIL {symbol}: audit_flags={token_dict['audit_flags']}")
        return False, f"audit_flags: {token_dict['audit_flags']}"

    return True, "ok"


# ============================================================
# Entry Signal (재귀성 엔진 placeholder)
# ============================================================

def detect_entry_signals(snapshots: list, chain: str) -> list[dict]:
    """
    재귀성 분석 결과 진입 후보 반환.
    완전한 Divergence Engine은 별도 모듈 (향후 Module 2).
    현재는 간단한 momentum + liquidity 기반 placeholder.
    """
    candidates = []
    for snap in snapshots:
        symbol = snap.get("symbol", "?")
        price_chg = snap.get("price_change_1h_pct", 0)
        txns = snap.get("txns_1h", 0)
        vol = snap.get("volume_1h_usd", 0)
        avg_tx = vol / max(txns, 1)

        if not (5 < price_chg < 25):
            log.debug(f"[{chain}] SKIP {symbol}: 1h={price_chg:+.2f}% (범위 5~25% 아님)")
            continue
        if txns < 50:
            log.debug(f"[{chain}] SKIP {symbol}: txns={txns} (<50)")
            continue
        if avg_tx < 100:
            log.debug(f"[{chain}] SKIP {symbol}: avg_tx=${avg_tx:.0f} (<$100)")
            continue
        candidates.append(snap)

    if candidates:
        log.info(f"[{chain}] {len(snapshots)}개 중 {len(candidates)}개 진입 후보:")
        for c in candidates:
            txns = c.get("txns_1h", 0)
            vol = c.get("volume_1h_usd", 0)
            avg_tx = vol / max(txns, 1)
            liq = c.get("liquidity_usd", 0)
            log.info(
                f"  ★ CANDIDATE: {c.get('symbol', '?')}\n"
                f"    price:      ${c.get('price_usd', 0):.6f}\n"
                f"    change_1h:  {c.get('price_change_1h_pct', 0):+.2f}%\n"
                f"    liquidity:  ${liq/1000:.0f}K\n"
                f"    volume_1h:  ${vol/1000:.0f}K\n"
                f"    txns_1h:    {txns}\n"
                f"    avg_tx:     ${avg_tx:.0f}"
            )
    else:
        log.info(f"[{chain}] {len(snapshots)}개 중 0개 진입 후보")

    return candidates


# ============================================================
# 메인 사이클
# ============================================================

class Orchestrator:
    def __init__(self, config: dict, dry_run: bool = False, trading_mode: str = "disabled"):
        self.config = config
        self.dry_run = dry_run
        self.trading_mode = trading_mode
        self.executor: Optional[TradeExecutor] = None
        self.trade_notifier: Optional[TelegramTradeNotifier] = None
        self.data_dir = Path(config["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path = self.data_dir / "run_heartbeat.json"

        pf_cfg = PortfolioConfig(chain_allocation=config["chain_allocation"])
        runtime_chain_configs = dict(CHAIN_CONFIGS)
        runtime_chain_configs.update(
            {
                chain: cfg
                for chain, cfg in DISABLED_CHAIN_CONFIGS.items()
                if chain in self._legacy_position_chains()
            }
        )
        self.pm = MultichainPositionManager(
            runtime_chain_configs, pf_cfg,
            storage_path=str(self.data_dir / "positions.jsonl")
        )

        self.last_scrape: dict[str, float] = {"bsc": 0, "solana": 0, "base": 0}
        self.last_summary_bucket: str | None = None
        self.mock_signal_used = False
        if self.dry_run:
            log.info("[DRY-RUN] 포지션 진입/청산 알림 비활성화")

        if self.trading_mode in {"dry_run", "live"}:
            self._init_trade_executor()

    def _write_heartbeat(self, stage: str, **extra):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "stage": stage,
            "trading_mode": self.trading_mode,
            "dry_run": self.dry_run,
        }
        payload.update(extra)
        self.heartbeat_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _init_trade_executor(self):
        trading_cfg = self.config["trading"]
        safety = SafetyCircuitBreaker(
            self.data_dir / "trading_safety_state.json",
            daily_loss_limit_usd=trading_cfg["daily_loss_limit_usd"],
            max_consecutive_losses=trading_cfg["max_consecutive_losses"],
            max_position_size_usd=trading_cfg["max_position_size_usd"],
            max_daily_trades=trading_cfg["max_daily_trades"],
        )
        notifier = TelegramTradeNotifier(
            bot_token=self.config["telegram"]["trading_bot_token"],
            chat_id=self.config["telegram"]["trading_chat_id"],
            state_path=self.data_dir / "telegram_trade_state.json",
            auto_approve_on_timeout=trading_cfg["auto_approve_on_timeout"],
        )
        self.trade_notifier = notifier
        self.executor = TradeExecutor(
            self.pm,
            notifier,
            safety,
            mode=self.trading_mode,
            state_path=self.data_dir / "trade_executor_state.json",
        )
        if not notifier._enabled():
            log.warning(
                "trading notifier is disabled; set TELEGRAM_TRADING_BOT_TOKEN and "
                "TELEGRAM_TRADING_CHAT_ID for real button approval"
            )
        notifier.configure_handlers(
            status_handler=safety.get_status,
            stop_handler=self._manual_stop,
            unhalt_handler=self._manual_unhalt,
        )

    def _manual_stop(self) -> str:
        if not self.executor:
            return "trade executor is not active"
        self.executor.safety.halt_for_24h("manual_stop")
        return self.executor.emergency_close_all()

    def _manual_unhalt(self) -> str:
        if not self.executor:
            return "trade executor is not active"
        self.executor.safety.manual_unhalt()
        return "manual halt cleared"

    # --------------------------------------------------------
    # 체인별 스크래핑
    # --------------------------------------------------------

    def _legacy_position_chains(self) -> set[str]:
        storage_path = self.data_dir / "positions.jsonl"
        if not storage_path.exists():
            return set()

        chains: set[str] = set()
        with storage_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not record.get("is_closed") and record.get("chain"):
                    chains.add(record["chain"])
        return chains

    def _chains_for_cycle(self) -> list[str]:
        chains = list(ACTIVE_ENTRY_CHAINS)
        for chain in sorted({pos.chain for pos in self.pm.positions.values()}):
            if chain not in chains:
                chains.append(chain)
        return chains

    def _mock_signal_for_chain(self, chain: str) -> dict:
        return {
            "symbol": f"MOCK_{chain.upper()}",
            "contract_address": f"mock-{chain}-contract",
            "pool_address": f"mock-{chain}-pool",
            "price_usd": 0.001234,
            "price_change_1h_pct": 12.5,
            "liquidity_usd": 450_000.0,
            "volume_1h_usd": 85_000.0,
            "txns_1h": 42,
            "holders_binance": 0,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _inject_mock_signal(self, snapshots_by_chain: dict[str, list[dict]]):
        trading_cfg = self.config["trading"]
        if not trading_cfg["mock_entry_signal"] or self.mock_signal_used:
            return
        chain = trading_cfg["mock_entry_chain"]
        snapshots_by_chain.setdefault(chain, [])
        snapshots_by_chain[chain] = list(snapshots_by_chain[chain]) + [self._mock_signal_for_chain(chain)]
        self.mock_signal_used = True
        log.info("[MOCK] injected entry signal for [%s]", chain.upper())

    def _build_trade_signal(self, snapshot: dict, chain: str) -> dict:
        cfg = self.pm.chain_configs[chain]
        capped_size_usd = min(
            self._recommended_position_size_usd(chain),
            float(self.config["trading"]["max_position_size_usd"]),
        )
        return {
            **snapshot,
            "chain": chain,
            "position_size_usd": capped_size_usd,
            "stop_loss_pct": cfg.stop_loss_pct,
            "take_profit_pct": cfg.take_profit_pct,
            "max_hold_hours": cfg.max_hold_hours,
            "profit_lock": True,
            "approval_timeout": self.config["trading"]["approval_timeout_sec"],
            "total_capital_usd": self.config["total_capital_usd"],
        }

    def scrape_chain(self, chain: str) -> list[dict]:
        """체인별 스크래퍼 호출. 실패 시 마지막 저장본 로드."""
        now = time.time()
        interval = self.config["scraper_schedule"][chain]

        if now - self.last_scrape[chain] < interval:
            log.debug(f"[{chain}] 아직 스크랩 주기 아님, 캐시 사용")
            return self.load_latest_snapshots(chain)

        log.info(f"[{chain}] 스크래핑 시작...")
        tokens = []

        if chain == "bsc":
            bscscan_key = self.config["api_keys"].get("bscscan") if self.config["scraper_enrich_holders"] else None
            tokens = scrape_trending("bsc", bscscan_key)
        elif chain == "base":
            basescan_key = self.config["api_keys"].get("basescan") if self.config["scraper_enrich_holders"] else None
            tokens = scrape_base_trending(basescan_key)
        elif chain == "solana":
            tokens = scrape_solana_trending()

        self.last_scrape[chain] = now

        # 저장
        if tokens:
            store = SnapshotStore(str(self.data_dir / f"{chain}_snapshots.jsonl"))
            store.save(tokens)
            log.info(f"[{chain}] {len(tokens)}개 스냅샷 저장")

        if tokens:
            return [self._token_to_dict(t) for t in tokens]

        cached = self.load_latest_snapshots(chain)
        if cached:
            log.warning(f"[{chain}] scraper returned no fresh data; using cached snapshots ({len(cached)} items)")
        return cached

    def _token_to_dict(self, token) -> dict:
        """TrendingToken → dict"""
        d = asdict(token)
        if hasattr(token, "avg_tx_size_usd"):
            d["avg_tx_size_usd"] = token.avg_tx_size_usd
        if hasattr(token, "liq_to_mcap_ratio"):
            d["liq_to_mcap_ratio"] = token.liq_to_mcap_ratio
        return d

    def load_latest_snapshots(self, chain: str) -> list[dict]:
        """마지막 스크랩 결과 로드 (타임스탬프 기준 최신만)"""
        path = self.data_dir / f"{chain}_snapshots.jsonl"
        if not path.exists():
            return []
        # 마지막 N건 로드 + 최신 timestamp 그룹만 반환
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        if not lines or lines == [""]:
            return []

        # 뒤에서 역순으로 읽으며 같은 timestamp끼리 묶기
        records = [json.loads(ln) for ln in lines[-200:]]
        if not records:
            return []
        latest_ts = records[-1]["timestamp"]
        return [r for r in records if r["timestamp"] == latest_ts]

    # --------------------------------------------------------
    # 사이클 실행
    # --------------------------------------------------------

    def run_cycle(self):
        cycle_started_at = datetime.now()
        self._write_heartbeat("cycle_start")
        log.info("=" * 60)
        log.info("사이클 시작")
        log.info("=" * 60)

        if self.trade_notifier:
            self._write_heartbeat("before_pending_commands")
            self.trade_notifier.process_pending_commands(
                long_poll_timeout=0,
                request_timeout=1.5,
                max_attempts=1,
            )
            self._write_heartbeat("after_pending_commands")
        if self.executor:
            self._write_heartbeat("before_pending_entries")
            for result in self.executor.process_pending_entries():
                log.info("executor pending result: %s", result.get("status"))
            self._write_heartbeat("after_pending_entries")

        snapshots_by_chain = {}
        self._write_heartbeat("before_scrape")

        # 1. 스크래핑 (체인별)
        # Base disabled for new entries on 2026-04-25. Keep scraping any
        # legacy position chains so existing positions can exit naturally.
        for chain in self._chains_for_cycle():
            try:
                self._write_heartbeat("before_scrape_chain", chain=chain)
                snapshots_by_chain[chain] = self.scrape_chain(chain)
                self._write_heartbeat("after_scrape_chain", chain=chain, snapshot_count=len(snapshots_by_chain[chain]))
            except Exception as e:
                log.error(f"[{chain}] 스크래핑 실패: {e}")
                cached = self.load_latest_snapshots(chain)
                if cached:
                    log.warning(f"[{chain}] using cached snapshots after scrape failure ({len(cached)} items)")
                snapshots_by_chain[chain] = cached
                self._write_heartbeat("scrape_chain_failed", chain=chain, snapshot_count=len(cached), error=str(e))

        # 2. 기존 포지션 업데이트 + 청산
        self._write_heartbeat("before_position_update")
        self._inject_mock_signal(snapshots_by_chain)
        exits = self.pm.update_all(snapshots_by_chain)
        self._write_heartbeat("after_position_update", exit_count=len(exits))
        for pos, reason, msg in exits:
            current_price = pos.current_price
            if self.executor:
                result = self.executor.handle_exit_signal(pos, reason)
                log.info("executor exit result: %s", result.get("status"))
                continue
            if not self.dry_run:
                closed_pos = self.pm.close_position(pos, reason, msg)
                self._notify_exit(closed_pos, reason, current_price, cycle_started_at)
            else:
                log.info(f"[DRY-RUN] 청산 스킵: {pos.symbol} ({reason})")

        # 3. 신규 진입 후보 탐지
        self._write_heartbeat("before_entry_scan")
        for chain, snapshots in snapshots_by_chain.items():
            if chain not in ACTIVE_ENTRY_CHAINS:
                continue
            if not snapshots:
                continue
            candidates = detect_entry_signals(snapshots, chain)
            if self.config["trading"]["mock_entry_signal"] and chain == self.config["trading"]["mock_entry_chain"]:
                mock_candidates = [
                    snap for snap in snapshots
                    if str(snap.get("contract_address", "")).startswith("mock-")
                ]
                candidates = mock_candidates + candidates
            max_signals_per_cycle = int(self.config["trading"]["max_signals_per_cycle"])
            if self.executor and max_signals_per_cycle > 0:
                candidates = candidates[:max_signals_per_cycle]
            for cand in candidates:
                ok, reason = passes_safety_gate(cand, chain)
                if not ok:
                    log.debug(f"[{chain}] {cand['symbol']} 게이트 실패: {reason}")
                    continue
                if self.executor:
                    result = self.executor.submit_entry_signal(self._build_trade_signal(cand, chain))
                    log.info("executor entry result: %s", result.get("status"))
                    continue
                if not self.dry_run:
                    pos = self.pm.open_position(
                        chain, cand,
                        total_capital_usd=self.config["total_capital_usd"]
                    )
                    if pos:
                        self._notify_entry(cand, pos)
                else:
                    log.info(
                        f"[DRY-RUN] 진입 후보: [{chain.upper()}] {cand['symbol']} "
                        f"price=${cand.get('price_usd', 0):.6f} "
                        f"1h={cand.get('price_change_1h_pct', 0):+.2f}% "
                        f"liq=${cand.get('liquidity_usd', 0)/1000:.0f}K "
                        f"txns={cand.get('txns_1h', 0)}"
                    )
                    preview = format_entry_alert(
                        cand,
                        chain,
                        self._recommended_position_size_usd(chain),
                        self.pm.chain_configs[chain],
                    )
                    log.info("[DRY-RUN] Telegram ENTRY preview:\n%s", preview)

        # 4. 리포트
        self._write_heartbeat("before_summary")
        self._maybe_send_summary(cycle_started_at)
        self._write_heartbeat("cycle_complete", open_positions=len(self.pm.positions))
        log.info("\n" + self.pm.summary())
        log.info("사이클 종료\n")

    def _recommended_position_size_usd(self, chain: str) -> float:
        cfg = self.pm.chain_configs[chain]
        return self.config["total_capital_usd"] * (cfg.capital_per_position_pct / 100.0)

    def _notify_entry(self, snapshot: dict, pos):
        if not self.config["telegram"]["alert_entry"]:
            return
        msg = format_entry_alert(
            snapshot,
            pos.chain,
            pos.size_usd,
            self.pm.chain_configs[pos.chain],
        )
        if len(msg) > 4096:
            msg = msg[:4090] + "..."
        send_telegram(msg, self.config)

    def _notify_exit(self, pos, reason, current_price: float, current_timestamp: datetime):
        if not self.config["telegram"]["alert_exit"]:
            return
        msg = format_exit_alert(pos, reason, current_price, current_timestamp)
        if len(msg) > 4096:
            msg = msg[:4090] + "..."
        send_telegram(msg, self.config)

    def _realized_today_usd(self, as_of: datetime) -> float:
        storage_path = self.data_dir / "positions.jsonl"
        if not storage_path.exists():
            return 0.0
        total = 0.0
        today = as_of.date()
        with storage_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not record.get("is_closed") or not record.get("exit_timestamp"):
                    continue
                try:
                    exit_ts = datetime.fromisoformat(record["exit_timestamp"])
                except Exception:
                    continue
                if exit_ts.date() == today:
                    total += float(record.get("realized_pnl_usd") or 0.0)
        return total

    def _cash_available_usd(self) -> float:
        committed = sum(pos.size_usd for pos in self.pm.positions.values())
        return max(0.0, self.config["total_capital_usd"] - committed)

    def _maybe_send_summary(self, as_of: datetime):
        interval = int(self.config["telegram"]["alert_summary_interval"])
        if interval <= 0:
            return
        if as_of.hour % interval != 0:
            return
        bucket = as_of.strftime("%Y-%m-%d %H")
        if bucket == self.last_summary_bucket:
            return

        self.last_summary_bucket = bucket
        message = format_summary_alert(
            open_positions=list(self.pm.positions.values()),
            realized_today_usd=self._realized_today_usd(as_of),
            cash_available_usd=self._cash_available_usd(),
            as_of=as_of,
        )
        if len(message) > 4096:
            message = message[:4090] + "..."

        if self.dry_run:
            log.info("[DRY-RUN] Telegram SUMMARY preview:\n%s", message)
            return
        send_telegram(message, self.config)


# ============================================================
# CLI 진입점
# ============================================================

def main():
    import argparse, os, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv(_BASE_DIR / ".env")
    else:
        env_path = _BASE_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회만 실행")
    ap.add_argument("--interval", type=int, default=600, help="루프 주기 초 (기본 10분)")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--dry-run", action="store_true", help="실제 진입 없이 로깅만")
    ap.add_argument("--verbose", action="store_true", help="DEBUG 레벨 로그 (탈락 이유 포함)")
    args = ap.parse_args()

    # 환경변수에서 API 키 로드
    CONFIG["api_keys"]["bscscan"] = os.getenv("BSCSCAN_API_KEY")
    CONFIG["api_keys"]["basescan"] = os.getenv("BASESCAN_API_KEY")  # 없으면 None, 홀더 조회 스킵
    CONFIG["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
    CONFIG["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID")
    CONFIG["telegram"]["trading_bot_token"] = os.getenv("TELEGRAM_TRADING_BOT_TOKEN")
    CONFIG["telegram"]["trading_chat_id"] = os.getenv("TELEGRAM_TRADING_CHAT_ID")
    CONFIG["telegram"]["alert_entry"] = os.getenv("ALERT_ENTRY", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["telegram"]["alert_exit"] = os.getenv("ALERT_EXIT", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["telegram"]["alert_summary_interval"] = int(os.getenv("ALERT_SUMMARY_INTERVAL", "4"))
    CONFIG["trading"]["mode"] = os.getenv("TRADING_MODE", "").strip().lower() or "disabled"
    CONFIG["trading"]["auto_approve_on_timeout"] = os.getenv("AUTO_APPROVE_ON_TIMEOUT", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["trading"]["daily_loss_limit_usd"] = float(os.getenv("DAILY_LOSS_LIMIT_USD", "50"))
    CONFIG["trading"]["max_consecutive_losses"] = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
    CONFIG["trading"]["max_position_size_usd"] = float(os.getenv("MAX_POSITION_SIZE_USD", "100"))
    CONFIG["trading"]["max_daily_trades"] = int(os.getenv("MAX_DAILY_TRADES", "30"))
    CONFIG["trading"]["mock_entry_signal"] = os.getenv("MOCK_ENTRY_SIGNAL", "false").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["trading"]["mock_entry_chain"] = os.getenv("MOCK_ENTRY_CHAIN", "bsc").strip().lower()
    CONFIG["trading"]["approval_timeout_sec"] = int(os.getenv("APPROVAL_TIMEOUT_SEC", "300"))
    CONFIG["trading"]["max_signals_per_cycle"] = int(os.getenv("MAX_SIGNALS_PER_CYCLE", "1"))
    CONFIG["total_capital_usd"] = args.capital
    CONFIG["chain_allocation"] = {
        "bsc": float(os.getenv("CHAIN_ALLOCATION_BSC", "0.55")),
        "solana": float(os.getenv("CHAIN_ALLOCATION_SOLANA", "0.45")),
        "base": float(os.getenv("CHAIN_ALLOCATION_BASE", "0.00")),
    }
    CONFIG["scraper_schedule"]["bsc"] = int(os.getenv("SCRAPE_INTERVAL_BSC", str(CONFIG["scraper_schedule"]["bsc"])))
    CONFIG["scraper_schedule"]["solana"] = int(os.getenv("SCRAPE_INTERVAL_SOLANA", str(CONFIG["scraper_schedule"]["solana"])))
    CONFIG["scraper_schedule"]["base"] = int(os.getenv("SCRAPE_INTERVAL_BASE", str(CONFIG["scraper_schedule"]["base"])))
    CONFIG["scraper_enrich_holders"] = os.getenv("ENRICH_HOLDERS", "false").strip().lower() in {"1", "true", "yes", "on"}

    # --verbose: DEBUG 레벨 활성화 (탈락 이유 등 상세 로그)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("DEBUG 모드 활성화")

    # DRY_RUN: CLI --dry-run 플래그 OR .env DRY_RUN=true 둘 다 인정
    env_dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    dry_run = args.dry_run or env_dry_run
    trading_mode = CONFIG["trading"]["mode"]
    if trading_mode == "dry_run":
        log.info("execution mode: TRADING_MODE=dry_run (approval flow + simulated execution)")
    else:
        pass
    log.info(f"실행 모드: {'DRY-RUN (시그널만, 실제 매매 없음)' if dry_run else 'LIVE (실제 매매 가능)'}")

    orch = Orchestrator(CONFIG, dry_run=dry_run, trading_mode=CONFIG["trading"]["mode"])

    if args.once:
        orch.run_cycle()
    else:
        log.info(f"루프 시작 (주기 {args.interval}초)")
        while True:
            try:
                orch.run_cycle()
            except KeyboardInterrupt:
                log.info("종료 요청")
                break
            except Exception as e:
                log.error(f"사이클 에러: {e}", exc_info=True)

            next_time = datetime.now().strftime("%H:%M:%S")
            from datetime import timedelta
            next_run = (datetime.now() + timedelta(seconds=args.interval)).strftime("%H:%M:%S")
            log.info(f"대기 중... 다음 사이클: {next_run} ({args.interval}초 후) — Ctrl+C로 종료")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
