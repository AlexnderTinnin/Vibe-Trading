"""Futu position rows carry both cost bases and the split of their P&L.

The fields come from ``PositionListQuery.unpack_rsp`` in the SDK module
``futu/trade/trade_query.py``: ``average_cost`` (``averageCostPrice``),
``pl_ratio_avg_cost`` (``averagePlRatio``), ``unrealized_pl``
(``unrealizedPL``) and ``realized_pl`` (``realizedPL``). That module fills a
field the response does not carry with ``NoneDataValue``, the string
``"N/A"``, rather than omitting the key.

Field semantics: https://openapi.futunn.com/futu-api-doc/en/trade/get-position-list.html
Average cost and unrealized P&L are unavailable for securities paper accounts.

All rows are synthetic: invented symbols and values in the shape the SDK
returns.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.portfolio.fx import build_rates
from src.portfolio.normalization import normalize_position, value_position
from src.trading.connectors.futu import sdk as futu_sdk

pytestmark = pytest.mark.unit

#: Synthetic rates: 8 HKD and 7 CNY per USD.
RATES = build_rates(usd_cny=Decimal("7"), usd_hkd=Decimal("8"))

NEW_KEYS = ("average_cost", "pl_ratio_avg_cost", "unrealized_pl", "realized_pl")

_BASE = {"position_side": "LONG", "position_market": "HK", "currency": "HKD"}

# Realized gains have pushed the diluted cost below zero, so the ratio on that
# basis is filled with 0.0 while the average-cost pair still reads normally.
DILUTED_COST_WENT_NEGATIVE = {
    **_BASE,
    "code": "HK.SYNTHA",
    "qty": 300.0,
    "can_sell_qty": 300.0,
    "cost_price": -1.50,
    "average_cost": 20.00,
    "market_val": 6600.0,
    "pl_ratio": 0.0,
    "pl_ratio_avg_cost": 10.00,
    "pl_val": 7050.0,
    "unrealized_pl": 600.0,
    "realized_pl": 6450.0,
}

# A large unrealized loss offset by an earlier realized gain.
PL_VAL_MIXES_REALIZED = {
    **_BASE,
    "code": "HK.SYNTHB",
    "qty": 2000.0,
    "can_sell_qty": 2000.0,
    "cost_price": 9.30,
    "average_cost": 12.00,
    "market_val": 18000.0,
    "pl_ratio": -3.23,
    "pl_ratio_avg_cost": -25.00,
    "pl_val": -600.0,
    "unrealized_pl": -6000.0,
    "realized_pl": 5400.0,
}

# The SDK reports none of the four fields for this row.
NEW_FIELDS_UNREPORTED = {
    **_BASE,
    "code": "HK.SYNTHC",
    "qty": 100.0,
    "can_sell_qty": 100.0,
    "cost_price": 10.00,
    "market_val": 800.0,
    "pl_ratio": -20.00,
    "pl_val": -200.0,
    **{key: "N/A" for key in NEW_KEYS},
}


def test_both_cost_bases_are_carried() -> None:
    """Without ``average_cost`` a negative ``cost_price`` reads as a bad value."""
    row = futu_sdk._position_to_dict(DILUTED_COST_WENT_NEGATIVE)

    assert row["cost_price"] == -1.50
    assert row["average_cost"] == 20.00


def test_both_pl_ratios_are_carried() -> None:
    """``pl_ratio`` 0.0 here means "cannot be computed", not "flat"."""
    row = futu_sdk._position_to_dict(DILUTED_COST_WENT_NEGATIVE)

    assert row["pl_ratio"] == 0.0
    assert row["pl_ratio_avg_cost"] == 10.00


def test_pl_val_is_carried_with_its_realized_and_unrealized_parts() -> None:
    row = futu_sdk._position_to_dict(PL_VAL_MIXES_REALIZED)

    assert row["pl_val"] == -600.0
    assert row["unrealized_pl"] == -6000.0
    assert row["realized_pl"] == 5400.0


def test_original_keys_are_kept() -> None:
    row = futu_sdk._position_to_dict(DILUTED_COST_WENT_NEGATIVE)

    assert row["code"] == "HK.SYNTHA"
    assert row["qty"] == 300.0
    assert row["can_sell_qty"] == 300.0
    assert row["market_val"] == 6600.0
    assert row["pl_val"] == 7050.0
    assert row["position_side"] == "LONG"
    assert row["market"] == "HK"
    assert row["currency"] == "HKD"


def test_unreported_new_fields_become_none() -> None:
    row = futu_sdk._position_to_dict(NEW_FIELDS_UNREPORTED)

    for key in NEW_KEYS:
        assert row[key] is None


def test_reported_unrealized_pl_is_the_valued_pnl() -> None:
    """Portfolio valuation reads the broker's unrealized P&L when it is present."""
    row = normalize_position("futu", futu_sdk._position_to_dict(PL_VAL_MIXES_REALIZED))

    valued = value_position(row, rates=RATES)

    assert valued["unrealized_pnl_usd"] == pytest.approx(-750.0)


def test_missing_average_cost_and_unrealized_pl_stay_unavailable() -> None:
    """Diluted cost alone cannot establish the unrealized part of P&L."""
    row = normalize_position("futu", futu_sdk._position_to_dict(NEW_FIELDS_UNREPORTED))

    valued = value_position(row, rates=RATES)

    assert valued["unrealized_pnl_usd"] is None
    assert valued["cost_price"] is None


def test_portfolio_displays_average_purchase_cost_not_diluted_cost() -> None:
    row = normalize_position("futu", futu_sdk._position_to_dict(DILUTED_COST_WENT_NEGATIVE))
    assert row["cost_price"] == 20.0
    assert value_position(row, rates=RATES)["unrealized_pnl_usd"] == pytest.approx(75.0)


def test_average_cost_can_price_pnl_when_broker_does_not_report_it() -> None:
    payload = {**PL_VAL_MIXES_REALIZED, "unrealized_pl": "N/A"}
    row = normalize_position("futu", futu_sdk._position_to_dict(payload))
    assert row["cost_price"] == 12.0
    assert value_position(row, rates=RATES)["unrealized_pnl_usd"] == pytest.approx(-750.0)
