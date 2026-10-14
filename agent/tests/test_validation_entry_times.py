"""Reloaded BaseEngine trades retain entry-based window attribution."""

from pathlib import Path

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from backtest.models import TradeRecord
from backtest.validation import _load_trades, walk_forward_analysis


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("same_day", [False, True])
def test_round_trip_preserves_entry_window(
    tmp_path: Path, direction: int, same_day: bool
) -> None:
    engine = ChinaAEngine({"initial_cash": 1000.0})
    dates = pd.date_range("2026-01-01", periods=10)
    engine.trades = [
        TradeRecord(
            symbol="TEST",
            direction=direction,
            entry_price=100.0,
            exit_price=100.0 + direction * pnl,
            entry_time=dates[entry],
            exit_time=dates[entry if same_day else exit_],
            size=1.0,
            leverage=1.0,
            pnl=pnl,
            pnl_pct=pnl,
            exit_reason="signal",
            holding_bars=0 if same_day else exit_ - entry,
            commission=0.0,
        )
        for entry, exit_, pnl in [(0, 5, 10.0), (1, 7, -5.0), (6, 9, 0.0)]
    ]
    equity = pd.Series([1000, 1010, 1005, 1020, 1015] * 2, index=dates)
    engine._write_artifacts(
        tmp_path,
        {},
        dates,
        equity,
        equity,
        pd.Series(0.0, index=dates),
        pd.DataFrame(index=dates),
        {},
        ["TEST"],
    )

    restored = _load_trades(tmp_path)
    actual = walk_forward_analysis(equity, restored, n_windows=2)
    expected = walk_forward_analysis(equity, engine.trades, n_windows=2)

    assert [w["trades"] for w in actual["windows"]] == [2, 1]
    assert [w["win_rate"] for w in actual["windows"]] == [0.5, 0.0]
    assert actual == expected
    assert [t.entry_time for t in restored] == [t.entry_time for t in engine.trades]
    assert [t.exit_time for t in restored] == [t.exit_time for t in engine.trades]
    assert [t.holding_bars for t in restored] == [t.holding_bars for t in engine.trades]


@pytest.mark.parametrize("event_column", [False, True])
def test_exit_only_artifacts_keep_timestamp_fallback(
    tmp_path: Path, event_column: bool
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    frame = pd.DataFrame(
        {
            "timestamp": ["2026-01-06"],
            "code": ["TEST"],
            "side": ["sell"],
            "price": [105],
            "qty": [1],
            "pnl": [5],
        }
    )
    if event_column:
        frame["event"] = "exit"
    frame.to_csv(artifacts / "trades.csv", index=False)

    trade = _load_trades(tmp_path)[0]

    assert trade.entry_time == trade.exit_time == pd.Timestamp("2026-01-06")


@pytest.mark.parametrize(
    "mismatch", ["symbol", "side", "future", "unmarked", "invalid_date", "timezone"]
)
def test_unmatched_previous_row_does_not_supply_entry_time(
    tmp_path: Path, mismatch: str
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    entry_date = {
        "future": "2026-01-07",
        "invalid_date": "invalid",
        "timezone": "2026-01-01T00:00:00Z",
    }.get(mismatch, "2026-01-01")
    frame = pd.DataFrame(
        {
            "timestamp": [
                entry_date,
                "2026-01-06",
            ],
            "code": ["OTHER" if mismatch == "symbol" else "TEST", "TEST"],
            "side": ["sell" if mismatch == "side" else "buy", "sell"],
            "price": [100, 105],
            "qty": [1, 1],
            "pnl": [0, 5],
            "event": ["unknown" if mismatch == "unmarked" else "entry", "exit"],
        }
    )
    frame.to_csv(artifacts / "trades.csv", index=False)

    trade = _load_trades(tmp_path)[0]

    assert trade.entry_time == trade.exit_time == pd.Timestamp("2026-01-06")
