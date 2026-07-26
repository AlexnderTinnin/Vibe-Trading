"""Regression tests for the bundled pair-trading signal template."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


def _load_example_module():
    path = (
        Path(__file__).parents[1]
        / "src"
        / "skills"
        / "pair-trading"
        / "example_signal_engine.py"
    )
    spec = importlib.util.spec_from_file_location("pair_trading_example", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pair_position_is_held_through_neutral_band() -> None:
    module = _load_example_module()
    values = [100, 100, 100, 70, 75, 80, 85, 90, 95, 100, 100, 100]
    dates = pd.date_range("2025-01-01", periods=len(values), freq="D")
    first = pd.DataFrame({"close": values}, index=dates)
    second = pd.DataFrame({"close": [100] * len(values)}, index=dates)

    signals = module.SignalEngine(
        lookback=3, entry_z=1.0, exit_z=0.25,
    ).generate({"A": first, "B": second})

    # The entry occurs at index 3. Index 4 is inside the hysteresis band:
    # it must hold the pair rather than flatten after one bar.
    assert signals["A"].iloc[3] == 0.5
    assert signals["A"].iloc[4] == 0.5
    assert signals["B"].iloc[4] == -0.5
