"""eth_macro_filter 레짐 분류 테스트.

경계가 btc_correlation 분석 버킷과 정확히 일치하는지 보장한다.
밴드가 어긋나면 진입시점 태깅과 사후 상관분석이 따로 놀게 된다.
"""
from eth_macro_filter import classify_eth_4h_regime


def test_band_centers():
    assert classify_eth_4h_regime(-5.0) == "strong_down"
    assert classify_eth_4h_regime(-1.0) == "down"
    assert classify_eth_4h_regime(0.0) == "flat"
    assert classify_eth_4h_regime(1.0) == "up"
    assert classify_eth_4h_regime(5.0) == "strong_up"


def test_boundaries_are_lo_inclusive_hi_exclusive():
    # 경계값은 위쪽 밴드의 하한(포함)으로 떨어진다: lo <= x < hi
    assert classify_eth_4h_regime(-2.0) == "down"        # not strong_down
    assert classify_eth_4h_regime(-0.5) == "flat"        # not down
    assert classify_eth_4h_regime(0.5) == "up"           # not flat
    assert classify_eth_4h_regime(2.0) == "strong_up"    # not up


def test_extremes_and_none():
    assert classify_eth_4h_regime(-100.0) == "strong_down"
    assert classify_eth_4h_regime(100.0) == "strong_up"
    assert classify_eth_4h_regime(None) == "unknown"


def test_matches_analysis_buckets():
    # btc_correlation 의 손실 집중 구간(완만한 상승)이 'up'으로 잡히는지 확인.
    # 분석에서 ETH 4h +0.5~+2% 구간이 승률 20%/평균 -9.5% 였던 그 밴드.
    for v in (0.5, 0.73, 1.08, 1.94, 1.99):
        assert classify_eth_4h_regime(v) == "up"


if __name__ == "__main__":
    test_band_centers()
    test_boundaries_are_lo_inclusive_hi_exclusive()
    test_extremes_and_none()
    test_matches_analysis_buckets()
    print("ok: all eth regime tests passed")
