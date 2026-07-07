import math

import pytest

from sentinel.options.pricing import bsm_price, crr_price, intrinsic, parity_gap


def test_bsm_golden_values() -> None:
    call = bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "call")
    put = bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "put")

    assert call == pytest.approx(10.4506, abs=1e-3)
    assert put == pytest.approx(5.5735, abs=1e-3)


def test_intrinsic_and_bsm_edges() -> None:
    assert intrinsic(105.0, 100.0, "call") == 5.0
    assert intrinsic(95.0, 100.0, "put") == 5.0
    assert bsm_price(105.0, 100.0, 0.0, 0.05, 0.0, 0.20, "call") == 5.0

    discounted = 100.0 * math.exp(-0.0) - 100.0 * math.exp(-0.05)
    assert bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.0, "call") == pytest.approx(
        discounted
    )


def test_put_call_parity_grid() -> None:
    for S in (80.0, 100.0, 125.0):
        for K in (90.0, 100.0, 110.0):
            for T in (0.25, 1.0, 2.0):
                call = bsm_price(S, K, T, 0.05, 0.01, 0.25, "call")
                put = bsm_price(S, K, T, 0.05, 0.01, 0.25, "put")
                assert parity_gap(S, K, T, 0.05, 0.01, call, put) == pytest.approx(
                    0.0, abs=1e-9
                )


def test_crr_european_matches_bsm_and_american_put_exceeds_european() -> None:
    bsm_call = bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "call")
    crr_call = crr_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "call", american=False)
    assert crr_call == pytest.approx(bsm_call, rel=5e-4)

    european_put = crr_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "put", american=False)
    american_put = crr_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "put", american=True)
    assert american_put >= european_put
    assert american_put > 5.5735
    assert american_put == pytest.approx(6.090, abs=0.05)


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "straddle")
