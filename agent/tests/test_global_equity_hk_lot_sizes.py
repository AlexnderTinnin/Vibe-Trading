"""HK board lots are set per issuer, so ``round_size`` must not assume 100.

A flat 100-share lot mis-sizes most HK names, and quietly: a target smaller
than one assumed lot rounds to zero and the symbol simply never trades. The
symbols and lot sizes below are synthetic.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from backtest.engines.global_equity import GlobalEquityEngine

LOTS = {"00001.HK": 20, "00002.HK": 100, "00003.HK": 5000}
LOGGER = "backtest.engines.global_equity"


def _engine(
    lots: dict[str, int] | None = LOTS,
    market: str = "hk",
    initial_cash: float = 1_000_000,
) -> GlobalEquityEngine:
    config: dict = {"initial_cash": initial_cash}
    if lots is not None:
        config["hk_lot_sizes"] = lots
    return GlobalEquityEngine(config, market=market)


def _sized(engine: GlobalEquityEngine, symbol: str, raw: float) -> float:
    engine._active_symbol = symbol
    return engine.round_size(raw, price=10.0)


@pytest.mark.parametrize(
    ("symbol", "raw", "expected"),
    [
        ("00001.HK", 99, 80),  # lot 20: four lots, where a 100 grid gives 0
        ("00001.HK", 19, 0),  # below one real lot
        ("00002.HK", 250, 200),  # lot 100 unchanged
        ("00003.HK", 12_345, 10_000),  # lot 5000
    ],
)
def test_round_size_uses_the_symbols_own_lot(
    symbol: str, raw: float, expected: float
) -> None:
    assert _sized(_engine(), symbol, raw) == expected


@pytest.mark.parametrize("written", ["1.HK", "0001.HK", " 00001.hk "])
def test_table_keys_match_however_the_code_is_padded(written: str) -> None:
    engine = _engine({written: 20})

    assert _sized(engine, "00001.HK", 99) == 80
    assert _sized(engine, "1.HK", 99) == 80
    assert engine.lot_assumptions == {}


def test_unlisted_symbol_falls_back_and_reaches_the_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _engine()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _sized(engine, "00009.HK", 250) == 200
        assert _sized(engine, "00009.HK", 350) == 300

    assert engine._engine_diagnostics() == {
        "lot_assumptions": [{"symbol": "0009.HK", "field": "board_lot", "value": 100}]
    }
    assert sum("0009.HK" in r.getMessage() for r in caplog.records) == 1


def test_non_positive_lot_is_treated_as_missing() -> None:
    engine = _engine({"00001.HK": 0})

    assert _sized(engine, "00001.HK", 250) == 200
    assert engine.lot_assumptions == {"0001.HK": 100}


def test_without_a_table_sizing_and_output_are_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _engine(lots=None)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _sized(engine, "00001.HK", 99) == 0
        assert _sized(engine, "00002.HK", 250) == 200

    assert engine._engine_diagnostics() == {}
    assert not caplog.records


def test_non_hk_markets_ignore_the_table() -> None:
    us = _engine(market="us")
    ca = _engine(market="ca")

    assert _sized(us, "AAPL", 59.4321) == 59.43
    assert _sized(ca, "SHOP.TO", 59.9) == 59.0
    assert us._engine_diagnostics() == {} and ca._engine_diagnostics() == {}


def test_a_rebalance_fills_each_symbol_on_its_own_lot() -> None:
    """Through the engine's own sizing path, not just ``round_size``."""
    engine = _engine({"00001.HK": 20, "00002.HK": 500}, initial_cash=100_000)
    dates = pd.date_range("2026-01-05", periods=2, freq="B")
    prices = {"00001.HK": [300.0, 300.0], "00002.HK": [3.0, 3.0]}
    data_map = {
        symbol: pd.DataFrame({"open": values, "close": values}, index=dates)
        for symbol, values in prices.items()
    }
    weights = pd.DataFrame(
        {"00001.HK": [0.0, 0.1], "00002.HK": [0.0, 0.1]}, index=dates
    )

    engine._execute_bars(
        dates, data_map, pd.DataFrame(prices, index=dates), weights, list(prices)
    )

    opened = {
        fill.symbol: abs(fill.signed_quantity)
        for fill in engine.fill_records
        if fill.action == "open"
    }
    assert opened["00001.HK"] > 0 and opened["00001.HK"] % 20 == 0
    assert opened["00002.HK"] > 0 and opened["00002.HK"] % 500 == 0
    assert engine.lot_assumptions == {}
