"""
Jupiter Swap API integration for Solana.
Manual swap helper only. Not wired into auto trading.
"""

from __future__ import annotations

import base64
import logging
import os
import socket
from dataclasses import dataclass
from typing import Optional

import requests
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.types import TxOpts

from .wallet_solana import SolanaWallet


log = logging.getLogger("trading.dex_jupiter")

JUPITER_BASE_URL = "https://api.jup.ag/swap/v1"
JUPITER_QUOTE_URL = f"{JUPITER_BASE_URL}/quote"
JUPITER_SWAP_URL = f"{JUPITER_BASE_URL}/swap"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class JupiterSwapResult:
    success: bool
    tx_signature: Optional[str] = None
    amount_in: float = 0.0
    amount_out: float = 0.0
    error: Optional[str] = None


class Jupiter:
    def __init__(self, wallet: SolanaWallet):
        self.wallet = wallet
        self.client = wallet.client
        self.api_key = os.getenv("JUPITER_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError("JUPITER_API_KEY missing in .env")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            }
        )

    def _friendly_error(self, exc: Exception) -> str:
        if isinstance(exc, requests.exceptions.ConnectionError):
            return (
                "Cannot reach Jupiter API. Check DNS/network/firewall access to "
                "api.jup.ag and confirm JUPITER_API_KEY is set."
            )
        if isinstance(exc, socket.gaierror):
            return "DNS lookup failed for api.jup.ag"
        return str(exc)

    def _get_mint_decimals(self, mint_address: str) -> int:
        mint_info = self.client.get_account_info_json_parsed(Pubkey.from_string(mint_address))
        value = mint_info.value
        if value is None:
            raise ValueError(f"Mint not found: {mint_address}")
        return int(value.data.parsed["info"]["decimals"])

    def _get_quote(self, input_mint: str, output_mint: str, amount_smallest_unit: int, slippage_bps: int) -> dict:
        response = self.session.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount_smallest_unit,
                "slippageBps": slippage_bps,
                "swapMode": "ExactIn",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def quote_buy_in_usdc(self, target_token_mint: str, amount_usdc: float, slippage_bps: int = 250) -> dict:
        amount_in = int(amount_usdc * 10**6)
        quote = self._get_quote(USDC_MINT, target_token_mint, amount_in, slippage_bps)
        quote["outputDecimals"] = self._get_mint_decimals(target_token_mint)
        return quote

    def quote_sell_to_usdc(self, token_mint: str, amount_token: float, slippage_bps: int = 250) -> dict:
        decimals = self._get_mint_decimals(token_mint)
        amount_in = int(amount_token * 10**decimals)
        quote = self._get_quote(token_mint, USDC_MINT, amount_in, slippage_bps)
        quote["inputDecimals"] = decimals
        return quote

    def _build_swap_transaction(self, quote: dict) -> VersionedTransaction:
        swap_response = self.session.post(
            JUPITER_SWAP_URL,
            json={
                "userPublicKey": str(self.wallet.keypair.pubkey()),
                "quoteResponse": quote,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            },
            timeout=20,
        )
        swap_response.raise_for_status()
        swap_data = swap_response.json()
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        return VersionedTransaction.from_bytes(tx_bytes)

    def _send_signed(self, versioned_tx: VersionedTransaction) -> str:
        signed_tx = VersionedTransaction(versioned_tx.message, [self.wallet.keypair])
        send_resp = self.client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )
        tx_sig = str(send_resp.value)
        log.info("jupiter tx sent: %s", tx_sig)
        self.client.confirm_transaction(send_resp.value, commitment="confirmed")
        return tx_sig

    def buy(self, target_token_mint: str, amount_usdc: float, slippage_pct: float = 2.5) -> JupiterSwapResult:
        try:
            slippage_bps = int(slippage_pct * 100)
            target_before = self.wallet.get_token_balance(target_token_mint)
            quote = self.quote_buy_in_usdc(target_token_mint, amount_usdc, slippage_bps)
            versioned_tx = self._build_swap_transaction(quote)
            tx_sig = self._send_signed(versioned_tx)
            target_after = self.wallet.get_token_balance(target_token_mint)
            return JupiterSwapResult(
                success=True,
                tx_signature=tx_sig,
                amount_in=amount_usdc,
                amount_out=max(0.0, target_after - target_before),
            )
        except Exception as exc:
            return JupiterSwapResult(success=False, error=self._friendly_error(exc))

    def sell(self, token_mint: str, amount_token: float, slippage_pct: float = 2.5) -> JupiterSwapResult:
        try:
            slippage_bps = int(slippage_pct * 100)
            usdc_before = self.wallet.get_usdc_balance()
            quote = self.quote_sell_to_usdc(token_mint, amount_token, slippage_bps)
            versioned_tx = self._build_swap_transaction(quote)
            tx_sig = self._send_signed(versioned_tx)
            usdc_after = self.wallet.get_usdc_balance()
            return JupiterSwapResult(
                success=True,
                tx_signature=tx_sig,
                amount_in=amount_token,
                amount_out=max(0.0, usdc_after - usdc_before),
            )
        except Exception as exc:
            return JupiterSwapResult(success=False, error=self._friendly_error(exc))
