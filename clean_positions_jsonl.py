from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = REPO_DIR / 'data' / 'positions.jsonl'


def _entry_key(record: dict) -> tuple[str, str, str]:
    chain = str(record.get('chain') or '').strip().lower()
    contract = str(record.get('contract_address') or '').strip().lower()
    entry_ts = str(record.get('entry_timestamp') or record.get('opened_at') or '').strip()
    return chain, contract, entry_ts


def clean_positions_jsonl(path: Path = DEFAULT_DATA_PATH) -> tuple[int, int]:
    if not path.exists():
        return 0, 0

    latest_by_key: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []
    total = 0

    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except Exception:
            continue
        key = _entry_key(record)
        total += 1
        if key not in latest_by_key:
            order.append(key)
        latest_by_key[key] = record

    cleaned_records = [latest_by_key[key] for key in order]
    serialized = "`n".join(json.dumps(rec, ensure_ascii=False) for rec in cleaned_records)
    if serialized:
        serialized += "`n"
    path.write_text(serialized, encoding='utf-8', newline='\n')
    return total, len(cleaned_records)


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DATA_PATH
    before, after = clean_positions_jsonl(target)
    print(f'cleaned {target.name}: {before} -> {after}')
