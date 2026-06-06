import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
MATCH_WINDOW_MINUTES = 15


@dataclass
class Outcome:
    snapshot_id: str
    timestamp: str
    chain: str
    symbol: str
    token_address: str
    entry_price: float
    final_score: float
    reflexivity_passed: bool
    horizon: str
    checked_at: str
    current_price: float
    price_change_pct: float
    fetch_status: str


@dataclass
class Candidate:
    snapshot_id: str
    timestamp: str
    chain: str
    symbol: str
    token_address: str
    price_usd: float
    price_change_5m_pct: float
    price_change_15m_pct: float
    price_change_1h_pct: float
    price_change_24h_pct: float
    volume_15m_usd: float
    volume_24h_usd: float
    avg_tx_size_usd: float
    txns_per_hour: int
    liquidity_usd: float
    pool_age_hours: float
    entry_path: Optional[str]
    final_score: float
    passed: bool
    entry_taken: bool
    skip_reason: Optional[str]


@dataclass
class Snapshot:
    timestamp: str
    chain: str
    symbol: str
    contract_address: str
    price_usd: float
    price_change_1h_pct: float
    price_change_24h_pct: float
    liquidity_usd: float
    volume_1h_usd: float
    txns_1h: int
    market_cap_usd: float = 0.0
    holders_binance: int = 0
    holders_total: int = 0
    dex_id: str = ""
    avg_tx_size_usd: float = 0.0


@dataclass
class ActualTrade:
    chain: str
    symbol: str
    contract_address: str
    entry_timestamp: str
    exit_timestamp: str
    entry_path: Optional[str]
    realized_pnl_pct: float
    realized_pnl_usd: float
    exit_reason: Optional[str]


def parse_iso_utc(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    txt = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_kst(value: str, is_end: bool = False) -> datetime:
    value = value.strip()
    if len(value) == 10:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
        if is_end:
            dt += timedelta(days=1)
        return dt
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def kst_range_to_utc(start_kst: str, end_kst: str) -> tuple[datetime, datetime]:
    return parse_kst(start_kst).astimezone(UTC), parse_kst(end_kst, is_end=True).astimezone(UTC)


def _read_jsonl_lines(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("`r`n", "\n").replace("`n", "\n")
    return raw.split("\n")


def _iter_json(path: Path):
    for line in _read_jsonl_lines(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def candidate_snapshot_id(timestamp: str, chain: str, token_address: str) -> str:
    return f"{timestamp}__{chain}__{token_address}"


def load_outcomes(path: Path | None = None) -> dict[str, list[Outcome]]:
    path = path or (DATA_DIR / "score_outcomes.jsonl")
    outcomes_by_id: dict[str, list[Outcome]] = {}
    for d in _iter_json(path):
        o = Outcome(
            snapshot_id=str(d.get("snapshot_id") or ""),
            timestamp=str(d.get("timestamp") or ""),
            chain=str(d.get("chain") or ""),
            symbol=str(d.get("symbol") or ""),
            token_address=str(d.get("token_address") or ""),
            entry_price=float(d.get("entry_price") or 0.0),
            final_score=float(d.get("final_score") or 0.0),
            reflexivity_passed=bool(d.get("reflexivity_passed")),
            horizon=str(d.get("horizon") or ""),
            checked_at=str(d.get("checked_at") or ""),
            current_price=float(d.get("current_price") or 0.0),
            price_change_pct=float(d.get("price_change_pct") or 0.0),
            fetch_status=str(d.get("fetch_status") or ""),
        )
        if o.snapshot_id:
            outcomes_by_id.setdefault(o.snapshot_id, []).append(o)
    return outcomes_by_id


def _dedup_positions(records: list[dict]) -> list[dict]:
    last_by_key: dict[tuple[str, str, str], dict] = {}
    for rec in records:
        key = (
            str(rec.get("chain") or "").lower(),
            str(rec.get("contract_address") or "").lower(),
            str(rec.get("entry_timestamp") or rec.get("opened_at") or ""),
        )
        last_by_key[key] = rec
    return list(last_by_key.values())


def _load_actual_entries(path: Path | None = None) -> list[dict]:
    path = path or (DATA_DIR / "positions.jsonl")
    records = _dedup_positions(list(_iter_json(path)))
    entries = []
    for rec in records:
        ts = parse_iso_utc(rec.get("entry_timestamp"))
        if not ts:
            continue
        entries.append({
            "chain": str(rec.get("chain") or "").lower(),
            "token_address": str(rec.get("contract_address") or "").lower(),
            "entry_timestamp": ts,
            "entry_path": rec.get("entry_path") or "reflexivity",
            "symbol": rec.get("symbol") or "",
        })
    return entries


def _match_candidates_to_actual_entries(candidates: list[Candidate], entries: list[dict]) -> None:
    by_key: dict[tuple[str, str], list[tuple[datetime, int]]] = {}
    for idx, cand in enumerate(candidates):
        ts = parse_iso_utc(cand.timestamp)
        if not ts:
            continue
        key = (cand.chain.lower(), cand.token_address.lower())
        by_key.setdefault(key, []).append((ts, idx))

    for items in by_key.values():
        items.sort(key=lambda x: x[0])

    window = timedelta(minutes=MATCH_WINDOW_MINUTES)
    used = set()
    for entry in sorted(entries, key=lambda e: e["entry_timestamp"]):
        key = (entry["chain"], entry["token_address"])
        items = by_key.get(key, [])
        chosen = None
        for ts, idx in items:
            if idx in used:
                continue
            delta = entry["entry_timestamp"] - ts
            if delta.total_seconds() < 0:
                continue
            if delta <= window:
                chosen = idx
            elif delta > window and chosen is not None:
                break
        if chosen is not None:
            used.add(chosen)
            candidates[chosen].passed = True
            candidates[chosen].entry_taken = True
            candidates[chosen].entry_path = entry["entry_path"]


def load_candidates(path: Path | None = None, only_paths: list[str] | None = None) -> list[Candidate]:
    path = path or (DATA_DIR / "score_shadow.jsonl")
    candidates: list[Candidate] = []
    for d in _iter_json(path):
        entry_path = d.get("entry_path")
        if entry_path is None:
            continue
        snapshot = d.get("snapshot") or {}
        raw = d.get("raw_signals") or {}
        token_address = str(d.get("token_address") or d.get("contract_address") or "").lower()
        timestamp = str(d.get("timestamp") or "")
        if not token_address or not timestamp:
            continue
        c = Candidate(
            snapshot_id=candidate_snapshot_id(timestamp, str(d.get("chain") or ""), token_address),
            timestamp=timestamp,
            chain=str(d.get("chain") or ""),
            symbol=str(d.get("symbol") or ""),
            token_address=token_address,
            price_usd=float(d.get("entry_price") or d.get("price_usd") or 0.0),
            price_change_5m_pct=float(d.get("price_change_5m_pct") or snapshot.get("price_change_5m_pct") or 0.0),
            price_change_15m_pct=float(d.get("price_change_15m_pct") or snapshot.get("price_change_15m_pct") or 0.0),
            price_change_1h_pct=float(d.get("price_change_1h_pct") or snapshot.get("price_change_1h_pct") or 0.0),
            price_change_24h_pct=float(d.get("price_change_24h_pct") or snapshot.get("price_change_24h_pct") or 0.0),
            volume_15m_usd=float(d.get("volume_15m_usd") or snapshot.get("volume_15m_usd") or 0.0),
            volume_24h_usd=float(d.get("volume_24h_usd") or snapshot.get("volume_24h_usd") or 0.0),
            avg_tx_size_usd=float(d.get("avg_tx_size_usd") or snapshot.get("avg_tx_size_usd") or 0.0),
            txns_per_hour=int(d.get("txns_per_hour") or raw.get("txns_per_hour") or d.get("txns_1h") or snapshot.get("txns_1h") or 0),
            liquidity_usd=float(d.get("liquidity_usd") or snapshot.get("liquidity_usd") or 0.0),
            pool_age_hours=float(d.get("pool_age_hours") or snapshot.get("pool_age_hours") or 0.0),
            entry_path=str(entry_path),
            final_score=float(d.get("final_score") or (d.get("score_v1") or {}).get("final_score") or 0.0),
            passed=False,
            entry_taken=False,
            skip_reason=d.get("skip_reason"),
        )
        candidates.append(c)

    _match_candidates_to_actual_entries(candidates, _load_actual_entries())

    if only_paths:
        candidates = [c for c in candidates if c.entry_path in only_paths]
    return candidates


def load_snapshots(chains: list | None = None) -> dict:
    if chains is None:
        chains = ['base', 'bsc', 'solana']

    result = defaultdict(list)
    total = 0
    for chain in chains:
        path = DATA_DIR / f"{chain}_snapshots.jsonl"
        if not path.exists():
            print(f"WARN: {path} not found")
            continue
        for d in _iter_json(path):
            try:
                s = Snapshot(
                    timestamp=str(d['timestamp']),
                    chain=str(d.get('chain', chain)),
                    symbol=str(d.get('symbol', '?')),
                    contract_address=str(d.get('contract_address', '')).lower(),
                    price_usd=float(d.get('price_usd', 0) or 0),
                    price_change_1h_pct=float(d.get('price_change_1h_pct', 0) or 0),
                    price_change_24h_pct=float(d.get('price_change_24h_pct', 0) or 0),
                    liquidity_usd=float(d.get('liquidity_usd', 0) or 0),
                    volume_1h_usd=float(d.get('volume_1h_usd', 0) or 0),
                    txns_1h=int(d.get('txns_1h', 0) or 0),
                    market_cap_usd=float(d.get('market_cap_usd', 0) or 0),
                    holders_binance=int(d.get('holders_binance', 0) or 0),
                    holders_total=int(d.get('holders_total', 0) or 0),
                    dex_id=str(d.get('dex_id', '') or ''),
                    avg_tx_size_usd=float(d.get('avg_tx_size_usd', 0) or 0),
                )
            except Exception:
                continue
            key = (s.chain, s.contract_address)
            result[key].append(s)
            total += 1

    for key in result:
        result[key].sort(key=lambda s: s.timestamp)

    print(f"Loaded {total} snapshots for {len(result)} unique tokens")
    return result


def load_actual_trades(path: Path | None = None, start_kst: str | None = None, end_kst: str | None = None) -> list[ActualTrade]:
    path = path or (DATA_DIR / "positions.jsonl")
    records = _dedup_positions(list(_iter_json(path)))
    start_utc = end_utc = None
    if start_kst and end_kst:
        start_utc, end_utc = kst_range_to_utc(start_kst, end_kst)
    trades: list[ActualTrade] = []
    for rec in records:
        if not bool(rec.get("is_closed")):
            continue
        exit_ts = parse_iso_utc(rec.get("exit_timestamp"))
        if not exit_ts:
            continue
        if start_utc and not (start_utc <= exit_ts < end_utc):
            continue
        trades.append(
            ActualTrade(
                chain=str(rec.get("chain") or ""),
                symbol=str(rec.get("symbol") or ""),
                contract_address=str(rec.get("contract_address") or "").lower(),
                entry_timestamp=str(rec.get("entry_timestamp") or ""),
                exit_timestamp=str(rec.get("exit_timestamp") or ""),
                entry_path=rec.get("entry_path"),
                realized_pnl_pct=float(rec.get("realized_pnl_pct") or 0.0),
                realized_pnl_usd=float(rec.get("realized_pnl_usd") or 0.0),
                exit_reason=rec.get("exit_reason"),
            )
        )
    return trades


def filter_by_period(candidates: list[Candidate], start_kst: str, end_kst: str) -> list[Candidate]:
    start_utc, end_utc = kst_range_to_utc(start_kst, end_kst)
    out: list[Candidate] = []
    for cand in candidates:
        ts = parse_iso_utc(cand.timestamp)
        if ts and start_utc <= ts < end_utc:
            out.append(cand)
    return out