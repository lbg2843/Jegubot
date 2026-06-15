"""
Reflexivity Trading System - Main Orchestrator
================================================
주요 기능:
1. 트렌딩 토큰 스캔 (BSC / Solana / Base)
2. 스냅샷 DB 저장
3. Divergence (반전) 신호 감지
4. 진입 후보 풀에서 안전장치 통과 확인
5. 포지션 관리 + 청산 시그널 처리
6. Telegram 알림
7. 대시보드 업데이트

운영 모드:
- 기본: dry_run (systemd timer로 1분 주기)
- live: 실제 거래 실행 (10분 주기 + Telegram 승인 버튼 흐름)
- backtest: 과거 1h 스냅샷 기반
"""

import os as _os_init
from pathlib import Path as _Path_init
try:
    from dotenv import load_dotenv as _load_dotenv_init
    _load_dotenv_init(_Path_init(__file__).parent / ".env", override=True)
except ImportError:
    pass

import atexit
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import asdict
from typing import Optional
from entry_logger import log_entry_snapshot

# 기존 모듈 import
import sys
sys.path.insert(0, str(Path(__file__).parent))

from trending_scraper import scrape_trending, SnapshotStore
from base_scraper import scrape_base_trending
from solana_scraper import scrape_solana_trending
from mc_position_manager import (
    ChainConfig,
    MultichainPositionManager,
    CHAIN_CONFIGS,
    DISABLED_CHAIN_CONFIGS,
    PortfolioConfig,
    ExitReason,
)
from notifier import format_entry_alert, format_exit_alert, format_summary_alert
from trading.executor import TradeExecutor
from trading.notifier import TelegramTradeNotifier
from score_model import evaluate_candidate, evaluate_multi
from score_outcome_tracker import add_pending as add_score_outcome_pending, process_pending as process_score_outcomes
from trading.safety import SafetyCircuitBreaker
from trading.time_utils import iso_utc_now, parse_iso_utc, utc_now
from eth_macro_filter import get_eth_macro_filter


_BASE_DIR = Path(__file__).parent
ACTIVE_ENTRY_CHAINS = ("bsc", "base")
MAJOR_TOKEN_DENYLIST = {
    "USDT", "USDC", "DAI", "WETH", "WBTC", "CBBTC", "ETH", "BNB",
    "FIL", "ARK", "WBNB", "BUSD",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "reflexivity.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,  # 다른 모듈이 먼저 basicConfig 호출했어도 강제 적용
)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("orchestrator")

LOCK_PATH = _BASE_DIR / "data" / "orchestrator.lock"


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(content, encoding=encoding)
    os.replace(tmp_path, path)


def _score_shadow_enabled() -> bool:
    return os.getenv("SCORE_SHADOW_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _score_shadow_path() -> Path:
    path = _BASE_DIR / "data" / "score_shadow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path


def _score_shadow_weights() -> dict:
    return {
        "momentum": float(os.getenv("SCORE_W_MOMENTUM", "0.30")),
        "tx_quality": float(os.getenv("SCORE_W_TX_QUALITY", "0.25")),
        "liquidity": float(os.getenv("SCORE_W_LIQUIDITY", "0.20")),
        "buy_pressure": float(os.getenv("SCORE_W_BUY_PRESSURE", "0.25")),
        "penalty_overheated": float(os.getenv("SCORE_PENALTY_OVERHEATED", "0.40")),
    }


def _score_shadow_threshold() -> float:
    return float(os.getenv("SCORE_ENTRY_THRESHOLD", "0.65"))


def _load_prev_snapshot_candidates(chain: str) -> dict[str, list[dict]]:
    path = _BASE_DIR / "data" / f"{chain}_snapshots.jsonl"
    if not path.exists():
        return {}

    rows_by_token: dict[str, list[dict]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}

    for line in lines[-5000:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        token_address = str(
            row.get("contract_address")
            or row.get("token_address")
            or ""
        ).strip().lower()
        if not token_address:
            continue
        rows_by_token.setdefault(token_address, []).append(row)
    return rows_by_token


def _find_prev_snapshot(snapshot: dict, historical_rows: dict[str, list[dict]]) -> dict | None:
    token_address = str(
        snapshot.get("contract_address")
        or snapshot.get("token_address")
        or ""
    ).strip().lower()
    if not token_address:
        return None
    current_ts_raw = snapshot.get("timestamp")
    if not current_ts_raw:
        return None
    try:
        current_ts = parse_iso_utc(str(current_ts_raw))
    except Exception:
        return None

    best_row = None
    best_ts = None
    for row in historical_rows.get(token_address, []):
        row_ts_raw = row.get("timestamp")
        if not row_ts_raw:
            continue
        try:
            row_ts = parse_iso_utc(str(row_ts_raw))
        except Exception:
            continue
        age_seconds = (current_ts - row_ts).total_seconds()
        if age_seconds < 50 * 60 or age_seconds > 70 * 60:
            continue
        if best_ts is None or row_ts > best_ts:
            best_ts = row_ts
            best_row = row
    return best_row


def _append_score_shadow_snapshot(
    snapshot: dict,
    chain: str,
    *,
    reflexivity_passed: bool,
    skip_reason: str | None,
    prev_snapshot: dict | None,
) -> None:
    if not _score_shadow_enabled():
        return
    # buys_1h / sells_1h now persisted via gecko_client; setdefault is a fallback for legacy snapshots.
    enriched_snapshot = dict(snapshot)
    enriched_snapshot.setdefault("buys_1h", 0)
    enriched_snapshot.setdefault("sells_1h", 0)
    record_timestamp = str(enriched_snapshot.get("timestamp") or iso_utc_now())
    multi = evaluate_multi(
        enriched_snapshot,
        prev_snapshot,
        _score_shadow_weights(),
        _score_shadow_threshold(),
    )
    # === SHADOW VALIDATION (5/14) ===
    # Hypothesis: final_score [0.15, 0.25) on Base = golden zone
    # Based on 780-record analysis: 98-100% win rate in this range
    # Started: 2026-05-14
    # Validation period: 7 days minimum
    # DO NOT use this for entry decisions yet - validation only
    _final_score = float(multi.score_v1_reflexivity.final_score or 0)
    _chain = str(chain or "").lower()
    special_gate_candidate = 0.15 <= _final_score < 0.25 and _chain == "base"
    special_gate_reason = (
        "final_score_0.15_0.25_base"
        if special_gate_candidate
        else f"final_score={_final_score:.4f}_chain={_chain}"
    )
    record = {
        "timestamp": record_timestamp,
        "chain": chain,
        "symbol": enriched_snapshot.get("symbol", "?"),
        "token_address": enriched_snapshot.get("contract_address"),
        "entry_path": enriched_snapshot.get("entry_path") or "reflexivity",
        "reflexivity_passed": bool(reflexivity_passed),
        "skip_reason": skip_reason,
        "entry_price": float(enriched_snapshot.get("price_usd", 0) or 0),
        "price_usd": float(enriched_snapshot.get("price_usd", 0) or 0),
        "price_change_5m_pct": float(enriched_snapshot.get("price_change_5m_pct", 0) or 0),
        "price_change_15m_pct": float(enriched_snapshot.get("price_change_15m_pct", 0) or 0),
        "price_change_1h_pct": float(enriched_snapshot.get("price_change_1h_pct", 0) or 0),
        "price_change_24h_pct": float(enriched_snapshot.get("price_change_24h_pct", 0) or 0),
        "pool_age_hours": enriched_snapshot.get("pool_age_hours"),
        "avg_tx_size_usd": float(enriched_snapshot.get("avg_tx_size_usd", 0) or 0),
        "liquidity_usd": float(enriched_snapshot.get("liquidity_usd", 0) or 0),
        "buys_1h": int(enriched_snapshot.get("buys_1h", 0) or 0),
        "sells_1h": int(enriched_snapshot.get("sells_1h", 0) or 0),
        "special_gate_candidate": special_gate_candidate,
        "special_gate_reason": special_gate_reason,
        "snapshot": {
            "price_change_5m_pct": float(enriched_snapshot.get("price_change_5m_pct", 0) or 0),
            "price_change_15m_pct": float(enriched_snapshot.get("price_change_15m_pct", 0) or 0),
            "price_change_1h_pct": float(enriched_snapshot.get("price_change_1h_pct", 0) or 0),
            "price_change_24h_pct": float(enriched_snapshot.get("price_change_24h_pct", 0) or 0),
            "pool_age_hours": enriched_snapshot.get("pool_age_hours"),
            "avg_tx_size_usd": float(enriched_snapshot.get("avg_tx_size_usd", 0) or 0),
            "liquidity_usd": float(enriched_snapshot.get("liquidity_usd", 0) or 0),
            "volume_1h_usd": float(enriched_snapshot.get("volume_1h_usd", 0) or 0),
            "volume_24h_usd": float(enriched_snapshot.get("volume_24h_usd", 0) or 0),
            "buys_1h": int(enriched_snapshot.get("buys_1h", 0) or 0),
            "sells_1h": int(enriched_snapshot.get("sells_1h", 0) or 0),
            "special_gate_candidate": special_gate_candidate,
            "special_gate_reason": special_gate_reason,
            "entry_path": enriched_snapshot.get("entry_path") or "reflexivity",
        },
        "score_v1": asdict(multi.score_v1_reflexivity),
        "final_score": multi.score_v1_reflexivity.final_score,
        "passed": multi.score_v1_reflexivity.passed,
        "score_v2_liquidity": multi.score_v2_liquidity,
        "score_v3_volume": multi.score_v3_volume,
        "score_v4_density": multi.score_v4_density,
        "raw_signals": {
            "liquidity_velocity_pct": multi.liquidity_velocity_raw,
            "volume_acceleration_ratio": multi.volume_acceleration_raw,
            "txns_per_hour": multi.txns_per_hour_raw,
            "tx_to_liq_ratio": multi.tx_to_liq_ratio_raw,
        },
        "entry_taken": False,
    }
    with _score_shadow_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    add_score_outcome_pending(record)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    try:
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout or "").strip()
        if not output or output.startswith("INFO:"):
            return False
        return "python.exe" in output.lower()
    except Exception:
        return False


def _release_single_instance_lock() -> None:
    try:
        if not LOCK_PATH.exists():
            return
        current = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
        if current and int(current) == os.getpid():
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def acquire_single_instance_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    while True:
        try:
            with LOCK_PATH.open("x", encoding="utf-8") as handle:
                handle.write(str(current_pid))
            atexit.register(_release_single_instance_lock)
            return
        except FileExistsError:
            existing_pid = None
            try:
                existing_pid = int(LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip())
            except Exception:
                existing_pid = None

            if existing_pid and existing_pid != current_pid and _pid_is_alive(existing_pid):
                log.error("orchestrator already running (PID %s). Exiting.", existing_pid)
                raise SystemExit(1)

            try:
                LOCK_PATH.unlink(missing_ok=True)
            except Exception:
                log.error("orchestrator lock exists but could not be cleared: %s", LOCK_PATH)
                raise SystemExit(1)


# ============================================================
# ?ㅼ젙
# ============================================================

CONFIG = {
    "total_capital_usd": float(os.getenv("TOTAL_CAPITAL_USD", "10000")),
    "chain_allocation": {"bsc": 0.55, "solana": 0.45, "base": 0.00},
    "api_keys": {
        "bscscan": None,   # os.getenv("BSCSCAN_API_KEY")
        "basescan": None,  # os.getenv("BASESCAN_API_KEY") ???놁쑝硫????議고쉶 ?ㅽ궢
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
        "daily_loss_limit_usd": 100.0,
        "max_consecutive_losses": 8,
        "max_position_size_usd": 100.0,
        "max_daily_trades": 30,
        "halt_duration_hours_first": 1,
        "halt_duration_hours_second": 4,
        "halt_duration_hours_third": 24,
        "loss_window_hours": 1.0,
        "mock_entry_signal": False,
        "mock_entry_chain": "bsc",
        "approval_timeout_sec": 300,
        "max_signals_per_cycle": 1,
    },
    "dashboard": {
        "enabled": True,
        "update_interval_minutes": 10,
    },
    "data_dir": str(_BASE_DIR / "data"),
    "scraper_enrich_holders": False,
    "scraper_schedule": {
        "bsc": 3600,       # 1h
        "solana": 900,     # 15min (誘멸뎄?? placeholder)
        "base": 7200,      # 2h
    },
}


# ============================================================
# Telegram ?뚮┝
# ============================================================

def send_telegram(msg: str, config: dict):
    """Telegram Bot API濡??뚮┝ ?꾩넚."""
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    if not token or not chat_id:
        log.debug("Telegram 誘몄꽕?? ?뚮┝ ?ㅽ궢")
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
        log.warning(f"Telegram ?꾩넚 ?ㅽ뙣: {e}")
        return False


# ============================================================
# Safety Gate (泥댁씤 怨듯넻)
# ============================================================

def passes_safety_gate(token_dict: dict, chain: str) -> tuple[bool, str]:
    """
    吏꾩엯 ???덉쟾??泥댄겕. 泥댁씤蹂꾨줈 ?꾧퀎媛??ㅻ? ???덉쓬.
    """
    eth_filter = get_eth_macro_filter()
    blocked, reason = eth_filter.should_block_entry()
    if blocked:
        return False, reason

    symbol = token_dict.get("symbol", "?")

    # sweet_spot ??: ?? ?? 15? ?? ?? ?? (falling knife)
    if token_dict.get("entry_path") == "sweet_spot":
        threshold = float(os.getenv("SWEET_SPOT_MAX_15M_DROP_PCT", "-2.5"))
        if "price_change_15m_pct" not in token_dict:
            log.warning(f"[gate] price_change_15m_pct missing for {symbol}")
        change_15m = token_dict.get("price_change_15m_pct", 0) or 0
        if change_15m < threshold:
            return False, f"sweet_spot_falling_15m ({change_15m:+.2f}% < {threshold:+.2f}%)"

    liq = token_dict.get("liquidity_usd", 0)

    # 理쒖냼 ?좊룞??
    min_liq = {"bsc": 100_000, "solana": 50_000, "base": 150_000}
    if liq < min_liq.get(chain, 100_000):
        log.debug(f"[{chain}] GATE FAIL {symbol}: liq=${liq/1000:.0f}K < ${min_liq.get(chain)/1000:.0f}K")
        return False, "low_liquidity"

    # Liquidity / MCap 鍮꾩쑉 (MCap ?????덉쓣 ?뚮쭔)
    mcap = token_dict.get("market_cap_usd")
    if mcap and mcap > 0:
        ratio = liq / mcap
        if ratio < 0.03:
            log.debug(f"[{chain}] GATE FAIL {symbol}: liq/mcap={ratio:.2%} (<3%)")
            return False, "low_liq_ratio"

    # 理쒖냼 嫄곕옒 嫄댁닔
    if token_dict.get("txns_1h", 0) < 20:
        log.debug(f"[{chain}] GATE FAIL {symbol}: txns={token_dict.get('txns_1h')} (<20)")
        return False, "low_tx_count"

    # 洹밸떒??媛寃?蹂??
    if token_dict.get("price_change_1h_pct", 0) > 50:
        log.debug(f"[{chain}] GATE FAIL {symbol}: 1h={token_dict.get('price_change_1h_pct'):+.1f}% (>50%)")
        return False, "already_pumped"

    # Audit flags
    if token_dict.get("audit_flags") and len(token_dict["audit_flags"]) > 0:
        log.debug(f"[{chain}] GATE FAIL {symbol}: audit_flags={token_dict['audit_flags']}")
        return False, f"audit_flags: {token_dict['audit_flags']}"

    return True, "ok"


# ============================================================
# Entry Signal (?ш????붿쭊 placeholder)
# ============================================================

def detect_entry_signals(snapshots: list, chain: str) -> list[dict]:
    """
    ??? ?? ?? ??.
    - BSC: ?? reflexivity momentum ?? ??
    - Solana: volume spike ?? ?? ??
    """
    candidates = []
    solana_volume_spike_ratio = float(os.getenv("SOLANA_VOLUME_SPIKE_RATIO", "3.0"))
    max_price_change_24h_pct = float(os.getenv("MAX_PRICE_CHANGE_24H_PCT", "500"))
    bsc_min_1h = float(os.getenv("BSC_REFLEXIVITY_MIN_1H_PCT", "8"))
    bsc_max_1h = float(os.getenv("BSC_REFLEXIVITY_MAX_1H_PCT", "20"))
    bsc_min_avg_tx = float(os.getenv("BSC_MIN_AVG_TX_USD", "150"))
    bsc_min_txns = int(os.getenv("BSC_MIN_TXNS_1H", "50"))
    base_min_txns = int(os.getenv("BASE_MIN_TXNS_1H", str(bsc_min_txns)))
    solana_min_avg_tx = float(os.getenv("SOLANA_MIN_AVG_TX_USD", "100"))
    solana_min_1h = float(os.getenv("SOLANA_REFLEXIVITY_MIN_1H_PCT", "8"))
    solana_max_1h = float(os.getenv("SOLANA_REFLEXIVITY_MAX_1H_PCT", "20"))
    solana_min_txns = int(os.getenv("SOLANA_MIN_TXNS_1H", "100"))
    historical_rows = _load_prev_snapshot_candidates(chain)
    for snap in snapshots:
        symbol = snap.get("symbol", "?")
        if str(symbol or "").upper() in MAJOR_TOKEN_DENYLIST:
            log.debug(f"[{chain}] skip denylisted major token: {symbol}")
            continue
        price_chg = float(snap.get("price_change_1h_pct", 0) or 0)
        price_change_24h_pct = float(snap.get("price_change_24h_pct", 0) or 0)
        txns = int(snap.get("txns_1h", 0) or 0)
        vol_1h = float(snap.get("volume_1h_usd", 0) or 0)
        avg_tx = vol_1h / max(txns, 1)
        avg_tx_size_usd = float(snap.get("avg_tx_size_usd", avg_tx) or avg_tx)
        prev_snapshot = _find_prev_snapshot(snap, historical_rows)
        skip_reason = None

        if chain == "solana":
            vol_24h = float(snap.get("volume_24h_usd", 0) or 0)
            avg_hourly_vol = vol_24h / 24 if vol_24h > 0 else 0.0
            volume_ratio = vol_1h / avg_hourly_vol if avg_hourly_vol > 0 else 0.0
            snap["volume_spike_ratio"] = volume_ratio
            if not (solana_min_1h <= price_chg <= solana_max_1h):
                skip_reason = f"1h_change out of [{solana_min_1h:.0f},{solana_max_1h:.0f}]"
                log.debug(f"[{chain}] SKIP {symbol}: 1h={price_chg:+.2f}% (expected {solana_min_1h:.0f}~{solana_max_1h:.0f}%)")
            elif price_change_24h_pct > max_price_change_24h_pct:
                skip_reason = f"price_change_24h>{max_price_change_24h_pct:.0f}"
                log.info(
                    f"[{chain}] SKIP {symbol}: price_change_24h={price_change_24h_pct:.1f}% "
                    f"> {max_price_change_24h_pct:.0f} (late-stage pump)"
                )
            elif avg_tx_size_usd < solana_min_avg_tx:
                skip_reason = f"avg_tx_size<${solana_min_avg_tx:.0f}"
                log.info(f"[{chain}] SKIP {symbol}: avg_tx_size=${avg_tx_size_usd:.0f} < ${solana_min_avg_tx:.0f}")
            elif txns < solana_min_txns:
                skip_reason = f"txns_1h<{solana_min_txns}"
                log.info(
                    f"[{chain}] SKIP {symbol}: txns_1h={txns} < {solana_min_txns} "
                    "(reflexivity passed but low activity)"
                )
                _append_score_shadow_snapshot(
                    snap,
                    chain,
                    reflexivity_passed=True,
                    skip_reason=skip_reason,
                    prev_snapshot=prev_snapshot,
                )
                continue
            elif avg_hourly_vol <= 0:
                skip_reason = "missing volume_24h data"
                log.debug(f"[{chain}] SKIP {symbol}: missing volume_24h data")
            elif volume_ratio < solana_volume_spike_ratio:
                skip_reason = f"volume_ratio<{solana_volume_spike_ratio:.1f}x"
                log.debug(
                    f"[{chain}] SKIP {symbol}: volume_ratio={volume_ratio:.2f} "
                    f"(<{solana_volume_spike_ratio:.1f}x)"
                )

            if skip_reason:
                _append_score_shadow_snapshot(
                    snap,
                    chain,
                    reflexivity_passed=False,
                    skip_reason=skip_reason,
                    prev_snapshot=prev_snapshot,
                )
                continue
            _append_score_shadow_snapshot(
                snap,
                chain,
                reflexivity_passed=True,
                skip_reason=None,
                prev_snapshot=prev_snapshot,
            )
            candidates.append(snap)
            continue

        if not (bsc_min_1h <= price_chg <= bsc_max_1h):
            skip_reason = f"1h_change out of [{bsc_min_1h:.0f},{bsc_max_1h:.0f}]"
            log.debug(f"[{chain}] SKIP {symbol}: 1h_change={price_chg:.2f}% out of [{bsc_min_1h:.0f},{bsc_max_1h:.0f}]")
        elif price_change_24h_pct > max_price_change_24h_pct:
            skip_reason = f"price_change_24h>{max_price_change_24h_pct:.0f}"
            log.debug(
                f"[{chain}] SKIP {symbol}: price_change_24h={price_change_24h_pct:.1f}% "
                f"> {max_price_change_24h_pct:.0f} (late-stage pump)"
            )
        elif avg_tx_size_usd < bsc_min_avg_tx:
            skip_reason = f"avg_tx_size<${bsc_min_avg_tx:.0f}"
            log.debug(f"[{chain}] SKIP {symbol}: avg_tx_size=${avg_tx_size_usd:.0f} < ${bsc_min_avg_tx:.0f}")
        else:
            min_txns = base_min_txns if chain == "base" else bsc_min_txns
            if txns < min_txns:
                skip_reason = f"txns_1h<{min_txns}"
                log.debug(
                    f"[{chain}] SKIP {symbol}: txns_1h={txns} < {min_txns} "
                    "(reflexivity passed but low activity)"
                )
                _append_score_shadow_snapshot(
                    snap,
                    chain,
                    reflexivity_passed=True,
                    skip_reason=skip_reason,
                    prev_snapshot=prev_snapshot,
                )
                continue

        if skip_reason:
            _append_score_shadow_snapshot(
                snap,
                chain,
                reflexivity_passed=False,
                skip_reason=skip_reason,
                prev_snapshot=prev_snapshot,
            )
            continue
        _append_score_shadow_snapshot(
            snap,
            chain,
            reflexivity_passed=True,
            skip_reason=None,
            prev_snapshot=prev_snapshot,
        )
        candidates.append(snap)

    if candidates:
        log.info(f"[{chain}] {len(snapshots)} snapshots scanned, {len(candidates)} entry candidates:")
        for c in candidates:
            txns = int(c.get("txns_1h", 0) or 0)
            vol_1h = float(c.get("volume_1h_usd", 0) or 0)
            avg_tx = vol_1h / max(txns, 1)
            liq = float(c.get("liquidity_usd", 0) or 0)
            message = (
                f"  * CANDIDATE: {c.get('symbol', '?')}\n"
                f"    price:      ${c.get('price_usd', 0):.6f}\n"
                f"    change_1h:  {c.get('price_change_1h_pct', 0):+.2f}%\n"
                f"    liquidity:  ${liq/1000:.0f}K\n"
                f"    volume_1h:  ${vol_1h/1000:.0f}K\n"
                f"    txns_1h:    {txns}\n"
                f"    avg_tx:     ${avg_tx:.0f}"
            )
            if chain == "solana":
                vol_24h = float(c.get("volume_24h_usd", 0) or 0)
                message += (
                    f"\n    volume_24h: ${vol_24h/1000:.0f}K"
                    f"\n    vol_ratio:  {float(c.get('volume_spike_ratio', 0) or 0):.2f}x"
                )
            log.info(message)
    else:
        log.info(f"[{chain}] {len(snapshots)} snapshots scanned, 0 entry candidates")

    return candidates


# ============================================================
# 硫붿씤 ?ъ씠??
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
        base_cap_raw = os.getenv("BASE_MAX_OPEN_POSITIONS", "").strip()
        if base_cap_raw and "base" in runtime_chain_configs:
            try:
                base_cap = int(base_cap_raw)
                if base_cap > 0:
                    runtime_chain_configs["base"] = ChainConfig(
                        **{
                            **asdict(runtime_chain_configs["base"]),
                            "max_concurrent_positions": base_cap,
                        }
                    )
            except ValueError:
                log.warning("BASE_MAX_OPEN_POSITIONS invalid: %s", base_cap_raw)
        self.pm = MultichainPositionManager(
            runtime_chain_configs, pf_cfg,
            storage_path=str(self.data_dir / "positions.jsonl"),
            state_path=str(self.data_dir / "portfolio_state.json"),
            mode=("dry_run" if self.dry_run else self.trading_mode),
        )
        for chain_name, cfg in self.pm.chain_configs.items():
            log.info(
                "[%s] exit config: SL=%s, TP=%s, trail_act=%s, trail_stop=%s, max_hold=%sh",
                chain_name,
                cfg.stop_loss_pct,
                cfg.take_profit_pct,
                cfg.trailing_activation_pct,
                cfg.trailing_stop_pct,
                cfg.max_hold_hours,
            )

        self.last_scrape: dict[str, float] = {"bsc": 0, "solana": 0, "base": 0}
        self.last_summary_bucket: str | None = None
        self.last_dashboard_bucket: str | None = None
        self.last_wallet_reconciliation_bucket: str | None = None
        self.mock_signal_used = False
        if self.dry_run:
            log.info("[DRY-RUN] ?ъ???吏꾩엯/泥?궛 ?뚮┝ 鍮꾪솢?깊솕")

        self.score_shadow_enabled = os.getenv("SCORE_SHADOW_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
        log.info(
            "[gate] sweet_spot falling-knife filter loaded: threshold=%+.2f%%",
            float(os.getenv("SWEET_SPOT_MAX_15M_DROP_PCT", "-2.5")),
        )
        shadow_enabled = os.getenv("SHADOW_ENABLED", "1") == "1"
        if shadow_enabled:
            log.info("[shadow] enabled, 4 shadow rules loaded: disable_gz, falling_3pct, falling_2pct, combo")
        else:
            log.info("[shadow] disabled")
        self.score_shadow_path = self.data_dir / "score_shadow.jsonl"
        self.score_shadow_path.touch(exist_ok=True)
        self.score_weights = {
            "momentum": float(os.getenv("SCORE_W_MOMENTUM", "0.30")),
            "tx_quality": float(os.getenv("SCORE_W_TX_QUALITY", "0.25")),
            "liquidity": float(os.getenv("SCORE_W_LIQUIDITY", "0.20")),
            "buy_pressure": float(os.getenv("SCORE_W_BUY_PRESSURE", "0.25")),
            "penalty_overheated": float(os.getenv("SCORE_PENALTY_OVERHEATED", "0.40")),
        }
        self.score_entry_threshold = float(os.getenv("SCORE_ENTRY_THRESHOLD", "0.65"))

        self._log_runtime_position_params()
        if self.trading_mode in {"dry_run", "live"}:
            self._init_trade_executor()
    def _log_runtime_position_params(self):
        for chain in ("bsc", "solana"):
            cfg = self.pm.chain_configs.get(chain)
            if not cfg:
                continue
            log.info(
                "%s params: stop_loss=%s trailing_activation=%s trailing_stop=%s take_profit=%s max_hold_hours=%s capital_per_position_pct=%s",
                chain,
                cfg.stop_loss_pct,
                cfg.trailing_activation_pct,
                cfg.trailing_stop_pct,
                cfg.take_profit_pct,
                cfg.max_hold_hours,
                cfg.capital_per_position_pct,
            )

    def _append_score_shadow_record(self, snapshot: dict, chain: str, *, entry_taken: bool, entry_status: str | None = None, entry_reason: str | None = None) -> None:
        if not self.score_shadow_enabled:
            return
        # buys_1h / sells_1h now persisted via gecko_client; setdefault is a fallback for legacy snapshots.
        enriched_snapshot = dict(snapshot)
        enriched_snapshot.setdefault("buys_1h", 0)
        enriched_snapshot.setdefault("sells_1h", 0)
        breakdown = evaluate_candidate(enriched_snapshot, self.score_weights, self.score_entry_threshold)
        # === SHADOW VALIDATION (5/14) ===
        # Hypothesis: final_score [0.15, 0.25) on Base = golden zone
        # Based on 780-record analysis: 98-100% win rate in this range
        # Started: 2026-05-14
        # Validation period: 7 days minimum
        # DO NOT use this for entry decisions yet - validation only
        _final_score = float(breakdown.final_score or 0)
        _chain = str(chain or "").lower()
        special_gate_candidate = 0.15 <= _final_score < 0.25 and _chain == "base"
        special_gate_reason = (
            "final_score_0.15_0.25_base"
            if special_gate_candidate
            else f"final_score={_final_score:.4f}_chain={_chain}"
        )
        record = {
            "timestamp": iso_utc_now(),
            "chain": chain,
            "symbol": enriched_snapshot.get("symbol", "?"),
            "token_address": enriched_snapshot.get("contract_address"),
            "entry_path": enriched_snapshot.get("entry_path") or "reflexivity",
            "reflexivity_passed": True,
            "price_change_5m_pct": float(enriched_snapshot.get("price_change_5m_pct", 0) or 0),
            "price_change_15m_pct": float(enriched_snapshot.get("price_change_15m_pct", 0) or 0),
            "price_change_1h_pct": float(enriched_snapshot.get("price_change_1h_pct", 0) or 0),
            "price_change_24h_pct": float(enriched_snapshot.get("price_change_24h_pct", 0) or 0),
            "pool_age_hours": enriched_snapshot.get("pool_age_hours"),
            "avg_tx_size_usd": float(enriched_snapshot.get("avg_tx_size_usd", 0) or 0),
            "liquidity_usd": float(enriched_snapshot.get("liquidity_usd", 0) or 0),
            "buys_1h": int(enriched_snapshot.get("buys_1h", 0) or 0),
            "sells_1h": int(enriched_snapshot.get("sells_1h", 0) or 0),
            "special_gate_candidate": special_gate_candidate,
            "special_gate_reason": special_gate_reason,
            **asdict(breakdown),
            "entry_taken": bool(entry_taken),
            "entry_status": entry_status,
            "entry_reason": entry_reason,
        }
        with self.score_shadow_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _score_shadow_result_taken(self, result: dict | None) -> bool:
        if not result:
            return False
        return str(result.get("status", "")).lower() in {"queued", "executed", "opened", "submitted"}

    def _write_heartbeat(self, stage: str, **extra):
        payload = {
            "timestamp": iso_utc_now(),
            "stage": stage,
            "trading_mode": self.trading_mode,
            "dry_run": self.dry_run,
        }
        payload.update(extra)
        _atomic_write_text(
            self.heartbeat_path,
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _init_trade_executor(self):
        trading_cfg = self.config["trading"]
        safety = SafetyCircuitBreaker(
            self.data_dir / "trading_safety_state.json",
            mode=self.trading_mode,
            daily_loss_limit_usd=trading_cfg["daily_loss_limit_usd"],
            max_consecutive_losses=trading_cfg["max_consecutive_losses"],
            max_position_size_usd=trading_cfg["max_position_size_usd"],
            max_daily_trades=trading_cfg["max_daily_trades"],
            halt_duration_hours_first=trading_cfg["halt_duration_hours_first"],
            halt_duration_hours_second=trading_cfg["halt_duration_hours_second"],
            halt_duration_hours_third=trading_cfg["halt_duration_hours_third"],
            loss_window_hours=trading_cfg["loss_window_hours"],
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
            close_handler=self._manual_close,
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

    def _manual_close(self, symbol: str) -> str:
        if not self.executor:
            return "trade executor is not active"
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return "usage: /close <symbol>"
        matches = [pos for pos in self.pm.positions.values() if pos.symbol.upper() == symbol]
        if not matches:
            return f"open position not found: {symbol}"
        result = self.executor.handle_exit_signal(matches[0], ManualExitReason.MANUAL_STOP)
        if result.get("status") == "closed":
            return f"manual close sent: {symbol} tx={result.get('tx_hash')}"
        return f"manual close failed: {symbol} reason={result.get('reason')}"

    # --------------------------------------------------------
    # 泥댁씤蹂??ㅽ겕?섑븨
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
            "timestamp": iso_utc_now(),
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
        capped_size_usd = self._resolved_position_size_usd(chain)
        return {
            **snapshot,
            "chain": chain,
            "entry_path": snapshot.get("entry_path") or "reflexivity",
            "position_size_usd": capped_size_usd,
            "stop_loss_pct": cfg.stop_loss_pct,
            "take_profit_pct": cfg.take_profit_pct,
            "max_hold_hours": cfg.max_hold_hours,
            "profit_lock": True,
            "approval_timeout": self.config["trading"]["approval_timeout_sec"],
            "total_capital_usd": self.config["total_capital_usd"],
        }

    def _compute_snapshot_final_score(self, snapshot: dict, chain: str, historical_rows: dict[str, list[dict]] | None = None) -> float:
        rows = historical_rows if historical_rows is not None else _load_prev_snapshot_candidates(chain)
        prev_snapshot = _find_prev_snapshot(snapshot, rows)
        enriched_snapshot = dict(snapshot)
        enriched_snapshot.setdefault("buys_1h", 0)
        enriched_snapshot.setdefault("sells_1h", 0)
        multi = evaluate_multi(
            enriched_snapshot,
            prev_snapshot,
            _score_shadow_weights(),
            _score_shadow_threshold(),
        )
        return float(multi.score_v1_reflexivity.final_score or 0.0)

    def _passes_golden_zone_gate(self, cand: dict, chain: str) -> tuple[bool, str]:
        """Alternative entry path: golden zone + safety filter."""
        if chain != "base":
            return False, "chain_not_base"

        final_score = float(cand.get("final_score", 0) or 0)
        if not (0.15 <= final_score < 0.25):
            return False, f"final_score_{final_score:.4f}_out_of_golden_zone"

        pool_age = cand.get("pool_age_hours")
        if pool_age is None or float(pool_age) < 168:
            return False, f"pool_age_{pool_age}_lt_168h"

        liquidity = float(cand.get("liquidity_usd", 0) or 0)
        if liquidity < 500000:
            return False, f"liquidity_{liquidity:.0f}_lt_500K"

        h24_change = float(cand.get("price_change_24h_pct", 0) or 0)
        if abs(h24_change) > 100:
            return False, f"h24_{h24_change:.1f}_extreme"

        golden_min_txns = int(os.getenv("GOLDEN_ZONE_MIN_TXNS_1H", "30"))
        txns_1h = int(cand.get("txns_1h", 0) or 0)
        if txns_1h < golden_min_txns:
            return False, f"txns_1h_{txns_1h}_lt_{golden_min_txns}_golden"

        return True, "golden_zone_safe"

    def _passes_sweet_spot_gate(self, cand: dict, chain: str) -> tuple[bool, str]:
        """Alternative entry path: sweet spot + safety filter."""
        enabled = os.getenv("ENABLE_SWEET_SPOT_PATH", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return False, "sweet_spot_disabled"
        if chain != "base":
            return False, "chain_not_base"

        raw_signals = cand.get("raw_signals") or {}
        txns_1h = int(raw_signals.get("txns_per_hour", cand.get("txns_1h", 0)) or 0)
        min_txns = int(os.getenv("SWEET_SPOT_MIN_TXNS_1H", "30"))
        max_txns = int(os.getenv("SWEET_SPOT_MAX_TXNS_1H", "100"))
        if not (min_txns <= txns_1h < max_txns):
            return False, f"txns_1h_{txns_1h}_out_of_{min_txns}_{max_txns}_sweet"

        liquidity = float(cand.get("liquidity_usd", 0) or 0)
        min_liquidity = float(os.getenv("SWEET_SPOT_MIN_LIQUIDITY_USD", "300000"))
        if liquidity < min_liquidity:
            return False, f"liquidity_too_low (${liquidity:,.0f} < ${min_liquidity:,.0f})"

        pool_age = cand.get("pool_age_hours")
        min_age = float(os.getenv("SWEET_SPOT_MIN_POOL_AGE_HOURS", "24"))
        if pool_age is None or float(pool_age) < min_age:
            return False, f"pool_age_{pool_age}_lt_{min_age}h"

        return True, "sweet_spot_safe"

    def _build_golden_zone_candidates(self, snapshots: list[dict], chain: str, exclude_addresses: set[str]) -> list[dict]:
        if chain != "base":
            return []

        historical_rows = _load_prev_snapshot_candidates(chain)
        candidates: list[dict] = []
        seen_addresses = set(exclude_addresses)
        for snap in snapshots:
            symbol = snap.get("symbol", "?")
            if str(symbol or "").upper() in MAJOR_TOKEN_DENYLIST:
                continue
            contract = str(snap.get("contract_address") or "").strip().lower()
            if not contract or contract in seen_addresses:
                continue
            cand = dict(snap)
            cand["final_score"] = self._compute_snapshot_final_score(cand, chain, historical_rows)
            cand["entry_path"] = "golden_zone"
            golden_pass, golden_reason = self._passes_golden_zone_gate(cand, chain)
            if not golden_pass:
                if golden_reason.startswith("txns_1h_"):
                    log.debug(f"[{chain}] SKIP {symbol}: txns_1h={cand.get('txns_1h', 0)} < {int(os.getenv('GOLDEN_ZONE_MIN_TXNS_1H', '30'))} (golden_zone gate)")
                else:
                    log.debug(f"[{chain}] golden-zone skip {symbol}: {golden_reason}")
                continue
            log.info(
                f"[{chain}] ENTRY {symbol} via golden_zone "
                f"(score={cand.get('final_score', 0):.2f}, age={float(cand.get('pool_age_hours', 0) or 0)/24:.1f}d, "
                f"liq=${float(cand.get('liquidity_usd', 0) or 0)/1_000_000:.1f}M, txns={int(cand.get('txns_1h', 0) or 0)})"
            )
            candidates.append(cand)
            seen_addresses.add(contract)
        return candidates

    def _build_sweet_spot_candidates(self, snapshots: list[dict], chain: str, exclude_addresses: set[str]) -> list[dict]:
        if chain != "base":
            return []

        candidates: list[dict] = []
        seen_addresses = set(exclude_addresses)
        for snap in snapshots:
            symbol = snap.get("symbol", "?")
            if str(symbol or "").upper() in MAJOR_TOKEN_DENYLIST:
                continue
            contract = str(snap.get("contract_address") or "").strip().lower()
            if not contract or contract in seen_addresses:
                continue
            cand = dict(snap)
            cand["entry_path"] = "sweet_spot"
            sweet_pass, sweet_reason = self._passes_sweet_spot_gate(cand, chain)
            if not sweet_pass:
                if sweet_reason.startswith("txns_1h_"):
                    min_txns = int(os.getenv("SWEET_SPOT_MIN_TXNS_1H", "30"))
                    max_txns = int(os.getenv("SWEET_SPOT_MAX_TXNS_1H", "100"))
                    txns = int((cand.get("raw_signals") or {}).get("txns_per_hour", cand.get("txns_1h", 0)) or 0)
                    log.debug(f"[sweet_spot] SKIP chain={chain} symbol={symbol} reason=txns_out_of_range ({txns} not in [{min_txns},{max_txns}))")
                else:
                    log.debug(f"[sweet_spot] SKIP chain={chain} symbol={symbol} reason={sweet_reason}")
                continue
            txns = int((cand.get("raw_signals") or {}).get("txns_per_hour", cand.get("txns_1h", 0)) or 0)
            liq = float(cand.get("liquidity_usd", 0) or 0)
            age = float(cand.get("pool_age_hours", 0) or 0)
            log.info(f"[sweet_spot] PASS chain={chain} symbol={symbol} txns_1h={txns} liq=${liq:,.0f} age={age:.1f}h")
            candidates.append(cand)
            seen_addresses.add(contract)
        return candidates

    def scrape_chain(self, chain: str) -> list[dict]:
        """泥댁씤蹂??ㅽ겕?섑띁 ?몄텧. ?ㅽ뙣 ??留덉?留???λ낯 濡쒕뱶."""
        now = time.time()
        interval = self.config["scraper_schedule"][chain]

        if now - self.last_scrape[chain] < interval:
            log.debug(f"[{chain}] scrape interval not reached yet, using cached snapshots")
            return self.load_latest_snapshots(chain)

        log.info(f"[{chain}] scraping started...")
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

        # ???
        if tokens:
            store = SnapshotStore(str(self.data_dir / f"{chain}_snapshots.jsonl"))
            store.save(tokens)
            log.info(f"[{chain}] {len(tokens)} snapshots saved")

        if tokens:
            return [self._token_to_dict(t) for t in tokens]

        cached = self.load_latest_snapshots(chain)
        if cached:
            log.warning(f"[{chain}] scraper returned no fresh data; using cached snapshots ({len(cached)} items)")
        return cached

    def _token_to_dict(self, token) -> dict:
        """TrendingToken ??dict"""
        d = asdict(token)
        if hasattr(token, "avg_tx_size_usd"):
            d["avg_tx_size_usd"] = token.avg_tx_size_usd
        if hasattr(token, "liq_to_mcap_ratio"):
            d["liq_to_mcap_ratio"] = token.liq_to_mcap_ratio
        return d

    def load_latest_snapshots(self, chain: str) -> list[dict]:
        """留덉?留??ㅽ겕??寃곌낵 濡쒕뱶 (??꾩뒪?ы봽 湲곗? 理쒖떊留?"""
        path = self.data_dir / f"{chain}_snapshots.jsonl"
        if not path.exists():
            return []
        # 留덉?留?N嫄?濡쒕뱶 + 理쒖떊 timestamp 洹몃９留?諛섑솚
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        if not lines or lines == [""]:
            return []

        # ?ㅼ뿉????닚?쇰줈 ?쎌쑝硫?媛숈? timestamp?쇰━ 臾띔린
        records = [json.loads(ln) for ln in lines[-200:]]
        if not records:
            return []
        latest_ts = records[-1]["timestamp"]
        return [r for r in records if r["timestamp"] == latest_ts]

    # --------------------------------------------------------
    # ?ъ씠???ㅽ뻾
    # --------------------------------------------------------

    def run_cycle(self):
        cycle_started_at = utc_now()
        self._write_heartbeat("cycle_start")
        log.info("=" * 60)
        log.info("cycle started")
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
                status = result.get("status")
                reason = result.get("reason")
                if reason:
                    log.info("executor pending result: %s (%s)", status, reason)
                else:
                    log.info("executor pending result: %s", status)
            self._write_heartbeat("after_pending_entries")

        snapshots_by_chain = {}
        self._write_heartbeat("before_scrape")

        # 1. ?ㅽ겕?섑븨 (泥댁씤蹂?
        # Base disabled for new entries on 2026-04-25. Keep scraping any
        # legacy position chains so existing positions can exit naturally.
        for chain in self._chains_for_cycle():
            try:
                self._write_heartbeat("before_scrape_chain", chain=chain)
                snapshots_by_chain[chain] = self.scrape_chain(chain)
                self._write_heartbeat("after_scrape_chain", chain=chain, snapshot_count=len(snapshots_by_chain[chain]))
            except Exception as e:
                log.error(f"[{chain}] scrape failed: {e}")
                cached = self.load_latest_snapshots(chain)
                if cached:
                    log.warning(f"[{chain}] using cached snapshots after scrape failure ({len(cached)} items)")
                snapshots_by_chain[chain] = cached
                self._write_heartbeat("scrape_chain_failed", chain=chain, snapshot_count=len(cached), error=str(e))

        self._attach_missing_position_fallbacks(snapshots_by_chain)
        self._reconcile_wallet_positions(snapshots_by_chain, cycle_started_at)

        # 2. 湲곗〈 ?ъ????낅뜲?댄듃 + 泥?궛
        self._write_heartbeat("before_position_update")
        self._inject_mock_signal(snapshots_by_chain)
        exits = self.pm.update_all(snapshots_by_chain)
        self._write_heartbeat("after_position_update", exit_count=len(exits))
        for pos, reason, msg in exits:
            current_price = pos.current_price
            if self.executor:
                result = self.executor.handle_exit_signal(pos, reason)
                log.info(
                    "executor exit result: %s%s",
                    result.get("status"),
                    f" ({result.get('reason')})" if result.get("reason") else "",
                )
                continue
            if not self.dry_run:
                closed_pos = self.pm.close_position(pos, reason, msg)
                self._notify_exit(closed_pos, reason, current_price, cycle_started_at)
            else:
                log.info(f"[DRY-RUN] exit skipped: {pos.symbol} ({reason})")

        # 3. ?좉퇋 吏꾩엯 ?꾨낫 ?먯?
        self._write_heartbeat("before_entry_scan")
        for chain, snapshots in snapshots_by_chain.items():
            if chain not in ACTIVE_ENTRY_CHAINS:
                continue
            if not snapshots:
                continue
            candidates = [dict(cand, entry_path=(cand.get("entry_path") or "reflexivity")) for cand in detect_entry_signals(snapshots, chain)]
            if chain == "base":
                reflexivity_addresses = {
                    str(cand.get("contract_address") or "").strip().lower()
                    for cand in candidates
                    if str(cand.get("contract_address") or "").strip()
                }
                golden_zone_candidates = self._build_golden_zone_candidates(snapshots, chain, reflexivity_addresses)
                candidates.extend(golden_zone_candidates)
                occupied_addresses = reflexivity_addresses | {
                    str(cand.get("contract_address") or "").strip().lower()
                    for cand in golden_zone_candidates
                    if str(cand.get("contract_address") or "").strip()
                }
                candidates.extend(self._build_sweet_spot_candidates(snapshots, chain, occupied_addresses))
            if self.config["trading"]["mock_entry_signal"] and chain == self.config["trading"]["mock_entry_chain"]:
                mock_candidates = [
                    dict(snap, entry_path=(snap.get("entry_path") or "reflexivity"))
                    for snap in snapshots
                    if str(snap.get("contract_address", "")).startswith("mock-")
                ]
                candidates = mock_candidates + candidates
            open_addresses = self.pm.get_open_token_addresses(chain)
            filtered_candidates = []
            for cand in candidates:
                contract = str(cand.get("contract_address") or "").strip().lower()
                if contract and contract in open_addresses:
                    log.debug(f"[{chain}] skip already-open: {cand.get('symbol', '?')}")
                    continue
                filtered_candidates.append(cand)
            candidates = filtered_candidates
            max_signals_per_cycle = int(self.config["trading"]["max_signals_per_cycle"])
            chain_max_signals = max_signals_per_cycle
            if chain == "solana":
                chain_max_signals = int(self.config["trading"].get("solana_max_signals_per_cycle", 1) or 1)
            submitted_signals = 0
            for cand in candidates:
                ok, reason = passes_safety_gate(cand, chain)

                if os.getenv("SHADOW_ENABLED", "1") == "1":
                    try:
                        from shadow_rules import evaluate_shadow, log_shadow_decision
                        shadow_results = evaluate_shadow(cand, passes_safety_gate)
                        log_shadow_decision(cand, (ok, reason), shadow_results)
                    except Exception as e:
                        log.warning(f"[shadow] eval error (continuing): {e}")

                if not ok:
                    if str(reason).startswith("eth_4h_down") or str(reason).startswith("sweet_spot_falling"):
                        log.info(f"[{chain}] skip entry: {reason} symbol={cand.get('symbol')}")
                    else:
                        log.debug(f"[{chain}] {cand['symbol']} safety gate failed: {reason}")
                    continue
                if self.executor and chain_max_signals > 0 and submitted_signals >= chain_max_signals:
                    break
                # 진입시점 ETH 4h 레짐 태깅 (분석 전용). 게이트가 이미 받아둔 캐시
                # 스냅샷 재사용 → 추가 네트워크 호출 없음. 사후 상관분석에서 "완만한
                # 상승(up)" 레짐 거래만 따로 묶어 재검증하기 위함.
                _eth_filter = get_eth_macro_filter()
                eth_change_4h, eth_regime = _eth_filter.current_regime()
                eth_extra = {"eth_4h_pct": eth_change_4h, "eth_4h_regime": eth_regime,
                             "eth_24h_pct": _eth_filter.current_eth_24h_pct()}
                # divergence v1 태깅(Module 2): 인식(pc_1h) vs 실수요(매수압) 괴리.
                # 진입 결정엔 안 쓰고 기록만 — forward 검증 후 게이트화 결정.
                try:
                    from divergence import compute as _div_compute
                    eth_extra.update(_div_compute(cand))
                except Exception:
                    pass
                if self.executor:
                    signal = self._build_trade_signal(cand, chain)
                    result = self.executor.submit_entry_signal(signal)
                    log.info(f"executor entry result: {result['status']} reason={result.get('reason', 'N/A')} symbol={signal.get('symbol', '?')} path={signal.get('entry_path', 'reflexivity')}")
                    # entry_logger: 실제 진입(queued)일 때만 스냅샷 기록. executor 경로에선
                    # 위 continue로 아래 log_entry_snapshot에 도달하지 못해 기록이 누락되던 버그 수정.
                    if result.get("status") == "queued":
                        log_entry_snapshot(
                            cand, chain,
                            mode=("dry" if self.dry_run else "live"),
                            extra={"executor_status": result.get("status"), "signal_id": result.get("signal_id"), **eth_extra},
                        )
                    continue
                if not self.dry_run:
                    pos = self.pm.open_position(
                        chain, cand,
                        total_capital_usd=self.config["total_capital_usd"]
                    )
                    if pos:
                        submitted_signals += 1
                        self._notify_entry(cand, pos)
                        log_entry_snapshot(cand, chain, mode="live", extra=eth_extra)   # ← 추가
                else:
                    log_entry_snapshot(cand, chain, mode="dry", extra=eth_extra)        # ← 추가
                    log.info(
                        f"[DRY-RUN] entry preview: [{chain.upper()}] {cand['symbol']} "
                        f"price=${cand.get('price_usd', 0):.6f} "
                        f"1h={cand.get('price_change_1h_pct', 0):+.2f}% "
                        f"liq=${cand.get('liquidity_usd', 0)/1000:.0f}K "
                        f"txns={cand.get('txns_1h', 0)} "
                        f"path={cand.get('entry_path', 'reflexivity')}"
                    )
                    preview = format_entry_alert(
                        cand,
                        chain,
                        self._resolved_position_size_usd(chain),
                        self.pm.chain_configs[chain],
                    )
                    submitted_signals += 1
                    log.info("[DRY-RUN] Telegram ENTRY preview:\n%s", preview)

        # 4. 由ы룷??
        if self.executor:
            late_results = self.executor.process_pending_entries()
            for result in late_results:
                status = result.get("status")
                reason = result.get("reason")
                if reason:
                    log.info("executor pending result (post-entry): %s (%s)", status, reason)
                else:
                    log.info("executor pending result (post-entry): %s", status)
        try:
            process_score_outcomes(snapshots_by_chain)
        except Exception as exc:
            log.warning("score outcome processing failed: %s", exc)
        self._write_heartbeat("before_summary")
        self._maybe_send_summary(cycle_started_at)
        self._maybe_update_dashboard(cycle_started_at)
        self._write_heartbeat("cycle_complete", open_positions=len(self.pm.positions))
        log.info("\n" + self.pm.summary())
        log.info("cycle finished\n")

    def _recommended_position_size_usd(self, chain: str) -> float:
        cfg = self.pm.chain_configs[chain]
        return self.config["total_capital_usd"] * (cfg.capital_per_position_pct / 100.0)

    def _resolved_position_size_usd(self, chain: str) -> float:
        fixed_cap = self._max_position_size_usd_for_chain(chain)
        if fixed_cap > 0:
            return fixed_cap
        return self._recommended_position_size_usd(chain)

    def _max_position_size_usd_for_chain(self, chain: str) -> float:
        trading_cfg = self.config["trading"]
        default_cap = float(trading_cfg["max_position_size_usd"])
        if chain == "bsc":
            return float(trading_cfg.get("bsc_position_size_usd", default_cap) or default_cap)
        if chain == "solana":
            return float(trading_cfg.get("solana_position_size_usd", default_cap) or default_cap)
        if chain == "base":
            return float(trading_cfg.get("base_position_size_usd", trading_cfg.get("bsc_position_size_usd", default_cap)) or trading_cfg.get("bsc_position_size_usd", default_cap) or default_cap)
        return default_cap
        return default_cap

    def _attach_missing_position_fallbacks(self, snapshots_by_chain: dict[str, list[dict]]) -> None:
        if not self.executor:
            return
        for position in list(self.pm.positions.values()):
            chain_snaps = snapshots_by_chain.setdefault(position.chain, [])
            if any(snap.get("contract_address") == position.contract_address for snap in chain_snaps):
                continue
            fallback = self.executor.build_market_snapshot(position)
            if not fallback:
                continue
            chain_snaps.append(fallback)
            log.info(
                "attached fallback market snapshot: [%s] %s price=$%.6f",
                position.chain,
                position.symbol,
                float(fallback.get("price_usd", 0.0)),
            )

    def _reconcile_wallet_positions(self, snapshots_by_chain: dict[str, list[dict]], cycle_started_at: datetime) -> None:
        if not self.executor or self.trading_mode != "live":
            return

        interval_minutes = int(self.config["trading"].get("wallet_reconciliation_interval_minutes", 10) or 10)
        grace_minutes = int(self.config["trading"].get("wallet_reconciliation_grace_minutes", 10) or 10)
        mismatch_pct = float(self.config["trading"].get("wallet_reconciliation_mismatch_pct", 5.0) or 5.0)
        if interval_minutes <= 0:
            return

        total_minutes = cycle_started_at.hour * 60 + cycle_started_at.minute
        if total_minutes % interval_minutes != 0:
            return

        bucket = cycle_started_at.strftime("%Y-%m-%d %H:%M")
        if bucket == self.last_wallet_reconciliation_bucket:
            return
        self.last_wallet_reconciliation_bucket = bucket

        seen_keys: set[str] = set()
        for position in list(self.pm.positions.values()):
            seen_keys.add(f"{position.chain}:{position.contract_address}")
            try:
                age_minutes = (cycle_started_at - parse_iso_utc(position.entry_timestamp)).total_seconds() / 60.0
            except Exception:
                age_minutes = grace_minutes + 1
            if age_minutes < grace_minutes:
                continue

            ok, wallet_balance = self.executor._try_get_wallet_token_balance(position.chain, position.contract_address)
            if not ok:
                continue

            ledger_amount = float(getattr(position, "token_amount", 0.0) or 0.0)
            if ledger_amount <= 0:
                ledger_amount = float(self.executor._estimate_token_amount(position) or 0.0)

            if wallet_balance <= 1e-12:
                self.executor.clear_wallet_mismatch(position.chain, position.contract_address)
                chain_snaps = snapshots_by_chain.get(position.chain, [])
                snap = next((s for s in chain_snaps if s.get("contract_address") == position.contract_address), None)
                if snap:
                    position.current_price = float(snap.get("price_usd") or position.current_price)
                    position.current_liquidity = float(snap.get("liquidity_usd") or position.current_liquidity)
                    position.current_b_holders = int(snap.get("holders_binance") or position.current_b_holders)
                position.last_update = iso_utc_now()

                closed_pos = self.pm.close_position(
                    position,
                    ExitReason.MANUAL,
                    "wallet_reconciliation_zero_balance",
                )
                self.executor.safety.record_trade_closed(
                    float(closed_pos.realized_pnl_usd or 0.0),
                    mode_at_close=self.trading_mode,
                )
                log.warning(
                    "wallet reconciliation closed stale position: [%s] %s balance=0 age=%.1fm",
                    position.chain,
                    position.symbol,
                    age_minutes,
                )
                self._notify_exit(
                    closed_pos,
                    ExitReason.MANUAL,
                    closed_pos.exit_price or closed_pos.current_price,
                    cycle_started_at,
                )
                continue

            tolerance = max(ledger_amount * (mismatch_pct / 100.0), 1e-6)
            if wallet_balance > ledger_amount + tolerance:
                self.executor.set_wallet_mismatch(
                    position.chain,
                    position.contract_address,
                    {
                        "type": "wallet_gt_ledger",
                        "symbol": position.symbol,
                        "wallet_token_amount": wallet_balance,
                        "ledger_token_amount": ledger_amount,
                        "position_size_usd": float(position.size_usd or 0.0),
                        "entry_timestamp": position.entry_timestamp,
                    },
                )
                log.warning(
                    "wallet reconciliation mismatch: [%s] %s wallet=%.6f ledger=%.6f",
                    position.chain,
                    position.symbol,
                    wallet_balance,
                    ledger_amount,
                )
            else:
                self.executor.clear_wallet_mismatch(position.chain, position.contract_address)

        try:
            latest_rows: dict[str, dict] = {}
            with self.pm.storage.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    chain = str(row.get("chain", "")).lower()
                    contract = str(row.get("contract_address", ""))
                    if chain not in {"bsc", "solana"} or not contract:
                        continue
                    if chain == "bsc" and not (contract.startswith("0x") and len(contract) == 42):
                        continue
                    if chain == "solana" and len(contract) < 20:
                        continue
                    latest_rows[f"{chain}:{contract}"] = row
        except Exception as exc:
            log.warning("wallet reconciliation latest-row scan failed: %s", exc)
            return

        for key, row in latest_rows.items():
            if key in seen_keys or not row.get("is_closed"):
                continue
            try:
                age_minutes = (cycle_started_at - parse_iso_utc(row.get("entry_timestamp", cycle_started_at.isoformat()))).total_seconds() / 60.0
            except Exception:
                age_minutes = grace_minutes + 1
            if age_minutes < grace_minutes:
                continue
            ok, wallet_balance = self.executor._try_get_wallet_token_balance(row.get("chain"), row.get("contract_address"))
            if not ok:
                continue
            if wallet_balance <= 1e-12:
                self.executor.clear_wallet_mismatch(row.get("chain"), row.get("contract_address"))
                continue
            self.executor.set_wallet_mismatch(
                row.get("chain"),
                row.get("contract_address"),
                {
                    "type": "wallet_only",
                    "symbol": row.get("symbol", "?"),
                    "wallet_token_amount": wallet_balance,
                    "ledger_token_amount": 0.0,
                    "position_size_usd": float(row.get("size_usd") or 0.0),
                    "entry_timestamp": row.get("entry_timestamp"),
                    "last_exit_reason": row.get("exit_reason"),
                },
            )
            log.warning(
                "wallet reconciliation found wallet-only holding: [%s] %s wallet=%.6f",
                row.get("chain"),
                row.get("symbol", "?"),
                wallet_balance,
            )

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
                    exit_ts = parse_iso_utc(record["exit_timestamp"])
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

    def _maybe_update_dashboard(self, as_of: datetime):
        dashboard_cfg = self.config.get("dashboard", {})
        if not dashboard_cfg.get("enabled", True):
            return

        interval = int(dashboard_cfg.get("update_interval_minutes", 10) or 10)
        if interval <= 0:
            return

        total_minutes = as_of.hour * 60 + as_of.minute
        if total_minutes % interval != 0:
            return

        bucket = as_of.strftime("%Y-%m-%d %H:%M")
        if bucket == self.last_dashboard_bucket:
            return

        try:
            from tools.build_dashboard import OUTPUT_FILE, render_dashboard

            html = render_dashboard()
            OUTPUT_FILE.write_text(html, encoding="utf-8")
            self.last_dashboard_bucket = bucket
            log.info("dashboard updated: %s", OUTPUT_FILE)
        except Exception as exc:
            log.warning("dashboard update failed: %s", exc)


# ============================================================
# CLI 吏꾩엯??
# ============================================================

def main():
    import argparse, os, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    acquire_single_instance_lock()
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
    ap.add_argument("--once", action="store_true", help="Run one cycle and exit")
    ap.add_argument("--interval", type=int, default=600, help="Loop interval in seconds (default 10 minutes)")
    ap.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Override total_capital_usd. If omitted, uses TOTAL_CAPITAL_USD env or CONFIG default.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Run without live entry execution")
    ap.add_argument("--verbose", action="store_true", help="Enable DEBUG logging (includes reject reasons)")
    args = ap.parse_args()

    # ?섍꼍蹂?섏뿉??API ??濡쒕뱶
    CONFIG["api_keys"]["bscscan"] = os.getenv("BSCSCAN_API_KEY")
    CONFIG["api_keys"]["basescan"] = os.getenv("BASESCAN_API_KEY")  # ?놁쑝硫?None, ???議고쉶 ?ㅽ궢
    CONFIG["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
    CONFIG["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID")
    CONFIG["telegram"]["trading_bot_token"] = os.getenv("TELEGRAM_TRADING_BOT_TOKEN")
    CONFIG["telegram"]["trading_chat_id"] = os.getenv("TELEGRAM_TRADING_CHAT_ID")
    CONFIG["telegram"]["alert_entry"] = os.getenv("ALERT_ENTRY", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["telegram"]["alert_exit"] = os.getenv("ALERT_EXIT", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["telegram"]["alert_summary_interval"] = int(os.getenv("ALERT_SUMMARY_INTERVAL", "4"))
    CONFIG["dashboard"]["enabled"] = os.getenv("DASHBOARD_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["dashboard"]["update_interval_minutes"] = int(os.getenv("DASHBOARD_UPDATE_INTERVAL_MINUTES", "10"))
    CONFIG["trading"]["solana_max_signals_per_cycle"] = int(os.getenv("SOLANA_MAX_SIGNALS_PER_CYCLE", "1"))
    CONFIG["trading"]["bsc_position_size_usd"] = float(os.getenv("BSC_POSITION_SIZE_USD", os.getenv("MAX_POSITION_SIZE_USD", "100")))
    CONFIG["trading"]["solana_position_size_usd"] = float(os.getenv("SOLANA_POSITION_SIZE_USD", os.getenv("MAX_POSITION_SIZE_USD", "100")))
    CONFIG["trading"]["base_position_size_usd"] = float(os.getenv("BASE_POSITION_SIZE_USD", os.getenv("BSC_POSITION_SIZE_USD", os.getenv("MAX_POSITION_SIZE_USD", "100"))))
    CONFIG["trading"]["wallet_reconciliation_interval_minutes"] = int(os.getenv("WALLET_RECONCILIATION_INTERVAL_MINUTES", "10"))
    CONFIG["trading"]["wallet_reconciliation_grace_minutes"] = int(os.getenv("WALLET_RECONCILIATION_GRACE_MINUTES", "10"))
    CONFIG["trading"]["wallet_reconciliation_mismatch_pct"] = float(os.getenv("WALLET_RECONCILIATION_MISMATCH_PCT", "5"))
    CONFIG["trading"]["mode"] = os.getenv("TRADING_MODE", "").strip().lower() or "disabled"
    CONFIG["trading"]["auto_approve_on_timeout"] = os.getenv("AUTO_APPROVE_ON_TIMEOUT", "true").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["trading"]["daily_loss_limit_usd"] = float(os.getenv("DAILY_LOSS_LIMIT_USD", "100"))
    CONFIG["trading"]["max_consecutive_losses"] = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "8"))
    CONFIG["trading"]["max_position_size_usd"] = float(os.getenv("MAX_POSITION_SIZE_USD", "100"))
    CONFIG["trading"]["max_daily_trades"] = int(os.getenv("MAX_DAILY_TRADES", "30"))
    CONFIG["trading"]["halt_duration_hours_first"] = int(os.getenv("HALT_DURATION_HOURS_FIRST", "1"))
    CONFIG["trading"]["halt_duration_hours_second"] = int(os.getenv("HALT_DURATION_HOURS_SECOND", "4"))
    CONFIG["trading"]["halt_duration_hours_third"] = int(os.getenv("HALT_DURATION_HOURS_THIRD", "24"))
    CONFIG["trading"]["loss_window_hours"] = float(os.getenv("LOSS_WINDOW_HOURS", "1"))
    CONFIG["trading"]["mock_entry_signal"] = os.getenv("MOCK_ENTRY_SIGNAL", "false").strip().lower() in {"1", "true", "yes", "on"}
    CONFIG["trading"]["mock_entry_chain"] = os.getenv("MOCK_ENTRY_CHAIN", "bsc").strip().lower()
    CONFIG["trading"]["approval_timeout_sec"] = int(os.getenv("APPROVAL_TIMEOUT_SEC", "300"))
    CONFIG["trading"]["max_signals_per_cycle"] = int(os.getenv("MAX_SIGNALS_PER_CYCLE", "1"))
    env_total_capital = os.getenv("TOTAL_CAPITAL_USD", "").strip()
    if env_total_capital:
        try:
            CONFIG["total_capital_usd"] = float(env_total_capital)
        except ValueError:
            log.warning("TOTAL_CAPITAL_USD invalid for orchestrator config: %s", env_total_capital)

    active_entry_chains_raw = os.getenv("ACTIVE_ENTRY_CHAINS", ",".join(ACTIVE_ENTRY_CHAINS)).strip()
    if active_entry_chains_raw:
        parsed_active_entry_chains = tuple(chain.strip().lower() for chain in active_entry_chains_raw.split(",") if chain.strip())
        if parsed_active_entry_chains:
            globals()["ACTIVE_ENTRY_CHAINS"] = parsed_active_entry_chains
    if args.capital is not None:
        CONFIG["total_capital_usd"] = args.capital
        log.info(f"total_capital_usd overridden by --capital: ${args.capital:.2f}")
    else:
        log.info(f"total_capital_usd from env/default: ${CONFIG['total_capital_usd']:.2f}")
    CONFIG["chain_allocation"] = {
        "bsc": float(os.getenv("CHAIN_ALLOCATION_BSC", "0.55")),
        "solana": float(os.getenv("CHAIN_ALLOCATION_SOLANA", "0.45")),
        "base": float(os.getenv("CHAIN_ALLOCATION_BASE", "0.00")),
    }
    CONFIG["scraper_schedule"]["bsc"] = int(os.getenv("SCRAPE_INTERVAL_BSC", str(CONFIG["scraper_schedule"]["bsc"])))
    CONFIG["scraper_schedule"]["solana"] = int(os.getenv("SCRAPE_INTERVAL_SOLANA", str(CONFIG["scraper_schedule"]["solana"])))
    CONFIG["scraper_schedule"]["base"] = int(os.getenv("SCRAPE_INTERVAL_BASE", str(CONFIG["scraper_schedule"]["base"])))
    CONFIG["scraper_enrich_holders"] = os.getenv("ENRICH_HOLDERS", "false").strip().lower() in {"1", "true", "yes", "on"}

    # --verbose: DEBUG ?덈꺼 ?쒖꽦??(?덈씫 ?댁쑀 ???곸꽭 濡쒓렇)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("DEBUG mode enabled")

    # DRY_RUN: CLI --dry-run ?뚮옒洹?OR .env DRY_RUN=true ?????몄젙
    env_dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    dry_run = args.dry_run or env_dry_run
    trading_mode = CONFIG["trading"]["mode"]
    if trading_mode == "dry_run":
        log.info("execution mode: TRADING_MODE=dry_run (approval flow + simulated execution)")
    else:
        pass
    log.info(f"execution mode: {'DRY-RUN (signal logging only, no live execution)' if dry_run else 'LIVE (live trading enabled)'}")

    orch = Orchestrator(CONFIG, dry_run=dry_run, trading_mode=CONFIG["trading"]["mode"])

    if args.once:
        orch.run_cycle()
    else:
        log.info(f"loop started (interval {args.interval}s)")
        while True:
            try:
                orch.run_cycle()
            except KeyboardInterrupt:
                log.info("shutdown requested")
                break
            except Exception as e:
                log.error(f"cycle error: {e}", exc_info=True)

            next_run = (utc_now() + timedelta(seconds=args.interval)).strftime("%H:%M:%S UTC")
            log.info(f"waiting... next cycle: {next_run} ({args.interval}s later) - press Ctrl+C to stop")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()


