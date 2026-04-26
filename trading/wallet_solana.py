import os

import base58
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey


class SolanaWallet:
    def __init__(self):
        self.private_key_b58 = os.getenv("SOLANA_PRIVATE_KEY")
        self.address = os.getenv("SOLANA_WALLET_ADDRESS")
        self.rpc_url = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

        if not self.private_key_b58 or not self.address:
            raise ValueError("SOLANA_PRIVATE_KEY or SOLANA_WALLET_ADDRESS missing")

        self.client = Client(self.rpc_url)

        try:
            secret_bytes = base58.b58decode(self.private_key_b58)
        except Exception as exc:
            raise ValueError(f"Invalid SOLANA_PRIVATE_KEY Base58 payload: {exc}") from exc

        if len(secret_bytes) != 64:
            raise ValueError(
                f"Invalid Solana private key length: {len(secret_bytes)} "
                "(expected 64 bytes from Phantom export)"
            )

        self.keypair = Keypair.from_bytes(secret_bytes)

        if str(self.keypair.pubkey()) != self.address:
            raise ValueError(
                f"Private key derives {self.keypair.pubkey()} "
                f"but SOLANA_WALLET_ADDRESS is {self.address}"
            )

    def get_sol_balance(self) -> float:
        resp = self.client.get_balance(self.keypair.pubkey())
        return resp.value / 1e9

    def get_token_balance(self, mint_address: str) -> float:
        owner = self.keypair.pubkey()
        mint_pubkey = Pubkey.from_string(mint_address)

        try:
            opts = TokenAccountOpts(mint=mint_pubkey)
            resp = self.client.get_token_accounts_by_owner_json_parsed(owner, opts)
            if not resp.value:
                return 0.0

            account_info = resp.value[0]
            parsed = account_info.account.data.parsed
            ui_amount = parsed["info"]["tokenAmount"]["uiAmount"]
            return float(ui_amount or 0.0)
        except Exception:
            return 0.0

    def get_usdc_balance(self) -> float:
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        return self.get_token_balance(usdc_mint)

    def get_status(self) -> dict:
        return {
            "address": self.address,
            "rpc": self.rpc_url,
            "sol_balance": self.get_sol_balance(),
            "usdc_balance": self.get_usdc_balance(),
        }
