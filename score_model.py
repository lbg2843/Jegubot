from dataclasses import dataclass
from typing import Optional


@dataclass
class ScoreBreakdown:
    momentum: float
    tx_quality: float
    liquidity: float
    buy_pressure: float
    overheated_penalty: float
    final_score: float
    passed: bool


@dataclass
class MultiScoreBreakdown:
    score_v1_reflexivity: ScoreBreakdown
    score_v2_liquidity: float
    score_v3_volume: float
    score_v4_density: float
    liquidity_velocity_raw: Optional[float]
    volume_acceleration_raw: Optional[float]
    txns_per_hour_raw: Optional[int]
    tx_to_liq_ratio_raw: Optional[float]


def compute_momentum_score(change_1h_pct: float) -> float:
    if change_1h_pct < 5:
        return 0.0
    if change_1h_pct < 8:
        return (change_1h_pct - 5) / 3 * 0.7
    if change_1h_pct <= 20:
        return 1.0
    if change_1h_pct <= 35:
        return 1.0 - (change_1h_pct - 20) / 15 * 0.5
    return 0.3


def compute_tx_quality_score(avg_tx_usd: float) -> float:
    if avg_tx_usd < 50:
        return 0.0
    if avg_tx_usd < 150:
        return (avg_tx_usd - 50) / 100 * 0.7
    if avg_tx_usd < 500:
        return 0.7 + (avg_tx_usd - 150) / 350 * 0.3
    return 1.0


def compute_liquidity_score(liq_usd: float) -> float:
    if liq_usd < 30_000:
        return 0.0
    if liq_usd < 100_000:
        return (liq_usd - 30_000) / 70_000 * 0.8
    if liq_usd <= 500_000:
        return 1.0
    if liq_usd <= 2_000_000:
        return 1.0 - (liq_usd - 500_000) / 1_500_000 * 0.4
    return 0.4


def compute_buy_pressure_score(buys: int, sells: int) -> float:
    total = buys + sells
    if total == 0:
        return 0.0
    ratio = buys / total
    if ratio < 0.5:
        return 0.0
    if ratio <= 0.7:
        return (ratio - 0.5) / 0.2
    return 1.0


def compute_overheated_penalty(change_24h_pct: float) -> float:
    if change_24h_pct < 100:
        return 0.0
    if change_24h_pct <= 300:
        return (change_24h_pct - 100) / 200 * 0.3
    if change_24h_pct <= 500:
        return 0.3 + (change_24h_pct - 300) / 200 * 0.3
    return 1.0


def compute_liquidity_velocity_score(current_liq: float, liq_1h_ago: float | None) -> float:
    if liq_1h_ago is None or liq_1h_ago <= 0:
        return 0.5

    velocity = (current_liq - liq_1h_ago) / liq_1h_ago

    if velocity < -0.20:
        return 0.0
    if velocity < -0.05:
        return 0.2
    if velocity < 0.05:
        return 0.5
    if velocity < 0.20:
        return 0.8
    if velocity < 0.50:
        return 1.0
    return 0.9


def compute_volume_acceleration_score(volume_1h: float, volume_24h: float) -> float:
    if volume_24h is None or volume_24h <= 0:
        return 0.0

    expected_hourly = volume_24h / 24
    if expected_hourly <= 0:
        return 0.0

    accel = volume_1h / expected_hourly

    if accel < 1.0:
        return 0.0
    if accel < 2.0:
        return 0.3
    if accel < 4.0:
        return 0.7
    if accel < 8.0:
        return 1.0
    return 0.85


def compute_trade_density_score(txns_1h: int, liquidity_usd: float, avg_tx_size_usd: float) -> float:
    if txns_1h < 10 or liquidity_usd <= 0:
        return 0.0

    count_score = min(txns_1h / 200, 1.0)

    if avg_tx_size_usd <= 0:
        quality_score = 0.0
    else:
        ratio = avg_tx_size_usd / liquidity_usd
        if ratio < 0.0001:
            quality_score = 0.2
        elif ratio < 0.0005:
            quality_score = 0.6
        elif ratio <= 0.01:
            quality_score = 1.0
        elif ratio <= 0.05:
            quality_score = 0.7
        else:
            quality_score = 0.3

    return round((count_score * 0.5 + quality_score * 0.5), 4)


def evaluate_candidate(snapshot: dict, weights: dict, threshold: float) -> ScoreBreakdown:
    m = compute_momentum_score(snapshot.get("price_change_1h_pct", 0) or 0)
    t = compute_tx_quality_score(snapshot.get("avg_tx_size_usd", 0) or 0)
    l = compute_liquidity_score(snapshot.get("liquidity_usd", 0) or 0)
    b = compute_buy_pressure_score(
        snapshot.get("buys_1h", 0) or 0,
        snapshot.get("sells_1h", 0) or 0,
    )
    p = compute_overheated_penalty(snapshot.get("price_change_24h_pct", 0) or 0)

    final = (
        weights["momentum"] * m
        + weights["tx_quality"] * t
        + weights["liquidity"] * l
        + weights["buy_pressure"] * b
        - weights["penalty_overheated"] * p
    )

    return ScoreBreakdown(
        momentum=m,
        tx_quality=t,
        liquidity=l,
        buy_pressure=b,
        overheated_penalty=p,
        final_score=round(final, 4),
        passed=final >= threshold,
    )


def evaluate_multi(
    snapshot: dict,
    prev_snapshot: dict | None,
    weights: dict,
    threshold: float,
) -> MultiScoreBreakdown:
    v1 = evaluate_candidate(snapshot, weights, threshold)

    current_liq = snapshot.get("liquidity_usd", 0) or 0
    liq_1h_ago = (prev_snapshot or {}).get("liquidity_usd") if prev_snapshot else None
    volume_1h = snapshot.get("volume_1h_usd", 0) or 0
    volume_24h = snapshot.get("volume_24h_usd", 0) or 0
    txns_1h = snapshot.get("txns_1h", 0) or 0
    avg_tx_size_usd = snapshot.get("avg_tx_size_usd", 0) or 0

    v2 = compute_liquidity_velocity_score(current_liq, liq_1h_ago)
    v3 = compute_volume_acceleration_score(volume_1h, volume_24h)
    v4 = compute_trade_density_score(txns_1h, current_liq, avg_tx_size_usd)

    liq_vel_raw = (
        ((current_liq - liq_1h_ago) / liq_1h_ago)
        if liq_1h_ago is not None and liq_1h_ago > 0
        else None
    )
    vol_accel_raw = (
        (volume_1h / (volume_24h / 24))
        if volume_24h is not None and volume_24h > 0
        else None
    )
    tx_to_liq_raw = (avg_tx_size_usd / current_liq) if current_liq > 0 and avg_tx_size_usd > 0 else None

    return MultiScoreBreakdown(
        score_v1_reflexivity=v1,
        score_v2_liquidity=round(v2, 4),
        score_v3_volume=round(v3, 4),
        score_v4_density=round(v4, 4),
        liquidity_velocity_raw=liq_vel_raw,
        volume_acceleration_raw=vol_accel_raw,
        txns_per_hour_raw=int(txns_1h),
        tx_to_liq_ratio_raw=tx_to_liq_raw,
    )
