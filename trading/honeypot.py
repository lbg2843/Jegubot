from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import requests


GOPLUS_BSC_SECURITY_URL = "https://api.gopluslabs.io/api/v1/token_security/56"


@dataclass
class HoneypotCheckResult:
    is_safe: bool
    reason: str
    layer: str
    details: dict = field(default_factory=dict)
    buy_quote_ok: bool = False
    sell_quote_ok: bool = False
    approve_simulated: bool = False
    sell_simulated: bool = False
    buy_friction_pct: Optional[float] = None
    sell_friction_pct: Optional[float] = None
    round_trip_loss_pct: Optional[float] = None


class HoneypotChecker:
    """
    Multi-layer honeypot detection.

    Order of checks:
    1. Buy quote sanity
    2. Sell quote sanity
    3. Round-trip friction
    4. Static contract scan (BSC only)
    """

    def __init__(self, chain: str, wallet=None, session: Optional[requests.Session] = None):
        self.chain = (chain or "").lower()
        self.wallet = wallet
        self.session = session or requests.Session()

    def check(
        self,
        token_address: str,
        test_amount_usd: float = 5.0,
        max_friction_pct: float = 15.0,
    ) -> HoneypotCheckResult:
        token_address = str(token_address or "").strip()
        if not token_address:
            return HoneypotCheckResult(
                is_safe=False,
                reason="missing_token_address",
                layer="input",
                details={},
            )

        try:
            buy_out = self._quote_buy(token_address, test_amount_usd)
            if buy_out is None or buy_out <= 0:
                return HoneypotCheckResult(
                    is_safe=False,
                    reason="cannot_quote_buy",
                    layer="layer1_buy_quote",
                    details={"token": token_address},
                )
        except Exception as exc:
            return HoneypotCheckResult(
                is_safe=False,
                reason=f"buy_quote_error: {str(exc)[:100]}",
                layer="layer1_buy_quote",
                details={"token": token_address},
            )

        try:
            sell_out = self._quote_sell(token_address, buy_out)
            if sell_out is None or sell_out <= 0:
                return HoneypotCheckResult(
                    is_safe=False,
                    reason="cannot_quote_sell_HONEYPOT_LIKELY",
                    layer="layer2_sell_quote",
                    details={"buy_amount": buy_out},
                    buy_quote_ok=True,
                )
        except Exception as exc:
            return HoneypotCheckResult(
                is_safe=False,
                reason=f"sell_quote_error_HONEYPOT_LIKELY: {str(exc)[:100]}",
                layer="layer2_sell_quote",
                details={"buy_amount": buy_out},
                buy_quote_ok=True,
            )

        round_trip_loss = (1 - (sell_out / test_amount_usd)) * 100
        if round_trip_loss > max_friction_pct:
            return HoneypotCheckResult(
                is_safe=False,
                reason=f"excessive_round_trip_loss_{round_trip_loss:.1f}pct",
                layer="layer3_friction",
                details={
                    "buy_in": test_amount_usd,
                    "sell_out": sell_out,
                    "loss_pct": round_trip_loss,
                },
                buy_quote_ok=True,
                sell_quote_ok=True,
                round_trip_loss_pct=round_trip_loss,
            )

        if self.chain == "bsc":
            static_risk = self._static_contract_scan(token_address)
            if static_risk["risk_score"] >= 7:
                return HoneypotCheckResult(
                    is_safe=False,
                    reason=f"high_static_risk_{static_risk['risk_score']}",
                    layer="layer4_static",
                    details=static_risk,
                    buy_quote_ok=True,
                    sell_quote_ok=True,
                    round_trip_loss_pct=round_trip_loss,
                )

        return HoneypotCheckResult(
            is_safe=True,
            reason="all_checks_passed",
            layer="ok",
            details={
                "buy_quote": buy_out,
                "sell_quote": sell_out,
                "round_trip_loss_pct": round_trip_loss,
            },
            buy_quote_ok=True,
            sell_quote_ok=True,
            round_trip_loss_pct=round_trip_loss,
        )

    def _quote_buy(self, token_address: str, amount_usd: float) -> float:
        if self.chain == "bsc":
            from .dex_pancakeswap import PancakeSwap

            if self.wallet is None:
                raise ValueError("BSC wallet required for BSC honeypot quotes")
            return PancakeSwap(self.wallet).quote_buy(token_address, amount_usd)

        if self.chain == "solana":
            from .dex_jupiter import Jupiter

            if self.wallet is None:
                raise ValueError("Solana wallet required for Solana honeypot quotes")
            quote = Jupiter(self.wallet).quote_buy_in_usdc(token_address, amount_usd)
            return int(quote["outAmount"]) / 10 ** int(quote.get("outputDecimals", 9))

        raise ValueError(f"unsupported_chain: {self.chain}")

    def _quote_sell(self, token_address: str, token_amount: float) -> float:
        if self.chain == "bsc":
            from .dex_pancakeswap import PancakeSwap

            if self.wallet is None:
                raise ValueError("BSC wallet required for BSC honeypot quotes")
            return PancakeSwap(self.wallet).quote_sell(token_address, token_amount)

        if self.chain == "solana":
            from .dex_jupiter import Jupiter

            if self.wallet is None:
                raise ValueError("Solana wallet required for Solana honeypot quotes")
            quote = Jupiter(self.wallet).quote_sell_to_usdc(token_address, token_amount)
            return int(quote["outAmount"]) / 10**6

        raise ValueError(f"unsupported_chain: {self.chain}")

    def _static_contract_scan(self, token_address: str) -> dict:
        try:
            response = self.session.get(
                GOPLUS_BSC_SECURITY_URL,
                params={"contract_addresses": token_address},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            if str(data.get("code")) not in {"1", "200"}:
                return {"risk_score": 0, "error": "api_error", "raw": data}

            result = (data.get("result") or {}).get(token_address.lower(), {}) or {}
            risk_score = 0
            flags: list[str] = []

            def flag_if(field: str, score: int, label: str):
                nonlocal risk_score
                if str(result.get(field, "0")) == "1":
                    risk_score = max(risk_score, score)
                    flags.append(label)

            flag_if("is_honeypot", 10, "honeypot_confirmed")
            flag_if("cannot_sell_all", 9, "cannot_sell_all")
            flag_if("cannot_buy", 8, "cannot_buy")

            for field, score, label in (
                ("transfer_pausable", 2, "transfer_pausable"),
                ("trading_cooldown", 1, "trading_cooldown"),
                ("is_blacklisted", 2, "blacklist_function"),
                ("owner_change_balance", 3, "owner_can_change_balance"),
                ("hidden_owner", 2, "hidden_owner"),
            ):
                if str(result.get(field, "0")) == "1":
                    risk_score += score
                    flags.append(label)

            buy_tax_pct = self._normalize_tax_pct(result.get("buy_tax", "0"))
            sell_tax_pct = self._normalize_tax_pct(result.get("sell_tax", "0"))
            if buy_tax_pct > 10.0:
                risk_score += 2
                flags.append(f"high_buy_tax_{buy_tax_pct:.1f}pct")
            if sell_tax_pct > 10.0:
                risk_score += 3
                flags.append(f"high_sell_tax_{sell_tax_pct:.1f}pct")

            return {
                "risk_score": min(risk_score, 10),
                "flags": flags,
                "buy_tax_pct": buy_tax_pct,
                "sell_tax_pct": sell_tax_pct,
                "raw": result,
            }
        except Exception as exc:
            return {"risk_score": 0, "error": str(exc)}

    @staticmethod
    def _normalize_tax_pct(raw_value) -> float:
        try:
            value = float(raw_value or 0)
        except (TypeError, ValueError):
            return 0.0
        if value <= 1.0:
            return value * 100.0
        return value
