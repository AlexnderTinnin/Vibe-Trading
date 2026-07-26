"""Point-in-time safety tests for the bundled SMC signal template."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _load_example_module():
    path = (
        Path(__file__).parents[1]
        / "src"
        / "skills"
        / "smc"
        / "example_signal_engine.py"
    )
    spec = importlib.util.spec_from_file_location("smc_example", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sample_frame(periods: int = 160) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    dates = pd.date_range("2025-01-01", periods=periods, freq="15min")
    close = 100 + np.cumsum(rng.normal(0, 1.2, periods))
    open_ = np.r_[close[0], close[:-1]]
    spread = rng.uniform(0.2, 1.0, periods)
    return pd.DataFrame({
        "open": open_,
        "high": np.maximum(open_, close) + spread,
        "low": np.minimum(open_, close) - spread,
        "close": close,
        "volume": rng.uniform(10, 100, periods),
    }, index=dates)


def test_future_bars_do_not_rewrite_historical_signals() -> None:
    module = _load_example_module()
    engine = module.SignalEngine(swing_length=4)
    full = _sample_frame()
    cutoff = 110

    prefix_signal = engine.generate({"X": full.iloc[:cutoff]})["X"]
    full_signal = engine.generate({"X": full})["X"].iloc[:cutoff]

    pd.testing.assert_series_equal(prefix_signal, full_signal)
    assert prefix_signal.abs().sum() > 0


def test_swing_length_must_be_positive() -> None:
    module = _load_example_module()
    with pytest.raises(ValueError, match="swing_length"):
        module.SignalEngine(swing_length=0)
