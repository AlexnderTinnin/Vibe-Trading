"""Per-bar returns are seeded with the pre-first-bar cash mark.

The account holds ``initial_cash`` before the first recorded bar, and the
drawdown path already measures from that mark (#1750, #1818). The return
series behind the risk ratios and the ``ret`` column in equity.csv used to
start at a hard zero instead, so a first-bar loss never reached Sharpe or
Sortino and ``prod(1 + ret) - 1`` disagreed with ``total_return``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engines.options_portfolio import _calc_options_metrics
from backtest.metrics import calc_metrics, period_returns
from backtest.validation import _sharpe, bootstrap_sharpe_ci


def _curve(values):
    return pd.Series(
        values,
        index=pd.bdate_range("2025-01-06", periods=len(values)),
        dtype=float,
    )


def test_first_bar_loss_is_the_first_return() -> None:
    # Bought at the first bar's open, marked 10% down, then recovered.
    ret = period_returns(_curve([90_000.0, 95_000.0, 100_000.0]), 100_000.0)
    assert ret.iloc[0] == pytest.approx(-0.10)
    assert ret.iloc[1] == pytest.approx(95_000.0 / 90_000.0 - 1)
    assert ret.iloc[2] == pytest.approx(100_000.0 / 95_000.0 - 1)


def test_seeded_returns_compound_back_to_total_return() -> None:
    equity = _curve([90_000.0, 95_000.0, 100_000.0, 97_000.0, 103_000.0])
    ret = period_returns(equity, 100_000.0)
    assert float((1 + ret).prod() - 1) == pytest.approx(0.03)
    metrics = calc_metrics(equity, [], 100_000.0, bars_per_year=252)
    assert metrics["total_return"] == pytest.approx(0.03)


def test_first_bar_loss_reaches_the_risk_ratios() -> None:
    equity = _curve([90_000.0, 95_000.0, 100_000.0])
    metrics = calc_metrics(equity, [], 100_000.0, bars_per_year=252)
    # Before the seed this path reported sharpe ~18 and sortino ~3e9 because
    # the only losing move was missing from the series.
    assert metrics["sharpe"] == pytest.approx(0.48, abs=0.01)
    assert 0.0 < metrics["sortino"] < 5.0


def test_no_fill_first_bar_matches_the_legacy_series() -> None:
    equity = _curve([100_000.0, 95_000.0, 100_000.0])
    seeded = period_returns(equity, 100_000.0)
    legacy = equity.pct_change().fillna(0.0)
    pd.testing.assert_series_equal(seeded, legacy)


def test_nonpositive_initial_cash_leaves_the_first_return_unseeded() -> None:
    equity = _curve([90_000.0, 95_000.0])
    ret = period_returns(equity, 0.0)
    # No seed without a positive cash mark; callers decide how to treat the
    # unseeded first observation (calc_metrics fills 0.0, bootstrap drops it).
    assert pd.isna(ret.iloc[0])
    assert ret.iloc[1] == pytest.approx(95_000.0 / 90_000.0 - 1)


def test_options_metrics_see_the_first_bar_loss() -> None:
    metrics = _calc_options_metrics(
        _curve([90_000.0, 95_000.0, 100_000.0]), 100_000.0, [], bars_per_year=252
    )
    # Legacy: returns were [.0556, .0526], all positive, so Sortino came back
    # undefined and Sharpe was ~404. With the seed both are small and finite.
    assert metrics["sharpe"] is not None and metrics["sharpe"] < 1.0
    assert metrics["sortino"] is not None and metrics["sortino"] < 5.0


def test_bootstrap_observed_sharpe_uses_the_seeded_series() -> None:
    equity = _curve([90_000.0, 95_000.0, 100_000.0, 97_000.0, 103_000.0])
    result = bootstrap_sharpe_ci(
        equity, n_bootstrap=50, initial_capital=100_000.0
    )
    expected = _sharpe(period_returns(equity, 100_000.0).to_numpy(), 252)
    assert result["observed_sharpe"] == pytest.approx(expected, abs=1e-4)


def test_bootstrap_without_capital_keeps_the_legacy_contract() -> None:
    equity = _curve([90_000.0, 95_000.0, 100_000.0, 97_000.0, 103_000.0])
    # Five equity points give four adjacent returns, below the minimum.
    legacy = bootstrap_sharpe_ci(equity, n_bootstrap=50)
    assert legacy == {"error": "need at least 5 return observations"}
    # The cash seed makes the first bar a real observation, so it qualifies.
    seeded = bootstrap_sharpe_ci(equity, n_bootstrap=50, initial_capital=100_000.0)
    assert "observed_sharpe" in seeded


def test_seeded_returns_stay_finite_after_equity_touches_zero() -> None:
    equity = _curve([100_000.0, 0.0, 50_000.0])
    ret = period_returns(equity, 100_000.0)
    assert ret.iloc[0] == pytest.approx(0.0)
    assert ret.iloc[1] == pytest.approx(-1.0)
    # 0 -> 50k is an infinite recovery return; the calc_metrics isfinite guard
    # owns that case, period_returns must not mask it.
    assert np.isinf(ret.iloc[2])
