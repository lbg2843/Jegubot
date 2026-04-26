from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from .time_utils import utc_now


GOPLUS_BSC_SECURITY_URL = "https://api.gopluslabs.io/api/v1/token_security/56"


@dataclass
class HoneypotResult:
    is_safe: bool
    reason: str
    layer: str
    details: dict = field(default_factory=dict)
    buy_quote_ok: bool = False
    sell_quote_ok: bool = False
    round_trip_loss_pct: Optional[float] = None
    static_risk_score: int = 0
    checked_at: datetime = field(default_factory=utc_now)


HoneypotCheckResult = HoneypotResult


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
    ) -> HoneypotResult:
        token_address = str(token_address or "").strip()
        if not token_address:
            return HoneypotResult(
                is_safe=False,
                reason="missing_token_address",
                layer="input",
                details={},
            )

        static_result = None
        if self.chain == "bsc":
            static_result = self._goplus_scan_bsc(token_address)
            if static_result["risk_score"] >= 8:
                return HoneypotResult(
                    is_safe=False,
                    reason=f"high_static_risk_{static_result['risk_score']}",
                    layer="layer1_static",
                    details=static_result,
                    static_risk_score=static_result["risk_score"],
                )

        try:
            buy_out = self._quote_buy(token_address, test_amount_usd)
            if buy_out is None or buy_out <= 0:
                return HoneypotResult(
                    is_safe=False,
                    reason="buy_quote_zero",
                    layer="layer2_buy_quote",
                    details={"token": token_address},
                    static_risk_score=(static_result or {}).get("risk_score", 0),
                )
        except Exception as exc:
            return HoneypotResult(
                is_safe=False,
                reason=f"buy_quote_failed: {str(exc)[:80]}",
                layer="layer2_buy_quote",
                details={"token": token_address},
                static_risk_score=(static_result or {}).get("risk_score", 0),
            )

        try:
            sell_out = self._quote_sell(token_address, buy_out)
            if sell_out is None or sell_out <= 0:
                return HoneypotResult(
                    is_safe=False,
                    reason="SELL_QUOTE_ZERO_HONEYPOT",
                    layer="layer3_sell_quote",
                    details={"buy_amount": buy_out},
                    buy_quote_ok=True,
                    static_risk_score=(static_result or {}).get("risk_score", 0),
                )
        except Exception as exc:
            return HoneypotResult(
                is_safe=False,
                reason=f"SELL_QUOTE_FAILED_HONEYPOT: {str(exc)[:80]}",
                layer="layer3_sell_quote",
                details={"buy_amount": buy_out},
                buy_quote_ok=True,
                static_risk_score=(static_result or {}).get("risk_score", 0),
            )

        round_trip_loss = (1 - (sell_out / test_amount_usd)) * 100
        if round_trip_loss > max_friction_pct:
            return HoneypotResult(
                is_safe=False,
                reason=f"excessive_friction_{round_trip_loss:.1f}pct",
                layer="layer4_friction",
                details={
                    "test_amount_usd": test_amount_usd,
                    "buy_amount_token": buy_out,
                    "sell_back_usd": sell_out,
                },
                buy_quote_ok=True,
                sell_quote_ok=True,
                round_trip_loss_pct=round_trip_loss,
                static_risk_score=(static_result or {}).get("risk_score", 0),
            )

        return HoneypotResult(
            is_safe=True,
            reason="all_layers_passed",
            layer="ok",
            details={
                "buy_quote": buy_out,
                "sell_quote_usd": sell_out,
            },
            buy_quote_ok=True,
            sell_quote_ok=True,
            round_trip_loss_pct=round_trip_loss,
            static_risk_score=(static_result or {}).get("risk_score", 0),
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

    def _goplus_scan_bsc(self, token_address: str) -> dict:
        try:
            response = self.session.get(
                GOPLUS_BSC_SECURITY_URL,
                params={"contract_addresses": token_address.lower()},
                timeout=10,
            )
            data = response.json()
            if str(data.get("code")) not in {"1", "200"}:
                return {"risk_score": 0, "error": "goplus_api_error", "available": False}

            result = (data.get("result") or {}).get(token_address.lower(), {}) or {}
            if not result:
                return {"risk_score": 0, "error": "token_not_in_goplus_db", "available": False}
            risk_score = 0
            flags: list[str] = []

            def flag_if(field: str, score: int, label: str):
                nonlocal risk_score
                if str(result.get(field, "0")) == "1":
                    risk_score = max(risk_score, score)
                    flags.append(label)

            flag_if("is_honeypot", 10, "honeypot_confirmed")
            flag_if("cannot_sell_all", 9, "cannot_sell_all")

            for field, score, label in (
                ("transfer_pausable", 3, "transfer_pausable"),
                ("owner_change_balance", 3, "owner_can_change_balance"),
                ("hidden_owner", 2, "hidden_owner"),
                ("is_blacklisted", 2, "blacklist_function"),
                ("is_mintable", 1, "mintable"),
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

            try:
                holders = result.get("lp_holders", []) or []
                if holders:
                    locked_pct = sum(
                        float(holder.get("percent", 0) or 0)
                        for holder in holders
                        if holder.get("is_locked") == 1
                    )
                    if locked_pct < 0.5:
                        risk_score += 2
                        flags.append(f"lp_unlock_{(1 - locked_pct) * 100:.0f}pct")
            except Exception:
                pass

            return {
                "risk_score": min(risk_score, 10),
                "flags": flags,
                "buy_tax_pct": buy_tax_pct,
                "sell_tax_pct": sell_tax_pct,
                "available": True,
                "raw": result,
            }
        except requests.Timeout:
            return {"risk_score": 0, "error": "timeout", "available": False}
        except Exception as exc:
            return {"risk_score": 0, "error": str(exc)[:80], "available": False}

    @staticmethod
    def _normalize_tax_pct(raw_value) -> float:
        try:
            value = float(raw_value or 0)
        except (TypeError, ValueError):
            return 0.0
        if value <= 1.0:
            return value * 100.0
        return value
