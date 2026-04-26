"""
지갑 연결 상태 확인용 단독 스크립트.
실행: python tools/check_wallets.py
"""

import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if load_dotenv is not None:
    load_dotenv()
else:
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        import os

        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

print("=" * 60)
print("지갑 연결 상태 점검")
print("=" * 60)

print("\n[BSC - Rabby]")
try:
    from trading.wallet_bsc import BSCWallet

    bsc = BSCWallet()
    status = bsc.get_status()
    print(f"  주소: {status['address']}")
    print(f"  RPC: {status['rpc']}")
    print(f"  연결: {'OK' if status['connected'] else 'FAIL'}")
    print(f"  블록: {status['block_number']}")
    print(f"  BNB 잔액: {status['bnb_balance']:.6f} BNB")
    print(f"  USDT 잔액: {status['usdt_balance']:.4f} USDT")
    print(f"  가스 가격: {status['gas_price_gwei']:.2f} Gwei")
except Exception as exc:
    print(f"  ERROR: {exc}")

print("\n[Solana - Phantom]")
try:
    from trading.wallet_solana import SolanaWallet

    sol = SolanaWallet()
    status = sol.get_status()
    print(f"  주소: {status['address']}")
    print(f"  RPC: {status['rpc']}")
    print(f"  SOL 잔액: {status['sol_balance']:.6f} SOL")
    print(f"  USDC 잔액: {status['usdc_balance']:.4f} USDC")
except Exception as exc:
    print(f"  ERROR: {exc}")

print("\n" + "=" * 60)
print("점검 완료. 잔액이 0이어도 OK (아직 입금 안 함).")
print("주소가 맞는지 Rabby/Phantom 앱에서 비교 확인하세요.")
print("=" * 60)
