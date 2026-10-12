"""Per-currency profile totals for mixed-currency journals (#1808).

``_compute_profile`` used to sum the round-trip ``pnl`` column directly, so a
Futu 综合账户 journal with both HK and US legs added HKD and USD as one
number. Round-trips now carry their settlement currency
(:func:`backtest.engines._market_hooks.code_currency`), and money totals are
pooled per currency with no FX conversion — the convention the shadow
backtest already reports (#997/#1310). Single-currency journals keep the
legacy flat-float shape.
"""

from __future__ import annotations

from src.tools.trade_journal_parsers import TradeRecord, records_to_dataframe
from src.tools.trade_journal_tool import _compute_profile, pair_trades_fifo


def _rec(
    dt: str, symbol: str, side: str, qty: float, price: float, fee: float = 0.0
) -> TradeRecord:
    return TradeRecord(
        datetime=dt,
        symbol=symbol,
        name="",
        side=side,
        quantity=qty,
        price=price,
        amount=qty * price,
        fee=fee,
        market="other",
    )


def _df(records: list[TradeRecord]):
    return records_to_dataframe(records)


def test_roundtrips_carry_settlement_currency() -> None:
    df = _df([
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
    ])
    rts = pair_trades_fifo(df)
    assert rts[0]["currency"] == "HKD"


def test_mixed_currency_journal_pools_pnl_per_currency() -> None:
    """The #1808 repro: HKD and USD must never be one headline number."""
    df = _df([
        # HK leg: 1000 HKD win
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
        # US legs: +200 win then -300 loss → USD pool drawdown -300
        _rec("2026-01-03 09:30:00", "AAPL", "buy", 100, 150.0),
        _rec("2026-01-06 09:30:00", "AAPL", "sell", 100, 152.0),
        _rec("2026-01-03 09:30:00", "MSFT", "buy", 100, 100.0),
        _rec("2026-01-09 09:30:00", "MSFT", "sell", 100, 97.0),
    ])
    profile = _compute_profile(df)

    assert profile["total_pnl"] == {"HKD": 1000.0, "USD": -100.0}
    assert profile["max_drawdown"] == {"HKD": 0.0, "USD": -300.0}
    # Ratio metrics stay dimensionless on the full sample.
    assert profile["total_roundtrips"] == 3
    assert profile["win_rate"] == round(2 / 3, 4)
    assert profile["currencies"] == ["HKD", "USD"]
    assert "no FX conversion" in profile["currency_warning"]
    assert profile["total_dividends_by_currency"] == {"HKD": 0.0, "USD": 0.0}


def test_single_currency_journal_keeps_legacy_shape() -> None:
    """One currency → plain floats, no split keys: zero behavior change."""
    df = _df([
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
    ])
    profile = _compute_profile(df)
    assert profile["total_pnl"] == 1000.0
    assert profile["max_drawdown"] == 0.0
    assert isinstance(profile["total_pnl"], float)
    assert "currencies" not in profile
    assert "currency_warning" not in profile


def test_dividends_join_their_own_currency_pool() -> None:
    df = _df([
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
        _rec("2026-01-06 09:30:00", "AAPL", "buy", 100, 150.0),
        _rec("2026-01-09 09:30:00", "AAPL", "sell", 100, 151.0),
        # HKD dividend joins the HKD pool, not a blended sum.
        _div("2026-01-07 09:30:00", "0700.HK", 50.0),
    ])
    profile = _compute_profile(df)
    assert profile["total_pnl"] == {"HKD": 1050.0, "USD": 100.0}
    assert profile["total_dividends"] == 50.0
    assert profile["total_dividends_by_currency"] == {"HKD": 50.0, "USD": 0.0}


def _div(dt: str, symbol: str, amount: float) -> TradeRecord:
    return TradeRecord(
        datetime=dt,
        symbol=symbol,
        name="",
        side="dividend",
        quantity=0.0,
        price=0.0,
        amount=amount,
        fee=0.0,
        market="other",
    )


def test_foreign_dividend_flips_profile_to_its_currency_pool() -> None:
    """A dividend in a currency with no round-trip still joins its own pool.

    The cash is real — pooling it with HKD round-trips (the legacy sum) would
    add USD to HKD, the exact bug #1808 describes. So it both flips the
    profile to the split shape and lands in its own pool.
    """
    df = _df([
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
        _div("2026-01-07 09:30:00", "AAPL", 25.0),
    ])
    profile = _compute_profile(df)
    assert profile["total_pnl"] == {"HKD": 1000.0, "USD": 25.0}
    assert profile["total_dividends"] == 25.0
    assert profile["total_dividends_by_currency"] == {"HKD": 0.0, "USD": 25.0}


def test_unmatched_positions_do_not_break_currency_pooling() -> None:
    """An open USD position alongside closed HKD legs stays single-pool."""
    df = _df([
        _rec("2026-01-02 09:30:00", "0700.HK", "buy", 100, 300.0),
        _rec("2026-01-05 09:30:00", "0700.HK", "sell", 100, 310.0),
        # Never sold → no round-trip, must not flip the profile to mixed.
        _rec("2026-01-06 09:30:00", "AAPL", "buy", 10, 150.0),
    ])
    profile = _compute_profile(df)
    assert profile["total_pnl"] == 1000.0
    assert "currency_warning" not in profile
