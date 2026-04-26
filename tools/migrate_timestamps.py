"""
Normalize stored position timestamps to explicit UTC offsets.

Usage:
  venv311\Scripts\python.exe tools\migrate_timestamps.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading.time_utils import parse_iso_utc

POSITIONS_PATH = ROOT / "data" / "positions.jsonl"
TIMESTAMP_FIELDS = ("entry_timestamp", "last_update", "exit_timestamp")


def main():
    if not POSITIONS_PATH.exists():
        print(f"positions file not found: {POSITIONS_PATH}")
        return

    backup = POSITIONS_PATH.with_name(f"{POSITIONS_PATH.stem}.backup{POSITIONS_PATH.suffix}")
    shutil.copy2(POSITIONS_PATH, backup)
    print(f"backup created: {backup}")

    changed = 0
    rows = []
    for raw in POSITIONS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        row_changed = False
        for field in TIMESTAMP_FIELDS:
            value = row.get(field)
            if not value:
                continue
            normalized = parse_iso_utc(value).isoformat()
            if normalized != value:
                row[field] = normalized
                row_changed = True
        if row_changed:
            changed += 1
        rows.append(json.dumps(row, ensure_ascii=False))

    POSITIONS_PATH.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    print(f"done: {changed} rows normalized to explicit UTC")


if __name__ == "__main__":
    main()
