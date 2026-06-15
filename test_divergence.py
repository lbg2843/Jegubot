"""divergence v1 신호 테스트."""
from divergence import buy_ratio, divergence_score, divergence_label, compute


def test_buy_ratio():
    assert buy_ratio(60, 40) == 0.6
    assert buy_ratio(0, 0) is None       # 데이터 없음
    assert buy_ratio(None, 10) is None
    assert buy_ratio(10, 0) == 1.0


def test_score_direction():
    # 평탄 + 매수우위 = 강세(양수), 과열 + 매도우위 = 약세(음수)
    bull = divergence_score(0.0, 70, 30)    # pc=0, buy 0.7 -> +0.2
    bear = divergence_score(20.0, 30, 70)   # pc=20(ext 1.0), buy 0.3 -> -0.2 -1.0 = -1.2
    assert bull is not None and bear is not None
    assert bull > 0 > bear
    assert divergence_score(0.0, None, None) is None


def test_extension_dominates():
    # 같은 매수우위라도 가격이 더 올랐으면 score 낮아야(과열 페널티)
    low = divergence_score(0.0, 60, 40)
    high = divergence_score(20.0, 60, 40)
    assert high < low


def test_label_quadrants():
    assert divergence_label(20.0, 30, 70) == "extended_sell"   # 최악 구간
    assert divergence_label(0.0, 70, 30) == "flat_buy"         # 최선 구간
    assert divergence_label(10.0, 50, 50) == "mid_neutral"
    assert divergence_label(0.0, 0, 0) == "unknown"


def test_compute_from_cand():
    cand = {"price_change_1h_pct": 18.0, "buys_1h": 20, "sells_1h": 80}
    out = compute(cand)
    assert out["divergence_label"] == "extended_sell"
    assert out["buy_ratio"] == 0.2
    assert out["divergence_score"] < 0
    # buys/sells 없으면 None/unknown (안전)
    out2 = compute({"price_change_1h_pct": 5.0})
    assert out2["divergence_score"] is None and out2["divergence_label"] == "unknown"


if __name__ == "__main__":
    test_buy_ratio()
    test_score_direction()
    test_extension_dominates()
    test_label_quadrants()
    test_compute_from_cand()
    print("ok: divergence v1 tests passed")
