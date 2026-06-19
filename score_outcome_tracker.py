import json
import logging
import os
from pathlib import Path

from trading.time_utils import iso_utc_now, parse_iso_utc, utc_now


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PENDING_PATH = DATA_DIR / "score_outcomes_pending.jsonl"
OUTCOMES_PATH = DATA_DIR / "score_outcomes.jsonl"

HORIZON_TO_SECONDS = {
    "1h": 3600,
    "4h": 4 * 3600,
    "24h": 24 * 3600,
}

# 24h 호라이즌 + 유예. 이보다 오래된 pending 은 유효 outcome 을 얻을 수 없어
# (토큰이 트렌딩에서 빠져 현재가 조회 불가) 조용히 prune → 큐 무한증식 차단.
_GRACE_SECONDS = 2 * 3600


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(content, encoding=encoding)
    os.replace(tmp_path, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    content = ""
    if rows:
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    _atomic_write_text(path, content, encoding="utf-8")


def _append_jsonl_record(path: Path, record: dict) -> None:
    rows = _read_jsonl(path)
    rows.append(record)
    _write_jsonl(path, rows)


def _tracking_enabled() -> bool:
    return os.getenv("SCORE_OUTCOME_TRACKING", "true").strip().lower() in {"1", "true", "yes", "on"}


def _max_fetch_attempts() -> int:
    try:
        return max(1, int(os.getenv("SCORE_OUTCOME_MAX_FETCH_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _max_per_cycle() -> int:
    # 가격 조회는 메모리 내 현재 스냅샷 스캔(네트워크 없음)이라 저렴 → 넉넉히.
    # 과거 기본값 10 은 유입(~수십/사이클)에 한참 못 미쳐 큐가 무한증식했음.
    try:
        return max(1, int(os.getenv("SCORE_OUTCOME_MAX_PER_CYCLE", "1000")))
    except ValueError:
        return 1000


def _expiry_seconds() -> int:
    try:
        return int(os.getenv("SCORE_OUTCOME_EXPIRY_SECONDS",
                             str(HORIZON_TO_SECONDS["24h"] + _GRACE_SECONDS)))
    except ValueError:
        return HORIZON_TO_SECONDS["24h"] + _GRACE_SECONDS


def build_snapshot_id(timestamp_iso: str, chain: str, token_address: str) -> str:
    return f"{timestamp_iso}__{chain}__{token_address}"


def add_pending(score_shadow_record: dict) -> None:
    if not _tracking_enabled():
        return
    timestamp_iso = str(score_shadow_record.get("timestamp") or "")
    chain = str(score_shadow_record.get("chain") or "")
    token_address = str(score_shadow_record.get("token_address") or "")
    if not timestamp_iso or not chain or not token_address:
        return

    # append-only. snapshot_id = ts__chain__token 이라 사이클마다 유니크 →
    # 과거의 read-all + O(N) 중복검사 + 전체 재기록(후보마다!)은 순수 낭비였음.
    # 중복(같은 사이클 같은 토큰 2회 점수)은 사실상 발생 안 하고, 나도 process 가
    # 무해하게 처리. → 진입당 비용 O(N)→O(1), 사이클당 219MB×수십회 churn 제거.
    record = {
        "snapshot_id": build_snapshot_id(timestamp_iso, chain, token_address),
        "timestamp": timestamp_iso,
        "chain": chain,
        "symbol": score_shadow_record.get("symbol", "?"),
        "token_address": token_address,
        "entry_price": float(score_shadow_record.get("entry_price") or score_shadow_record.get("price_usd") or 0.0),
        "final_score": float(score_shadow_record.get("final_score") or 0.0),
        "reflexivity_passed": bool(score_shadow_record.get("reflexivity_passed")),
        "checks_remaining": ["1h", "4h", "24h"],
        "fetch_attempts": {"1h": 0, "4h": 0, "24h": 0},
    }
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PENDING_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _lookup_current_price(pending_row: dict, snapshots_by_chain: dict[str, list[dict]]) -> tuple[str, float | None]:
    chain = str(pending_row.get("chain") or "")
    token_address = str(pending_row.get("token_address") or "")
    if not chain or not token_address:
        return "error", None

    for snap in snapshots_by_chain.get(chain, []):
        if str(snap.get("contract_address") or "") != token_address:
            continue
        try:
            return "ok", float(snap.get("price_usd") or 0.0)
        except Exception:
            return "error", None
    return "not_found", None


def process_pending(snapshots_by_chain: dict[str, list[dict]]) -> None:
    if not _tracking_enabled():
        return

    pending_rows = _read_jsonl(PENDING_PATH)
    if not pending_rows:
        return

    max_attempts = _max_fetch_attempts()
    max_checks = _max_per_cycle()
    expiry_seconds = _expiry_seconds()
    now = utc_now()
    checks_done = 0
    pruned = 0
    outcomes_to_append: list[dict] = []
    updated_rows: list[dict] = []

    for row in pending_rows:
        checks_remaining = list(row.get("checks_remaining") or [])
        fetch_attempts = dict(row.get("fetch_attempts") or {})
        if not checks_remaining:
            continue

        try:
            snapshot_at = parse_iso_utc(row.get("timestamp"))
        except Exception:
            continue

        # 24h+유예 경과: 토큰이 트렌딩에서 빠져 현재가 조회 불가 → 유효 outcome
        # 불가능. 조용히 drop(재기록·outcome 기록 안 함). 첫 실행 시 50만 backlog 자동 압축.
        if (now - snapshot_at).total_seconds() > expiry_seconds:
            pruned += 1
            continue

        remaining_after = []
        for horizon in checks_remaining:
            if checks_done >= max_checks:
                remaining_after.append(horizon)
                continue
            due_seconds = HORIZON_TO_SECONDS.get(horizon)
            if due_seconds is None:
                continue
            elapsed = (now - snapshot_at).total_seconds()
            if elapsed < due_seconds:
                remaining_after.append(horizon)
                continue

            checks_done += 1
            status, current_price = _lookup_current_price(row, snapshots_by_chain)
            entry_price = float(row.get("entry_price") or 0.0)

            if status == "ok" and current_price is not None and current_price > 0:
                price_change_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else None
                outcomes_to_append.append(
                    {
                        "snapshot_id": row.get("snapshot_id"),
                        "timestamp": row.get("timestamp"),
                        "chain": row.get("chain"),
                        "symbol": row.get("symbol"),
                        "token_address": row.get("token_address"),
                        "entry_price": entry_price,
                        "final_score": float(row.get("final_score") or 0.0),
                        "reflexivity_passed": bool(row.get("reflexivity_passed")),
                        "horizon": horizon,
                        "checked_at": iso_utc_now(),
                        "current_price": current_price,
                        "price_change_pct": price_change_pct,
                        "fetch_status": "ok",
                    }
                )
                continue

            attempts = int(fetch_attempts.get(horizon, 0) or 0) + 1
            fetch_attempts[horizon] = attempts
            if attempts >= max_attempts:
                outcomes_to_append.append(
                    {
                        "snapshot_id": row.get("snapshot_id"),
                        "timestamp": row.get("timestamp"),
                        "chain": row.get("chain"),
                        "symbol": row.get("symbol"),
                        "token_address": row.get("token_address"),
                        "entry_price": entry_price,
                        "final_score": float(row.get("final_score") or 0.0),
                        "reflexivity_passed": bool(row.get("reflexivity_passed")),
                        "horizon": horizon,
                        "checked_at": iso_utc_now(),
                        "current_price": None,
                        "price_change_pct": None,
                        "fetch_status": status,
                    }
                )
                continue

            remaining_after.append(horizon)

        if remaining_after:
            row["checks_remaining"] = remaining_after
            row["fetch_attempts"] = fetch_attempts
            updated_rows.append(row)

    if outcomes_to_append:
        outcome_rows = _read_jsonl(OUTCOMES_PATH)
        outcome_rows.extend(outcomes_to_append)
        _write_jsonl(OUTCOMES_PATH, outcome_rows)

    _write_jsonl(PENDING_PATH, updated_rows)

    if pruned:
        logging.getLogger("score_outcome_tracker").info(
            "pending prune: %d개 만료 제거(>%dh), 잔여 %d행",
            pruned, expiry_seconds // 3600, len(updated_rows))
