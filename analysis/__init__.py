"""Shared analysis helpers for Jegubot offline reports."""

from .corrections import CONSERVATIVE, IDEAL, PESSIMISTIC, PROFILES, REALISTIC, CorrectionProfile
from .loader import load_all_snapshots, load_candidates_from_log
from .metrics import PortfolioMetrics, compute_metrics
from .sim_engine import SimParams, TradeResult, params_for_chain, simulate_trade

__all__ = [
    "CONSERVATIVE",
    "CorrectionProfile",
    "IDEAL",
    "PESSIMISTIC",
    "PortfolioMetrics",
    "PROFILES",
    "REALISTIC",
    "SimParams",
    "TradeResult",
    "compute_metrics",
    "load_all_snapshots",
    "load_candidates_from_log",
    "params_for_chain",
    "simulate_trade",
]
