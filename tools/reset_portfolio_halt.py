"""
Reset portfolio halt state for mc_position_manager.
Usage:
  .\venv311\Scripts\python.exe tools\reset_portfolio_halt.py
"""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from mc_position_manager import CHAIN_CONFIGS, PortfolioConfig, MultichainPositionManager


def main() -> None:
    data_dir = ROOT / "data"
    storage_path = data_dir / "positions.jsonl"
    state_path = data_dir / "portfolio_state.json"

    pm = MultichainPositionManager(
        CHAIN_CONFIGS,
        PortfolioConfig(chain_allocation={"bsc": 0.55, "solana": 0.45}),
        storage_path=str(storage_path),
        state_path=str(state_path),
        mode="live",
    )

    before = {
        "initial_capital": pm.initial_capital,
        "peak_equity": pm.peak_equity,
        "realized_pnl_total": pm.realized_pnl_total,
        "halt_mode": pm.halt_mode,
        "halt_recovery_equity": pm.halt_recovery_equity,
        "halt_until": pm.halt_until,
        "current_equity": pm.current_equity(),
    }
    print("Before reset:")
    for key, value in before.items():
        print(f"  {key}: {value}")

    baseline_equity = pm.current_equity() or pm.initial_capital or float(os.getenv("TOTAL_CAPITAL_USD", "0") or 0.0)
    pm.reset_portfolio_halt(current_equity=baseline_equity)

    after = {
        "initial_capital": pm.initial_capital,
        "peak_equity": pm.peak_equity,
        "realized_pnl_total": pm.realized_pnl_total,
        "halt_mode": pm.halt_mode,
        "halt_recovery_equity": pm.halt_recovery_equity,
        "halt_until": pm.halt_until,
        "current_equity": pm.current_equity(),
        "state_path": str(state_path),
    }
    print("\nAfter reset:")
    for key, value in after.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
