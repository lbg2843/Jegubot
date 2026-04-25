"""Correction profiles for realistic offline analysis."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorrectionProfile:
    name: str
    slippage_pct: dict[str, float]
    gas_fee_usd: dict[str, float]
    rug_as_total_loss: bool
    rug_loss_pct: float


IDEAL = CorrectionProfile(
    name="ideal",
    slippage_pct={"bsc": 0.0, "base": 0.0, "solana": 0.0},
    gas_fee_usd={"bsc": 0.0, "base": 0.0, "solana": 0.0},
    rug_as_total_loss=False,
    rug_loss_pct=0.0,
)

REALISTIC = CorrectionProfile(
    name="realistic",
    slippage_pct={"bsc": 1.5, "base": 1.0, "solana": 2.5},
    gas_fee_usd={"bsc": 0.50, "base": 0.08, "solana": 0.001},
    rug_as_total_loss=False,
    rug_loss_pct=0.0,
)

CONSERVATIVE = CorrectionProfile(
    name="conservative",
    slippage_pct={"bsc": 1.5, "base": 1.0, "solana": 2.5},
    gas_fee_usd={"bsc": 0.50, "base": 0.08, "solana": 0.001},
    rug_as_total_loss=True,
    rug_loss_pct=-95.0,
)

PESSIMISTIC = CorrectionProfile(
    name="pessimistic",
    slippage_pct={"bsc": 2.5, "base": 1.8, "solana": 4.0},
    gas_fee_usd={"bsc": 0.50, "base": 0.08, "solana": 0.001},
    rug_as_total_loss=True,
    rug_loss_pct=-95.0,
)


PROFILES = {
    "ideal": IDEAL,
    "realistic": REALISTIC,
    "conservative": CONSERVATIVE,
    "pessimistic": PESSIMISTIC,
}
