"""Load logs and snapshot series for offline analysis."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SNAPSHOT_CHAINS = ("bsc", "base", "solana")

_CANDIDATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[DRY-RUN\] "
    r"진입 후보: \[(\w+)\] (\S+) price=\$([0-9.]+)"
)
_CYCLE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?사이클 시작")
_ERROR_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[(ERROR|WARNING)\].*?:\s*(.*)")


def _parse_snapshot_record(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        record = json.loads(raw)
        record["_ts"] = _to_utc_datetime(record["timestamp"])
        record["_price"] = float(record.get("price_usd") or 0.0)
        record["_symbol"] = (record.get("symbol") or "").upper()
        return record
    except Exception:
        return None


def load_snapshots(data_dir: Path, chain: str) -> dict[str, list[dict]]:
    """Return sorted snapshots grouped by symbol for a single chain."""
    path = data_dir / f"{chain}_snapshots.jsonl"
    grouped: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return {}

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            record = _parse_snapshot_record(line)
            if not record:
                continue
            grouped[record["_symbol"]].append(record)

    for symbol, rows in grouped.items():
        rows.sort(key=lambda item: item["_ts"])
        grouped[symbol] = rows
    return dict(grouped)


def load_all_snapshots(data_dir: Path, chains: Iterable[str] = SNAPSHOT_CHAINS) -> dict[str, dict[str, list[dict]]]:
    return {chain: load_snapshots(data_dir, chain) for chain in chains}


def load_candidates_from_log(log_path: Path) -> list[dict]:
    """Return unique dry-run candidates keyed by the first chain/symbol occurrence."""
    seen: dict[tuple[str, str], dict] = {}
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _CANDIDATE_RE.search(line)
            if not match:
                continue
            ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            chain = match.group(2).lower()
            symbol = match.group(3).upper()
            if (chain, symbol) in seen:
                continue
            seen[(chain, symbol)] = {
                "ts": ts,
                "chain": chain,
                "symbol": symbol,
                "entry_price": float(match.group(4)),
            }
    return sorted(seen.values(), key=lambda item: item["ts"])


def parse_log_diagnostics(log_path: Path) -> tuple[list[datetime], list[tuple[str, str]]]:
    cycles: list[datetime] = []
    errors: list[tuple[str, str]] = []
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            cycle_match = _CYCLE_RE.search(line)
            if cycle_match:
                cycles.append(datetime.strptime(cycle_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
            error_match = _ERROR_RE.search(line)
            if error_match:
                errors.append((error_match.group(2), error_match.group(3).strip()))
    return cycles, errors


def series_from_entry(ts_list: list[dict], entry_ts: datetime) -> list[dict]:
    return [row for row in ts_list if row["_ts"] >= entry_ts]


def first_price_at_or_after(ts_list: list[dict], target_ts: datetime) -> float | None:
    for row in ts_list:
        if row["_ts"] >= target_ts and row["_price"] > 0:
            return row["_price"]
    return None


def _to_utc_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
