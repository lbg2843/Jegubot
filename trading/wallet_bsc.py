import os

from eth_account import Account
from web3 import Web3


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


class BSCWallet:
    def __init__(self):
        self.private_key = os.getenv("BSC_PRIVATE_KEY")
        self.address = os.getenv("BSC_WALLET_ADDRESS")
        self.rpc_url = os.getenv("BSC_RPC", "https://bsc-dataseed1.binance.org")

        if not self.private_key or not self.address:
            raise ValueError("BSC_PRIVATE_KEY or BSC_WALLET_ADDRESS missing in .env")

        if not self.private_key.startswith("0x"):
            self.private_key = "0x" + self.private_key

        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot connect to BSC RPC: {self.rpc_url}")

        self.account = Account.from_key(self.private_key)
        if self.account.address.lower() != self.address.lower():
            raise ValueError(
                f"Private key derives {self.account.address} "
                f"but BSC_WALLET_ADDRESS is {self.address}"
            )

    def get_bnb_balance(self) -> float:
        wei = self.w3.eth.get_balance(self.address)
        return float(self.w3.from_wei(wei, "ether"))

    def get_token_balance(self, token_address: str) -> float:
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        balance_raw = contract.functions.balanceOf(self.address).call()
        decimals = contract.functions.decimals().call()
        return balance_raw / (10 ** decimals)

    def get_usdt_balance(self) -> float:
        usdt_bsc = "0x55d398326f99059fF775485246999027B3197955"
        return self.get_token_balance(usdt_bsc)

    def estimate_gas_price_gwei(self) -> float:
        return float(self.w3.from_wei(self.w3.eth.gas_price, "gwei"))

    def get_status(self) -> dict:
        return {
            "address": self.address,
            "rpc": self.rpc_url,
            "connected": self.w3.is_connected(),
            "bnb_balance": self.get_bnb_balance(),
            "usdt_balance": self.get_usdt_balance(),
            "gas_price_gwei": self.estimate_gas_price_gwei(),
            "block_number": self.w3.eth.block_number,
        }
