"""
PancakeSwap V2 Router for BSC.
Router: 0x10ED43C718714eb63d5aA57B78B54704E256024E
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

from .wallet_bsc import BSCWallet


log = logging.getLogger("trading.dex_pancakeswap")

PANCAKE_ROUTER_V2 = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


@dataclass
class SwapResult:
    success: bool
    tx_hash: Optional[str] = None
    amount_in: float = 0.0
    amount_out: float = 0.0
    gas_used: int = 0
    gas_cost_bnb: float = 0.0
    error: Optional[str] = None


class PancakeSwap:
    def __init__(self, wallet: BSCWallet):
        self.wallet = wallet
        self.w3 = wallet.w3
        self.router_address = Web3.to_checksum_address(PANCAKE_ROUTER_V2)
        self.router = self.w3.eth.contract(address=self.router_address, abi=ROUTER_ABI)

    def _erc20(self, token_address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )

    def get_token_info(self, token_address: str) -> dict:
        contract = self._erc20(token_address)
        return {
            "symbol": contract.functions.symbol().call(),
            "decimals": contract.functions.decimals().call(),
            "address": Web3.to_checksum_address(token_address),
        }

    def quote_buy(self, target_token: str, amount_usdt: float) -> float:
        path = [
            Web3.to_checksum_address(USDT_BSC),
            Web3.to_checksum_address(WBNB),
            Web3.to_checksum_address(target_token),
        ]
        amount_in_wei = int(amount_usdt * 10**18)
        amounts = self.router.functions.getAmountsOut(amount_in_wei, path).call()
        decimals = self.get_token_info(target_token)["decimals"]
        return amounts[-1] / (10**decimals)

    def quote_sell(self, token: str, amount_token: float) -> float:
        info = self.get_token_info(token)
        amount_in_wei = int(amount_token * 10 ** info["decimals"])
        path = [
            Web3.to_checksum_address(token),
            Web3.to_checksum_address(WBNB),
            Web3.to_checksum_address(USDT_BSC),
        ]
        amounts = self.router.functions.getAmountsOut(amount_in_wei, path).call()
        return amounts[-1] / 10**18

    def approve_if_needed(self, token: str, amount_wei: int) -> Optional[str]:
        contract = self._erc20(token)
        current = contract.functions.allowance(self.wallet.address, self.router_address).call()
        if current >= amount_wei:
            return None

        max_uint = 2**256 - 1
        nonce = self.w3.eth.get_transaction_count(self.wallet.address)
        tx = contract.functions.approve(self.router_address, max_uint).build_transaction(
            {
                "from": self.wallet.address,
                "nonce": nonce,
                "gas": 60000,
                "gasPrice": self.w3.eth.gas_price,
            }
        )
        signed = self.w3.eth.account.sign_transaction(tx, self.wallet.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        tx_hex = tx_hash.hex()
        log.info("approve tx sent: %s", tx_hex)
        if receipt.status != 1:
            raise RuntimeError(f"Approve failed: {tx_hex}")
        return tx_hex

    def _simulate(self, tx: dict):
        call_tx = {k: v for k, v in tx.items() if k not in {"gas", "gasPrice", "nonce", "chainId"}}
        self.w3.eth.call(call_tx)

    def _finalize_result(self, tx_hash, receipt, amount_in: float, balance_before: float, balance_after: float) -> SwapResult:
        gas_price = receipt.effectiveGasPrice if getattr(receipt, "effectiveGasPrice", None) else 0
        gas_cost_bnb = float(self.w3.from_wei(receipt.gasUsed * gas_price, "ether"))
        return SwapResult(
            success=True,
            tx_hash=tx_hash.hex(),
            amount_in=amount_in,
            amount_out=max(0.0, balance_after - balance_before),
            gas_used=receipt.gasUsed,
            gas_cost_bnb=gas_cost_bnb,
        )

    def buy(
        self,
        target_token: str,
        amount_usdt: float,
        slippage_pct: float = 2.0,
        deadline_seconds: int = 300,
    ) -> SwapResult:
        try:
            target_info = self.get_token_info(target_token)
            amount_in_wei = int(amount_usdt * 10**18)
            approve_hash = self.approve_if_needed(USDT_BSC, amount_in_wei)
            if approve_hash:
                print(f"  Approved USDT: {approve_hash}")

            expected_out = self.quote_buy(target_token, amount_usdt)
            min_out = expected_out * (1 - slippage_pct / 100)
            min_out_wei = int(min_out * 10 ** target_info["decimals"])
            path = [
                Web3.to_checksum_address(USDT_BSC),
                Web3.to_checksum_address(WBNB),
                Web3.to_checksum_address(target_token),
            ]
            deadline = int(time.time()) + deadline_seconds
            nonce = self.w3.eth.get_transaction_count(self.wallet.address)
            gas_price = self.w3.eth.gas_price

            balance_before = self.wallet.get_token_balance(target_token)
            tx = self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in_wei,
                min_out_wei,
                path,
                self.wallet.address,
                deadline,
            ).build_transaction(
                {
                    "from": self.wallet.address,
                    "nonce": nonce,
                    "gas": 300000,
                    "gasPrice": gas_price,
                }
            )
            self._simulate(tx)

            signed = self.w3.eth.account.sign_transaction(tx, self.wallet.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info("buy tx sent: %s", tx_hash.hex())
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                return SwapResult(success=False, tx_hash=tx_hash.hex(), error="Transaction reverted")

            balance_after = self.wallet.get_token_balance(target_token)
            return self._finalize_result(tx_hash, receipt, amount_usdt, balance_before, balance_after)
        except Exception as exc:
            return SwapResult(success=False, error=str(exc))

    def sell(
        self,
        token: str,
        amount_token: float,
        slippage_pct: float = 2.0,
        deadline_seconds: int = 300,
    ) -> SwapResult:
        try:
            token_info = self.get_token_info(token)
            amount_in_wei = int(amount_token * 10 ** token_info["decimals"])
            approve_hash = self.approve_if_needed(token, amount_in_wei)
            if approve_hash:
                print(f"  Approved {token_info['symbol']}: {approve_hash}")

            expected_out = self.quote_sell(token, amount_token)
            min_out = expected_out * (1 - slippage_pct / 100)
            min_out_wei = int(min_out * 10**18)
            path = [
                Web3.to_checksum_address(token),
                Web3.to_checksum_address(WBNB),
                Web3.to_checksum_address(USDT_BSC),
            ]
            deadline = int(time.time()) + deadline_seconds
            nonce = self.w3.eth.get_transaction_count(self.wallet.address)
            gas_price = self.w3.eth.gas_price

            usdt_before = self.wallet.get_usdt_balance()
            tx = self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in_wei,
                min_out_wei,
                path,
                self.wallet.address,
                deadline,
            ).build_transaction(
                {
                    "from": self.wallet.address,
                    "nonce": nonce,
                    "gas": 300000,
                    "gasPrice": gas_price,
                }
            )
            self._simulate(tx)

            signed = self.w3.eth.account.sign_transaction(tx, self.wallet.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info("sell tx sent: %s", tx_hash.hex())
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                return SwapResult(success=False, tx_hash=tx_hash.hex(), error="Transaction reverted")

            usdt_after = self.wallet.get_usdt_balance()
            return self._finalize_result(tx_hash, receipt, amount_token, usdt_before, usdt_after)
        except Exception as exc:
            return SwapResult(success=False, error=str(exc))
