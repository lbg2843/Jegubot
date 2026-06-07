"""Compare four entry models on the historical dry-run candidate dataset."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from analysis.corrections import IDEAL
from analysis.loader import (
    SNAPSHOT_CHAINS,
    load_all_snapshots,
    load_candidates_from_log,
    series_from_entry,
)
from analysis.metrics import PortfolioMetrics, compute_metrics
from analysis.sim_engine import (
    ANALYSIS_TOTAL_CAPITAL_USD,
    SimParams,
    TradeResult,
    position_size_usd_for_chain,
    simulate_trade,
)


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "reflexivity.log"
REPORT_PATH = BASE_DIR / "reports" / "multi_model_comparison.md"
CHAINS = ("bsc", "solana")


@dataclass
class SignalDecision:
    eligible: bool
    missing_data: bool = False
    note: str = ""


@dataclass
class ModelSpec:
    name: str
    description: str
    evaluator: Callable[[dict, Optional[dict]], SignalDecision]


@dataclass
class ModelRun:
    model: ModelSpec
    trades: list[TradeResult]
    eligible_signals: int
    total_opportunities: int
    data_missing_events: int
    span_hours: float
    metrics: PortfolioMetrics
    avg_hold_hours: float
    equity_max_dd_pct: float


def _common_params() -> SimParams:
    return SimParams(
        stop_loss_pct=-18.0,
        trailing_stop_pct=8.0,
        trailing_activation_pct=5.0,
        take_profit_pct=30.0,
        max_hold_hours=12,
        slippage_pct=0.0,
        gas_fee_usd=0.0,
        rug_as_total_loss=False,
        profit_lock_enabled=True,
        correction=IDEAL,
    )


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def model_reflexivity(snapshot: dict, prev_snapshot: Optional[dict]) -> SignalDecision:
    change_1h = _as_float(snapshot.get("price_change_1h_pct"))
    volume_1h = _as_float(snapshot.get("volume_1h_usd"))
    txns_1h = int(snapshot.get("txns_1h") or 0)
    return SignalDecision(5 <= change_1h <= 25 and volume_1h >= 50_000 and txns_1h >= 50)


def model_volume_spike(snapshot: dict, prev_snapshot: Optional[dict]) -> SignalDecision:
    change_1h = _as_float(snapshot.get("price_change_1h_pct"))
    volume_1h = _as_float(snapshot.get("volume_1h_usd"))
    volume_24h = _as_float(snapshot.get("volume_24h_usd"))
    txns_1h = int(snapshot.get("txns_1h") or 0)
    if volume_24h <= 0:
        return SignalDecision(False, missing_data=True, note="missing_volume_24h")
    avg_hourly = volume_24h / 24.0
    if avg_hourly <= 0:
        return SignalDecision(False, missing_data=True, note="invalid_volume_24h")
    volume_ratio = volume_1h / avg_hourly
    return SignalDecision(0 <= change_1h <= 5 and volume_ratio >= 5 and txns_1h >= 100)


def model_mean_reversion(snapshot: dict, prev_snapshot: Optional[dict]) -> SignalDecision:
    if prev_snapshot is None:
        return SignalDecision(False, missing_data=True, note="missing_prev_snapshot")
    change_1h = _as_float(snapshot.get("price_change_1h_pct"))
    volume_1h = _as_float(snapshot.get("volume_1h_usd"))
    prev_volume_1h = _as_float(prev_snapshot.get("volume_1h_usd"))
    liquidity = _as_float(snapshot.get("liquidity_usd"))
    if prev_volume_1h <= 0:
        return SignalDecision(False, missing_data=True, note="missing_prev_volume")
    return SignalDecision(
        -20 <= change_1h <= -10 and volume_1h > prev_volume_1h * 1.5 and liquidity >= 100_000
    )


def model_dip_buy(snapshot: dict, prev_snapshot: Optional[dict]) -> SignalDecision:
    change_24h = _as_float(snapshot.get("price_change_24h_pct"))
    change_1h = _as_float(snapshot.get("price_change_1h_pct"))
    volume_1h = _as_float(snapshot.get("volume_1h_usd"))
    volume_24h = _as_float(snapshot.get("volume_24h_usd"))
    if volume_24h <= 0:
        return SignalDecision(False, missing_data=True, note="missing_volume_24h")
    avg_hourly = volume_24h / 24.0
    if avg_hourly <= 0:
        return SignalDecision(False, missing_data=True, note="invalid_volume_24h")
    return SignalDecision(change_24h > 0 and -15 <= change_1h <= -5 and volume_1h >= avg_hourly)


MODELS = [
    ModelSpec(
        name="Reflexivity",
        description="1h change +5~25%, 1h volume >= $50K, txns >= 50",
        evaluator=model_reflexivity,
    ),
    ModelSpec(
        name="Volume Spike",
        description="1h change 0~5%, 1h volume >= 24h avg * 5, txns >= 100",
        evaluator=model_volume_spike,
    ),
    ModelSpec(
        name="Mean Reversion",
        description="1h change -10~-20%, volume trend > 1.5x, liquidity >= $100K",
        evaluator=model_mean_reversion,
    ),
    ModelSpec(
        name="Dip Buy",
        description="24h positive, 1h pullback -5~-15%, 1h volume >= 24h avg hourly",
        evaluator=model_dip_buy,
    ),
]


def _find_entry_snapshot(series: list[dict], candidate_ts: datetime) -> tuple[Optional[int], Optional[dict]]:
    for index, row in enumerate(series):
        if row["_ts"] >= candidate_ts:
            return index, row
    return None, None


def _simulate_model(
    model: ModelSpec,
    snapshots_by_chain: dict[str, dict[str, list[dict]]],
    candidates: list[dict],
) -> ModelRun:
    trades: list[TradeResult] = []
    eligible_signals = 0
    data_missing_events = 0
    filtered_candidates = [candidate for candidate in candidates if candidate["chain"] in CHAINS]

    min_ts: Optional[datetime] = None
    max_ts: Optional[datetime] = None

    for candidate in filtered_candidates:
        min_ts = candidate["ts"] if min_ts is None else min(min_ts, candidate["ts"])
        max_ts = candidate["ts"] if max_ts is None else max(max_ts, candidate["ts"])

        chain = candidate["chain"]
        symbol = candidate["symbol"]
        series = snapshots_by_chain.get(chain, {}).get(symbol, [])
        if not series:
            continue

        index, snapshot = _find_entry_snapshot(series, candidate["ts"])
        if snapshot is None or index is None:
            continue

        prev_snapshot = series[index - 1] if index > 0 else None
        decision = model.evaluator(snapshot, prev_snapshot)
        if decision.missing_data:
            data_missing_events += 1
        if not decision.eligible:
            continue

        eligible_signals += 1
        entry_snapshot = dict(snapshot)
        entry_snapshot["_position_size_usd"] = position_size_usd_for_chain(chain, ANALYSIS_TOTAL_CAPITAL_USD)
        future = series_from_entry(series, candidate["ts"])[1:]
        trades.append(simulate_trade(entry_snapshot, future, _common_params()))

    metrics = compute_metrics(trades)
    closed = [trade for trade in trades if trade.exit_reason != "open" and trade.hold_hours is not None]
    avg_hold_hours = sum(trade.hold_hours or 0.0 for trade in closed) / len(closed) if closed else 0.0
    span_hours = (
        (max_ts - min_ts).total_seconds() / 3600.0
        if min_ts is not None and max_ts is not None and max_ts >= min_ts
        else 0.0
    )
    return ModelRun(
        model=model,
        trades=trades,
        eligible_signals=eligible_signals,
        total_opportunities=len(filtered_candidates),
        data_missing_events=data_missing_events,
        span_hours=span_hours,
        metrics=metrics,
        avg_hold_hours=avg_hold_hours,
        equity_max_dd_pct=_equity_max_drawdown_pct(trades),
    )


def _equity_max_drawdown_pct(trades: list[TradeResult]) -> float:
    closed = [trade for trade in trades if trade.exit_timestamp is not None and trade.pnl_usd is not None]
    closed.sort(key=lambda trade: trade.exit_timestamp or trade.entry_timestamp)
    equity = ANALYSIS_TOTAL_CAPITAL_USD
    peak = equity
    max_dd = 0.0
    for trade in closed:
        equity += float(trade.pnl_usd or 0.0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity / peak - 1.0) * 100.0)
    return max_dd


def _candidate_rate(run: ModelRun) -> float:
    if run.span_hours <= 0:
        return 0.0
    return run.eligible_signals / run.span_hours


def _exit_reason_summary(trades: list[TradeResult]) -> str:
    counter = Counter(trade.exit_reason for trade in trades if trade.exit_reason != "open")
    if not counter:
        return "-"
    return ", ".join(f"{reason}:{count}" for reason, count in sorted(counter.items()))


def _recommendation(runs: list[ModelRun]) -> str:
    viable = [run for run in runs if run.metrics.closed_trades >= 5]
    if not viable:
        return "모든 모델이 표본 부족이거나 데이터 부족입니다. 추가 dry_run 로그가 더 필요합니다."

    best = max(
        viable,
        key=lambda item: (
            item.metrics.total_pnl_usd,
            item.metrics.profit_factor,
            item.metrics.win_rate_pct,
            item.equity_max_dd_pct,
        ),
    )
    return (
        f"현재 후보 로그 표본 기준 가장 나아 보이는 모델은 **{best.model.name}** 입니다. "
        f"총손익 ${best.metrics.total_pnl_usd:+.2f}, "
        f"승률 {best.metrics.win_rate_pct:.1f}%, "
        f"PF {best.metrics.profit_factor:.2f}, "
        f"Max DD {best.equity_max_dd_pct:.2f}%."
    )


def _build_markdown(runs: list[ModelRun], candidate_count: int) -> str:
    lines = [
        "# Multi-Model Comparison",
        "",
        f"- 비교 기준 데이터셋: `reflexivity.log`에서 추출한 dry_run 후보 {candidate_count}건",
        "- 대상 체인: BSC, Solana",
        "- 공통 청산 로직: TP 30 / SL -18 / trailing 5/8 / max hold 12h",
        "- 실행 가정: offline idealized simulation (slippage / gas 미반영)",
        "",
        "| Model | Trades | Win% | Avg PnL | Total PnL | PF | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        metrics = run.metrics
        lines.append(
            f"| {run.model.name} | {metrics.closed_trades} | {metrics.win_rate_pct:.1f}% | "
            f"${metrics.avg_pnl_usd:+.2f} | ${metrics.total_pnl_usd:+.2f} | "
            f"{metrics.profit_factor:.2f} | {run.equity_max_dd_pct:.2f}% |"
        )

    lines.extend(
        [
            "",
            "## Candidate Counts",
            "",
            "| Model | Eligible Signals | Closed Trades | Signals / Hour | Data Missing Events |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run.model.name} | {run.eligible_signals} | {run.metrics.closed_trades} | "
            f"{_candidate_rate(run):.2f} | {run.data_missing_events} |"
        )

    lines.extend(
        [
            "",
            "## Exit Reason Distribution",
            "",
            "| Model | Exit Reasons | Avg Hold (h) |",
            "|---|---|---:|",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run.model.name} | {_exit_reason_summary(run.trades)} | {run.avg_hold_hours:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
        ]
    )
    for run in runs:
        lines.append(f"- **{run.model.name}**: {run.model.description}")
        if run.eligible_signals == 0:
            lines.append("  - 결과: 후보 0건")
        elif run.metrics.closed_trades < 5:
            lines.append(f"  - 결과: 표본 부족 ({run.metrics.closed_trades} closed trades)")
        if run.data_missing_events > 0:
            lines.append(f"  - 데이터 부족 이벤트: {run.data_missing_events}")

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            _recommendation(runs),
            "",
            "## Simulation Limits",
            "",
            "- 이번 비교는 전체 시장 스냅샷 전수조사가 아니라, 기존 dry_run 후보 로그를 동일 표본으로 재평가한 결과입니다.",
            "- `volume_24h_usd`가 없는 구간은 Volume Spike / Dip Buy에 불리하며, 이 경우 `data missing`으로 별도 집계했습니다.",
            "- 실시간 실행에선 slippage, 라우팅, RPC 지연, 포지션 중복 방지 로직 등으로 결과가 달라질 수 있습니다.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    snapshots_by_chain = load_all_snapshots(DATA_DIR, SNAPSHOT_CHAINS)
    candidates = load_candidates_from_log(LOG_FILE)
    runs = [_simulate_model(model, snapshots_by_chain, candidates) for model in MODELS]
    markdown = _build_markdown(runs, len([candidate for candidate in candidates if candidate["chain"] in CHAINS]))
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
