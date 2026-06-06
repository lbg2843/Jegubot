from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
UTC = timezone.utc
DEFAULT_POSITIONS_PATH = Path(r'C:\Users\82109\Desktop\APP만들기\Jegubot\data\positions.jsonl')
SIZE_FALLBACK_USD = 50.0

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def parse_iso_utc(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_text(raw: str) -> str:
    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('`r`n', '\n').replace('`n', '\n').replace('`r', '\n')
    return text


def load_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    raw = path.read_text(encoding='utf-8-sig', errors='replace')
    text = normalize_text(raw)
    lines = text.split('\n')
    total_lines = len(lines)
    parsed = 0
    failed = 0
    warnings: list[str] = []
    last_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    dup_counts: Counter[tuple[str, str, str]] = Counter()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        try:
            record = json.loads(stripped)
        except Exception:
            failed += 1
            warnings.append(f'WARN: parse failure — {stripped[:120]}')
            continue
        parsed += 1
        key = make_key(record)
        dup_counts[key] += 1
        last_by_key[key] = record

    records = list(last_by_key.values())
    duplicate_lines = 0
    for key, count in dup_counts.items():
        if count > 1:
            duplicate_lines += count - 1
            warnings.append(f'WARN: duplicate key found — {key} appears {count} times. Using last record.')

    return records, {
        'total_lines': total_lines,
        'parsed': parsed,
        'failed': failed,
        'unique_keys': len(records),
        'duplicate_lines': duplicate_lines,
    }, warnings


def make_key(record: dict[str, Any]) -> tuple[str, str, str]:
    chain = str(record.get('chain') or '').strip().lower()
    contract = str(record.get('contract_address') or '').strip().lower()
    entry_ts = str(record.get('entry_timestamp') or record.get('opened_at') or '').strip()
    return chain, contract, entry_ts


def safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return fallback
        return float(value)
    except Exception:
        return fallback


def safe_int(value: Any, fallback: int = 0) -> int:
    try:
        if value is None or value == '':
            return fallback
        return int(value)
    except Exception:
        return fallback


def classify_path(value: Any) -> str:
    if value is None:
        return 'null'
    text = str(value).strip()
    return text if text else 'null'


def holds_hours(entry_dt: datetime, ref_dt: datetime) -> float:
    return max(0.0, (ref_dt - entry_dt).total_seconds() / 3600.0)


def filter_between(dt: datetime | None, start_utc: datetime, end_utc: datetime) -> bool:
    return dt is not None and start_utc <= dt < end_utc


def collect_integrity_warnings(records: list[dict[str, Any]], now_utc: datetime) -> list[str]:
    warnings: list[str] = []
    open_contract_counts: Counter[str] = Counter()

    for rec in records:
        symbol = str(rec.get('symbol') or '?')
        entry_ts_raw = str(rec.get('entry_timestamp') or rec.get('opened_at') or '')
        entry_dt = parse_iso_utc(entry_ts_raw)
        exit_dt = parse_iso_utc(rec.get('exit_timestamp'))
        is_closed = bool(rec.get('is_closed'))
        contract = str(rec.get('contract_address') or '').strip().lower()
        realized_pct = rec.get('realized_pnl_pct')

        if entry_dt and entry_dt > now_utc:
            warnings.append(f'WARN: future entry_timestamp — {symbol} {entry_ts_raw}')

        if is_closed and (exit_dt is None or realized_pct is None):
            warnings.append(f'WARN: closed record missing exit fields — {symbol} {entry_ts_raw}')

        if (not is_closed) and exit_dt is not None:
            warnings.append(f'WARN: open record has exit_timestamp — {symbol} {entry_ts_raw}')

        if not is_closed and contract:
            open_contract_counts[contract] += 1

    for contract, count in open_contract_counts.items():
        if count > 1:
            warnings.append(f'WARN: multiple open positions for same contract — {contract}, {count} positions')

    return warnings


def build_markdown(report: dict[str, Any]) -> str:
    health = report['health']
    period = report['period']
    entries = report['entries']
    closed = report['closed']
    open_now = report['open_now']

    lines: list[str] = []
    lines.append('# PnL Report')
    lines.append('')
    lines.append('## Period')
    lines.append(f"- KST: {period['kst_start']} ~ {period['kst_end']}")
    lines.append(f"- UTC: {period['utc_start']} ~ {period['utc_end']}")
    lines.append('')
    lines.append('## Data Health Check')
    lines.append(f"- positions.jsonl 총 라인: {health['total_lines']}")
    lines.append(f"- 파싱 성공: {health['parsed']}")
    lines.append(f"- 파싱 실패: {health['failed']}")
    lines.append(f"- Unique keys (chain, contract, entry_ts): {health['unique_keys']}")
    lines.append(f"- 중복 라인 발견: {health['duplicate_lines']}")
    lines.append('')
    lines.append('## Entries opened in range')
    lines.append(f"- 총 진입: {entries['count']}건")
    lines.append(f"- by chain: base={entries['by_chain'].get('base', 0)}, bsc={entries['by_chain'].get('bsc', 0)}, solana={entries['by_chain'].get('solana', 0)}")
    lines.append(
        f"- by entry_path: sweet_spot={entries['by_path'].get('sweet_spot', 0)}, reflexivity={entries['by_path'].get('reflexivity', 0)}, golden_zone={entries['by_path'].get('golden_zone', 0)}, null={entries['by_path'].get('null', 0)}"
    )
    lines.append('')
    lines.append('## Closed in range')
    lines.append(f"- 총 청산: {closed['count']}건")
    lines.append(f"- Wins (pnl > 0): {closed['wins']}")
    lines.append(f"- Losses (pnl < 0): {closed['losses']}")
    lines.append(f"- Flat (pnl == 0): {closed['flat']}")
    lines.append(f"- Realized PnL: ${closed['realized_sum']:.2f}")
    lines.append(f"- Avg PnL%: {closed['avg_pct']:.2f}%")
    lines.append(f"- Median PnL%: {closed['median_pct']:.2f}%")
    lines.append('')
    lines.append('### By entry_path (closed in range)')
    lines.append('| Path | N | Win% | Avg% | Sum$ |')
    lines.append('|------|---|------|------|------|')
    for row in closed['by_path_chain_rows']:
        lines.append(f"| {row['label']} | {row['n']} | {row['win_pct']:.1f}% | {row['avg_pct']:.2f}% | {row['sum_usd']:.2f} |")
    lines.append('')
    lines.append('### By exit_reason (closed in range)')
    lines.append('| Reason | N | Avg% | Sum$ |')
    lines.append('|--------|---|------|------|')
    for row in closed['by_reason_rows']:
        lines.append(f"| {row['reason']} | {row['n']} | {row['avg_pct']:.2f}% | {row['sum_usd']:.2f} |")
    lines.append('')
    lines.append('## Detailed Trades (closed in range)')
    lines.append('정렬: exit_timestamp ASC')
    lines.append('')
    lines.append('| Symbol | Chain | Path | Entry(KST) | Exit(KST) | Hold | Entry$ | Exit$ | PnL% | PnL$ | Reason |')
    lines.append('|--------|-------|------|------------|-----------|------|--------|-------|------|------|--------|')
    for row in closed['detail_rows']:
        lines.append(
            f"| {row['symbol']} | {row['chain']} | {row['path']} | {row['entry_kst']} | {row['exit_kst']} | {row['hold_h']}h | {row['entry_price']} | {row['exit_price']} | {row['pnl_pct']:.2f}% | {row['pnl_usd']:.2f} | {row['reason']} |"
        )
    lines.append('')
    lines.append('## Open Positions Now (dedup 적용)')
    lines.append('정렬: entry_timestamp ASC')
    lines.append('')
    lines.append('| Symbol | Chain | Path | Entry(KST) | Hold | Entry$ | Current$ | Peak | Unreal% | Unreal$ |')
    lines.append('|--------|-------|------|------------|------|--------|----------|------|---------|---------|')
    for row in open_now['rows']:
        lines.append(
            f"| {row['symbol']} | {row['chain']} | {row['path']} | {row['entry_kst']} | {row['hold_h']}h | {row['entry_price']} | {row['current_price']} | {row['peak_price']} | {row['unreal_pct']:.2f}% | {row['unreal_usd']:.2f} |"
        )
    return '\n'.join(lines) + '\n'


def build_report(records: list[dict[str, Any]], health: dict[str, int], start_utc: datetime, end_utc: datetime, now_utc: datetime) -> dict[str, Any]:
    entries_opened: list[dict[str, Any]] = []
    closed_in_range: list[dict[str, Any]] = []
    open_now: list[dict[str, Any]] = []

    for rec in records:
        rec.setdefault('size_usd', SIZE_FALLBACK_USD if rec.get('size_usd') in (None, '') else rec.get('size_usd'))
        entry_dt = parse_iso_utc(rec.get('entry_timestamp') or rec.get('opened_at'))
        exit_dt = parse_iso_utc(rec.get('exit_timestamp'))
        is_closed = bool(rec.get('is_closed')) and exit_dt is not None

        if filter_between(entry_dt, start_utc, end_utc):
            entries_opened.append(rec)
        if is_closed and filter_between(exit_dt, start_utc, end_utc):
            closed_in_range.append(rec)
        if not bool(rec.get('is_closed')) and exit_dt is None:
            open_now.append(rec)

    entry_chain_counts = Counter(str(rec.get('chain') or '').lower() for rec in entries_opened)
    entry_path_counts = Counter(classify_path(rec.get('entry_path')) for rec in entries_opened)

    realized_pcts = [safe_float(rec.get('realized_pnl_pct')) for rec in closed_in_range]
    realized_usds = [safe_float(rec.get('realized_pnl_usd')) for rec in closed_in_range]
    wins = sum(1 for value in realized_usds if value > 0)
    losses = sum(1 for value in realized_usds if value < 0)
    flat = sum(1 for value in realized_usds if value == 0)

    by_path_chain: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in closed_in_range:
        by_path_chain[(str(rec.get('chain') or '').lower(), classify_path(rec.get('entry_path')))].append(rec)

    by_path_chain_rows = []
    for (chain, path), rows in sorted(by_path_chain.items()):
        pnls = [safe_float(r.get('realized_pnl_usd')) for r in rows]
        pcts = [safe_float(r.get('realized_pnl_pct')) for r in rows]
        by_path_chain_rows.append({
            'label': f'{chain}/{path}',
            'n': len(rows),
            'win_pct': (sum(1 for p in pnls if p > 0) / len(rows) * 100.0) if rows else 0.0,
            'avg_pct': statistics.mean(pcts) if pcts else 0.0,
            'sum_usd': sum(pnls),
        })

    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in closed_in_range:
        by_reason[str(rec.get('exit_reason') or 'NONE')].append(rec)
    by_reason_rows = []
    for reason, rows in sorted(by_reason.items()):
        pnls = [safe_float(r.get('realized_pnl_usd')) for r in rows]
        pcts = [safe_float(r.get('realized_pnl_pct')) for r in rows]
        by_reason_rows.append({
            'reason': reason,
            'n': len(rows),
            'avg_pct': statistics.mean(pcts) if pcts else 0.0,
            'sum_usd': sum(pnls),
        })

    detail_rows = []
    for rec in sorted(closed_in_range, key=lambda r: parse_iso_utc(r.get('exit_timestamp')) or datetime.min.replace(tzinfo=UTC)):
        entry_dt = parse_iso_utc(rec.get('entry_timestamp') or rec.get('opened_at'))
        exit_dt = parse_iso_utc(rec.get('exit_timestamp'))
        hold_h = holds_hours(entry_dt, exit_dt) if entry_dt and exit_dt else 0.0
        detail_rows.append({
            'symbol': rec.get('symbol') or '?',
            'chain': rec.get('chain') or '?',
            'path': classify_path(rec.get('entry_path')),
            'entry_kst': entry_dt.astimezone(KST).strftime('%Y-%m-%d %H:%M') if entry_dt else '-',
            'exit_kst': exit_dt.astimezone(KST).strftime('%Y-%m-%d %H:%M') if exit_dt else '-',
            'hold_h': f'{hold_h:.1f}',
            'entry_price': f"{safe_float(rec.get('entry_price')):.8f}",
            'exit_price': f"{safe_float(rec.get('exit_price')):.8f}",
            'pnl_pct': safe_float(rec.get('realized_pnl_pct')),
            'pnl_usd': safe_float(rec.get('realized_pnl_usd')),
            'reason': rec.get('exit_reason') or 'NONE',
        })

    open_rows = []
    for rec in sorted(open_now, key=lambda r: parse_iso_utc(r.get('entry_timestamp') or r.get('opened_at')) or datetime.min.replace(tzinfo=UTC)):
        entry_dt = parse_iso_utc(rec.get('entry_timestamp') or rec.get('opened_at'))
        entry_price = safe_float(rec.get('entry_price'))
        current_price = safe_float(rec.get('current_price'))
        peak_price = safe_float(rec.get('peak_price'))
        size_usd = safe_float(rec.get('size_usd'), SIZE_FALLBACK_USD)
        unreal_pct = ((current_price / entry_price - 1.0) * 100.0) if entry_price > 0 else 0.0
        unreal_usd = size_usd * unreal_pct / 100.0
        open_rows.append({
            'symbol': rec.get('symbol') or '?',
            'chain': rec.get('chain') or '?',
            'path': classify_path(rec.get('entry_path')),
            'entry_kst': entry_dt.astimezone(KST).strftime('%Y-%m-%d %H:%M') if entry_dt else '-',
            'hold_h': f'{holds_hours(entry_dt, now_utc):.1f}' if entry_dt else '0.0',
            'entry_price': f"{entry_price:.8f}",
            'current_price': f"{current_price:.8f}",
            'peak_price': f"{peak_price:.8f}",
            'unreal_pct': unreal_pct,
            'unreal_usd': unreal_usd,
        })

    return {
        'period': {
            'kst_start': start_utc.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S %Z'),
            'kst_end': end_utc.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S %Z'),
            'utc_start': start_utc.strftime('%Y-%m-%d %H:%M:%S %Z'),
            'utc_end': end_utc.strftime('%Y-%m-%d %H:%M:%S %Z'),
        },
        'health': health,
        'entries': {
            'count': len(entries_opened),
            'by_chain': dict(entry_chain_counts),
            'by_path': dict(entry_path_counts),
        },
        'closed': {
            'count': len(closed_in_range),
            'wins': wins,
            'losses': losses,
            'flat': flat,
            'realized_sum': sum(realized_usds),
            'avg_pct': statistics.mean(realized_pcts) if realized_pcts else 0.0,
            'median_pct': statistics.median(realized_pcts) if realized_pcts else 0.0,
            'by_path_chain_rows': by_path_chain_rows,
            'by_reason_rows': by_reason_rows,
            'detail_rows': detail_rows,
        },
        'open_now': {
            'count': len(open_now),
            'rows': open_rows,
        },
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument('--kst-start')
    ap.add_argument('--kst-end')
    ap.add_argument('--last-24h', action='store_true')
    ap.add_argument('--last-7d', action='store_true')
    ap.add_argument('--last-30d', action='store_true')
    ap.add_argument('--output')
    ap.add_argument('--json', action='store_true')
    return ap.parse_args()


def resolve_period(args: argparse.Namespace, now_utc: datetime) -> tuple[datetime, datetime]:
    if args.kst_start or args.kst_end:
        if not (args.kst_start and args.kst_end):
            raise SystemExit('--kst-start and --kst-end must be provided together')
        start = datetime.strptime(args.kst_start, '%Y-%m-%d %H:%M').replace(tzinfo=KST).astimezone(UTC)
        end = datetime.strptime(args.kst_end, '%Y-%m-%d %H:%M').replace(tzinfo=KST).astimezone(UTC)
        return start, end
    if args.last_24h:
        return now_utc - timedelta(hours=24), now_utc
    if args.last_7d:
        return now_utc - timedelta(days=7), now_utc
    if args.last_30d:
        return now_utc - timedelta(days=30), now_utc
    raise SystemExit('One of --kst-start/--kst-end, --last-24h, --last-7d, or --last-30d is required')


def main() -> int:
    args = parse_args()
    now_utc = datetime.now(UTC)
    start_utc, end_utc = resolve_period(args, now_utc)
    records, health, parse_warnings = load_records(DEFAULT_POSITIONS_PATH)
    integrity_warnings = collect_integrity_warnings(records, now_utc)
    for message in parse_warnings + integrity_warnings:
        warn(message)
    report = build_report(records, health, start_utc, end_utc, now_utc)
    output = json.dumps(report, ensure_ascii=False, indent=2) if args.json else build_markdown(report)
    if args.output:
        Path(args.output).write_text(output, encoding='utf-8', newline='\n')
    else:
        print(output, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
