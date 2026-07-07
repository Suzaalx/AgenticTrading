import pytest

from sentinel.options.greeks import delta, gamma, phi, rho, theta, vega


def test_scalar_greeks_golden_values_and_units() -> None:
    S = 100.0
    K = 100.0
    T = 1.0
    r = 0.05
    q = 0.0
    sigma = 0.20

    assert delta(S, K, T, r, q, sigma, "call") == pytest.approx(0.6368, abs=1e-4)
    assert gamma(S, K, T, r, q, sigma) == pytest.approx(0.018762, abs=1e-6)

    vega_per_1_vol = vega(S, K, T, r, q, sigma)
    assert vega_per_1_vol == pytest.approx(37.524, abs=1e-3)
    assert vega_per_1_vol / 100.0 == pytest.approx(0.37524, abs=1e-5)

    theta_per_year = theta(S, K, T, r, q, sigma, "call")
    assert theta_per_year == pytest.approx(-6.414, abs=1e-3)
    assert theta_per_year / 365.0 == pytest.approx(-0.01757, abs=1e-5)

    rho_per_1_rate = rho(S, K, T, r, q, sigma, "call")
    assert rho_per_1_rate == pytest.approx(53.232, abs=1e-3)
    assert rho_per_1_rate / 100.0 == pytest.approx(0.5323, abs=1e-4)


def test_put_delta_and_greek_edges() -> None:
    assert delta(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "put") == pytest.approx(
        -0.3632, abs=1e-4
    )
    assert gamma(100.0, 100.0, 1.0, 0.05, 0.0, 0.0) == 0.0
    assert vega(100.0, 100.0, 0.0, 0.05, 0.0, 0.20) == 0.0
    assert theta(100.0, 100.0, 0.0, 0.05, 0.0, 0.20, "call") == 0.0
    assert rho(100.0, 100.0, 0.0, 0.05, 0.0, 0.20, "put") == 0.0


def test_phi_helper() -> None:
    assert phi(0.0) == pytest.approx(0.3989422804, abs=1e-10)
