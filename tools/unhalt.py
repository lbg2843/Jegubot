r"""
Emergency halt reset tool.

Usage:
  venv311\Scripts\python.exe tools\unhalt.py
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "trading_safety_state.json"


def main():
    if not STATE_PATH.exists():
        print(f"State file not found: {STATE_PATH}")
        return

    backup_path = ROOT / "data" / f"trading_safety_state.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    shutil.copy2(STATE_PATH, backup_path)
    print(f"Backup created: {backup_path}")

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    print("\n현재 상태:")
    print(f"  halt_until: {state.get('halt_until')}")
    print(f"  halt_reason: {state.get('halt_reason')}")
    print(f"  consecutive_losses: {state.get('consecutive_losses', 0)}")
    print(f"  recent_losses: {len(state.get('recent_losses') or [])}")

    state["halt_until"] = None
    state["halt_reason"] = None
    state["consecutive_losses"] = 0
    state["recent_losses"] = []
    state["halt_count_today"] = 0

    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("\nOK: Halt cleared. Bot restart required.")


if __name__ == "__main__":
    main()
