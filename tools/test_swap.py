"""
Manual swap verification tool for mainnet.

Usage:
  python tools/test_swap.py bsc-quote 0xTOKEN
  python tools/test_swap.py bsc-buy 0xTOKEN 5
  python tools/test_swap.py bsc-sell 0xTOKEN AMOUNT
  python tools/test_swap.py sol-quote MINT
  python tools/test_swap.py sol-buy MINT 5
  python tools/test_swap.py sol-sell MINT AMOUNT
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


def _load_env():
    if load_dotenv is not None:
        load_dotenv()
        return
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key.strip():
            import os

            os.environ.setdefault(key.strip(), value.strip())


_load_env()

MAX_MANUAL_SWAP_USD = 50.0


def confirm(prompt: str) -> bool:
    response = input(f"{prompt} (y/N): ").strip().lower()
    return response == "y"


def ensure_amount_cap(amount: float):
    if amount > MAX_MANUAL_SWAP_USD:
        raise ValueError(f"Refusing swap over ${MAX_MANUAL_SWAP_USD:.0f} in Phase 2B")


def cmd_bsc_quote(token: str):
    from trading.dex_pancakeswap import PancakeSwap
    from trading.wallet_bsc import BSCWallet

    wallet = BSCWallet()
    pancake = PancakeSwap(wallet)
    info = pancake.get_token_info(token)
    print(f"Token: {info['symbol']} ({token})")
    print(f"Decimals: {info['decimals']}")
    for usdt_amount in [1, 5, 10, 50]:
        out = pancake.quote_buy(token, usdt_amount)
        print(f"  ${usdt_amount} USDT -> {out:.6f} {info['symbol']}")


def cmd_bsc_buy(token: str, amount_usdt: float):
    from trading.dex_pancakeswap import PancakeSwap
    from trading.wallet_bsc import BSCWallet

    ensure_amount_cap(amount_usdt)
    wallet = BSCWallet()
    pancake = PancakeSwap(wallet)

    bnb = wallet.get_bnb_balance()
    usdt = wallet.get_usdt_balance()
    print("\nCurrent balance:")
    print(f"  BNB (gas): {bnb:.6f}")
    print(f"  USDT: {usdt:.4f}")

    if usdt < amount_usdt:
        print(f"ERROR USDT insufficient: {usdt:.4f} < {amount_usdt:.4f}")
        return
    if bnb < 0.001:
        print(f"ERROR BNB too low for gas: {bnb:.6f}")
        return

    info = pancake.get_token_info(token)
    expected = pancake.quote_buy(token, amount_usdt)
    print(f"\nQuote: ${amount_usdt} USDT -> {expected:.6f} {info['symbol']}")
    print(f"Min out with 2% slippage: {expected * 0.98:.6f}")

    if not confirm("\nExecute real swap?"):
        print("Cancelled")
        return

    result = pancake.buy(token, amount_usdt, slippage_pct=2.0)
    if result.success:
        print("\nSUCCESS")
        print(f"  TX: https://bscscan.com/tx/{result.tx_hash}")
        print(f"  Received: {result.amount_out:.6f} {info['symbol']}")
        print(f"  Gas used: {result.gas_used}")
        print(f"  Gas cost: {result.gas_cost_bnb:.6f} BNB")
    else:
        print(f"\nFAILED: {result.error}")
        if result.tx_hash:
            print(f"  TX: https://bscscan.com/tx/{result.tx_hash}")


def cmd_bsc_sell(token: str, amount_token: float):
    from trading.dex_pancakeswap import PancakeSwap
    from trading.wallet_bsc import BSCWallet

    wallet = BSCWallet()
    pancake = PancakeSwap(wallet)
    info = pancake.get_token_info(token)
    balance = wallet.get_token_balance(token)
    print(f"\nCurrent {info['symbol']} balance: {balance:.6f}")
    print(f"Quote to USDT: {pancake.quote_sell(token, amount_token):.6f}")

    if balance < amount_token:
        print("ERROR token balance insufficient")
        return
    if not confirm(f"\nSell {amount_token} {info['symbol']}?"):
        print("Cancelled")
        return

    result = pancake.sell(token, amount_token, slippage_pct=2.0)
    if result.success:
        print("SUCCESS")
        print(f"  TX: https://bscscan.com/tx/{result.tx_hash}")
        print(f"  Received: {result.amount_out:.6f} USDT")
        print(f"  Gas used: {result.gas_used}")
        print(f"  Gas cost: {result.gas_cost_bnb:.6f} BNB")
    else:
        print(f"FAILED: {result.error}")
        if result.tx_hash:
            print(f"  TX: https://bscscan.com/tx/{result.tx_hash}")


def cmd_sol_quote(mint: str):
    from trading.dex_jupiter import Jupiter
    from trading.wallet_solana import SolanaWallet

    try:
        wallet = SolanaWallet()
        jupiter = Jupiter(wallet)
        for usdc in [1, 5, 10]:
            quote = jupiter.quote_buy_in_usdc(mint, usdc)
            out = int(quote["outAmount"])
            decimals = quote["outputDecimals"]
            print(f"  ${usdc} USDC -> {out / 10**decimals:.6f} target")
    except Exception as exc:
        print(f"FAILED: {exc}")


def cmd_sol_buy(mint: str, amount_usdc: float):
    from trading.dex_jupiter import Jupiter
    from trading.wallet_solana import SolanaWallet

    ensure_amount_cap(amount_usdc)
    wallet = SolanaWallet()
    jupiter = Jupiter(wallet)

    sol = wallet.get_sol_balance()
    usdc = wallet.get_usdc_balance()
    print("\nCurrent balance:")
    print(f"  SOL (gas): {sol:.6f}")
    print(f"  USDC: {usdc:.4f}")

    if usdc < amount_usdc:
        print("ERROR USDC insufficient")
        return
    if sol < 0.005:
        print("ERROR SOL too low for gas")
        return

    quote = jupiter.quote_buy_in_usdc(mint, amount_usdc)
    out = int(quote["outAmount"]) / 10 ** quote["outputDecimals"]
    print(f"\nQuote: ${amount_usdc} USDC -> {out:.6f} target")

    if not confirm("\nExecute real swap?"):
        print("Cancelled")
        return

    result = jupiter.buy(mint, amount_usdc, slippage_pct=2.5)
    if result.success:
        print("SUCCESS")
        print(f"  TX: https://solscan.io/tx/{result.tx_signature}")
        print(f"  Received: {result.amount_out:.6f} target")
    else:
        print(f"FAILED: {result.error}")


def cmd_sol_sell(mint: str, amount_token: float):
    from trading.dex_jupiter import Jupiter
    from trading.wallet_solana import SolanaWallet

    wallet = SolanaWallet()
    jupiter = Jupiter(wallet)
    quote = jupiter.quote_sell_to_usdc(mint, amount_token)
    out = int(quote["outAmount"]) / 10**6
    print(f"\nQuote: {amount_token} token -> {out:.6f} USDC")

    if not confirm("\nExecute real sell?"):
        print("Cancelled")
        return

    result = jupiter.sell(mint, amount_token, slippage_pct=2.5)
    if result.success:
        print("SUCCESS")
        print(f"  TX: https://solscan.io/tx/{result.tx_signature}")
        print(f"  Received: {result.amount_out:.6f} USDC")
    else:
        print(f"FAILED: {result.error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["bsc-quote", "bsc-buy", "bsc-sell", "sol-quote", "sol-buy", "sol-sell"],
    )
    parser.add_argument("token")
    parser.add_argument("amount", nargs="?", type=float)
    args = parser.parse_args()

    if args.command == "bsc-quote":
        cmd_bsc_quote(args.token)
    elif args.command == "bsc-buy":
        if args.amount is None:
            print("amount required")
            return
        cmd_bsc_buy(args.token, args.amount)
    elif args.command == "bsc-sell":
        if args.amount is None:
            print("amount required")
            return
        cmd_bsc_sell(args.token, args.amount)
    elif args.command == "sol-quote":
        cmd_sol_quote(args.token)
    elif args.command == "sol-buy":
        if args.amount is None:
            print("amount required")
            return
        cmd_sol_buy(args.token, args.amount)
    elif args.command == "sol-sell":
        if args.amount is None:
            print("amount required")
            return
        cmd_sol_sell(args.token, args.amount)


if __name__ == "__main__":
    main()
