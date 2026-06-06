import json
import shutil
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POSITIONS = REPO / 'data' / 'positions.jsonl'
BACKUP = REPO / 'data' / 'positions.jsonl.bak_20260602'


def load_lines(path: Path):
    raw = path.read_text(encoding='utf-8-sig', errors='replace')
    raw = raw.replace('\r\n', '\n').replace('\r', '\n').replace('`r`n', '\n').replace('`n', '\n')
    return raw.split('\n')


def main():
    if not POSITIONS.exists():
        raise SystemExit(f'missing file: {POSITIONS}')

    if not BACKUP.exists():
        shutil.copy2(POSITIONS, BACKUP)

    lines = load_lines(POSITIONS)
    records = []
    parse_failures = 0
    for idx, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            rec = json.loads(line)
        except Exception as exc:
            parse_failures += 1
            print(f'WARN: parse failure at line {idx}: {exc}', file=sys.stderr)
            continue
        key = (
            str(rec.get('chain') or '').lower(),
            str(rec.get('contract_address') or ''),
            str(rec.get('entry_timestamp') or ''),
        )
        records.append((idx, key, rec))

    counts = Counter(key for _, key, _ in records)
    for key, count in counts.items():
        if count > 1:
            print(f'WARN: duplicate key found — {key} appears {count} times. Using last record.', file=sys.stderr)

    last_by_key = {}
    for idx, key, rec in records:
        last_by_key[key] = (idx, rec)

    deduped = [item[1] for item in sorted(last_by_key.values(), key=lambda t: t[0])]

    closed = 0
    open_ = 0
    for rec in deduped:
        is_closed = bool(rec.get('is_closed'))
        exit_ts = rec.get('exit_timestamp')
        symbol = rec.get('symbol', '?')
        entry_ts = rec.get('entry_timestamp')
        if is_closed:
            closed += 1
            if not exit_ts:
                print(f'WARN: closed record missing exit_timestamp — {symbol} {entry_ts}', file=sys.stderr)
        else:
            open_ += 1
            if exit_ts:
                print(f'WARN: open record has exit_timestamp — {symbol} {entry_ts}', file=sys.stderr)

    output = '\n'.join(json.dumps(rec, ensure_ascii=False) for rec in deduped) + '\n'
    POSITIONS.write_text(output, encoding='utf-8')

    print(f'before: {len(records)}')
    print(f'after: {len(deduped)}')
    print(f'unique_keys: {len(last_by_key)}')
    print(f'is_closed=true: {closed}')
    print(f'is_closed=false: {open_}')
    if parse_failures:
        print(f'parse_failures: {parse_failures}')


if __name__ == '__main__':
    main()