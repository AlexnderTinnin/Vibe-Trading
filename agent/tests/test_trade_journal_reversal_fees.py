"""A reversal's fee is shared by its closing and opening quantities."""

from decimal import Decimal

import pandas as pd
import pytest

from src.tools.trade_journal_tool import _compute_profile, pair_trades_fifo


def _frame(rows):
    dates = pd.date_range("2026-01-02 10:00", periods=len(rows), freq="h")
    return pd.DataFrame(
        [
            {
                "datetime": dt,
                "symbol": "AAPL.US",
                "name": "Fixture",
                "side": side,
                "quantity": qty,
                "price": price,
                "amount": qty * price,
                "fee": fee,
                "market": "us",
            }
            for dt, (side, qty, price, fee) in zip(dates, rows)
        ]
    )


def _net_cash(rows):
    assert sum((1 if side == "buy" else -1) * qty for side, qty, _, _ in rows) == 0
    return sum(
        (Decimal(-1) if side == "buy" else Decimal(1))
        * Decimal(str(qty))
        * Decimal(str(price))
        - Decimal(str(fee))
        for side, qty, price, fee in rows
    )


def _reversal_rows(short_first, fee, shape):
    opening, reversal, closing = (
        ("sell", "buy", "sell") if short_first else ("buy", "sell", "buy")
    )
    reversal_price, close_price = (90, 95) if short_first else (110, 105)
    entries = (
        [(opening, 10, 100, 2)]
        if shape != "multiple_lots"
        else [(opening, 4, 100, 0.8), (opening, 6, 100, 1.2)]
    )
    exits = (
        [(closing, 5, close_price, 1)]
        if shape != "partial_exits"
        else [(closing, 2, close_price, 0.4), (closing, 3, close_price, 0.6)]
    )
    return entries + [(reversal, 15, reversal_price, fee)] + exits


@pytest.mark.parametrize("short_first", [False, True])
@pytest.mark.parametrize("fee", [3, 0])
@pytest.mark.parametrize("shape", ["single_lot", "multiple_lots", "partial_exits"])
def test_flat_reversal_pnl_matches_net_cash(short_first, fee, shape):
    rows = _reversal_rows(short_first, fee, shape)

    trips = pair_trades_fifo(_frame(rows))

    expected_first = "short" if short_first else "long"
    assert trips[0]["side"] == expected_first
    assert trips[-1]["side"] != expected_first
    assert all(trip["currency"] == "USD" for trip in trips)
    assert sum(Decimal(str(trip["pnl"])) for trip in trips) == _net_cash(rows)


@pytest.mark.parametrize("short_first", [False, True])
def test_reversal_rebate_is_allocated_once(short_first):
    rows = _reversal_rows(short_first, -3, "single_lot")

    trips = pair_trades_fifo(_frame(rows))

    assert sum(Decimal(str(trip["pnl"])) for trip in trips) == _net_cash(rows)


@pytest.mark.parametrize("short_first", [False, True])
def test_correct_fee_preserves_small_net_winner_in_profile(short_first):
    rows = _reversal_rows(short_first, 3, "single_lot")
    # The residual lot earns 2.50 gross: its 1.00 entry fee and 1.00 exit
    # fee leave +0.50. Reusing the whole 3.00 reversal fee makes it -1.50.
    side, qty, _, fee = rows[-1]
    rows[-1] = (side, qty, 90.5 if short_first else 109.5, fee)
    rows += [("buy", 2, 100, 1), ("sell", 2, 97, 1)]

    profile = _compute_profile(_frame(rows))

    assert [t["pnl"] for t in profile["roundtrips_sample"]] == [96.0, 0.5, -8.0]
    assert profile["total_pnl"] == float(_net_cash(rows)) == 88.5
    assert profile["win_rate"] == 0.6667
    assert profile["profit_loss_ratio"] == 6.03  # ((96 + .5) / 2) / 8
