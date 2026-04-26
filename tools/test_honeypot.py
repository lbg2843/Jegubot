"""
Manual honeypot/scam filter check helper.

Examples:
  venv311\Scripts\python.exe tools\test_honeypot.py bsc 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82
  venv311\Scripts\python.exe tools\test_honeypot.py solana DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from trading.honeypot import HoneypotChecker
from trading.wallet_bsc import BSCWallet
from trading.wallet_solana import SolanaWallet


def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("chain", choices=["bsc", "solana"])
    parser.add_argument("token")
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--max-friction", type=float, default=15.0)
    args = parser.parse_args()

    wallet = BSCWallet() if args.chain == "bsc" else SolanaWallet()
    checker = HoneypotChecker(args.chain, wallet)

    started = time.perf_counter()
    result = checker.check(
        args.token,
        test_amount_usd=args.amount,
        max_friction_pct=args.max_friction,
    )
    elapsed = time.perf_counter() - started

    print(json.dumps(
        {
            "is_safe": result.is_safe,
            "reason": result.reason,
            "layer": result.layer,
            "round_trip_loss_pct": result.round_trip_loss_pct,
            "details": result.details,
            "elapsed_sec": round(elapsed, 3),
        },
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
