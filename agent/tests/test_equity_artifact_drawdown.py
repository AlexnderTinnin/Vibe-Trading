"""The equity.csv drawdown column must agree with the reported max drawdown.

``calc_metrics`` measures drawdown from ``initial_cash``, which the account holds
before the first recorded bar. The equity artifact, which the Web UI plots as
Drawdown%, took its high-water mark from the first recorded bar instead, so a
loss on that bar never showed in it. That bar trades whenever a warm-up boundary
is declared: the last warm-up signal fills at its open.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backtest.engines.base import BaseEngine


class _Engine(BaseEngine):
    """Frictionless engine: only the drawdown bookkeeping is under test."""

    def can_execute(self, symbol, direction, bar):
        return True

    def round_size(self, raw_size, price):
        return float(raw_size)

    def calc_commission(self, size, price, direction, is_open):
        return 0.0

    def apply_slippage(self, price, direction):
        return price


class _Loader:
    def __init__(self, data_map):
        self._data_map = data_map

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        return self._data_map


class _AlwaysLong:
    def generate(self, data):
        return {code: pd.Series(1.0, index=frame.index) for code, frame in data.items()}


def test_equity_csv_drawdown_counts_a_loss_on_the_first_bar(tmp_path: Path) -> None:
    dates = pd.bdate_range("2025-01-06", periods=5)
    # Two warm-up bars, then the first evaluated bar opens at 100 and closes at 90.
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 95.0, 100.0],
            "close": [100.0, 100.0, 90.0, 95.0, 100.0],
        },
        index=dates,
    )
    metrics = _Engine({"initial_cash": 100_000.0}).run_backtest(
        {
            "codes": ["AAA"],
            "start_date": str(dates[0].date()),
            "end_date": str(dates[-1].date()),
            "warmup_bars": 2,
        },
        _Loader({"AAA": frame}),
        _AlwaysLong(),
        tmp_path,
    )
    equity = pd.read_csv(tmp_path / "artifacts" / "equity.csv", index_col="timestamp")

    # The whole account bought at 100 and is marked at 90: 10% below the cash.
    assert equity["equity"].iloc[0] == pytest.approx(90_000.0)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)
    assert equity["drawdown"].tolist() == pytest.approx([-0.10, -0.05, 0.0])
    assert equity["drawdown"].min() == pytest.approx(metrics["max_drawdown"])


def test_equity_csv_ret_column_is_seeded_with_initial_cash(tmp_path: Path) -> None:
    dates = pd.bdate_range("2025-01-06", periods=5)
    # Same fill pattern as the drawdown test: the first evaluated bar loses 10%.
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 95.0, 100.0],
            "close": [100.0, 100.0, 90.0, 95.0, 100.0],
        },
        index=dates,
    )
    metrics = _Engine({"initial_cash": 100_000.0}).run_backtest(
        {
            "codes": ["AAA"],
            "start_date": str(dates[0].date()),
            "end_date": str(dates[-1].date()),
            "warmup_bars": 2,
        },
        _Loader({"AAA": frame}),
        _AlwaysLong(),
        tmp_path,
    )
    equity = pd.read_csv(tmp_path / "artifacts" / "equity.csv", index_col="timestamp")

    # Row by row the ret column must agree with the drawdown column, and the
    # series must compound back to the reported total return.
    assert equity["ret"].iloc[0] == pytest.approx(-0.10)
    compounded = float((1 + equity["ret"]).prod() - 1)
    assert compounded == pytest.approx(metrics["total_return"])
