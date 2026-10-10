"""Currency labelling of Futu account balances.

A Futu multi-currency account reports ``"N/A"`` as the currency of its funds
row, while the amounts are denominated in the currency the query asked for.
Portfolio valuation rejects an unknown currency, so the label has to come from
the request; an empty label would instead be valued as USD.

All rows and amounts are synthetic. The SDK boundary is mocked: no OpenD
gateway, no network, no credentials.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.portfolio.compatibility import PortfolioContractError
from src.portfolio.fx import build_rates, to_usd
from src.portfolio.normalization import account_total_usd
from src.trading.connectors.futu import sdk as futu_sdk

pytestmark = pytest.mark.unit

#: Synthetic rates: 8 HKD and 7 CNY per USD.
RATES = build_rates(usd_cny=Decimal("7"), usd_hkd=Decimal("8"))


class _FakeFutu:
    RET_OK = 0

    class TrdEnv:
        SIMULATE = "SIMULATE"


class _RecordingTradeCtx:
    """Stands in for ``OpenSecTradeContext`` and records the funds query."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.accinfo_kwargs: dict[str, Any] = {}

    def get_acc_list(self) -> tuple[int, list[dict[str, Any]]]:
        return (0, [{"trd_env": "SIMULATE", "acc_id": 123}])

    def accinfo_query(self, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        self.accinfo_kwargs = kwargs
        return (0, self.rows)

    def close(self) -> None:
        pass


def test_unreported_currency_takes_the_requested_currency() -> None:
    row = {"currency": "N/A", "total_assets": 1600.0, "cash": 800.0}

    assert futu_sdk._account_to_dict(row, "HKD")["currency"] == "HKD"
    assert futu_sdk._account_to_dict(row, "USD")["currency"] == "USD"


def test_broker_reported_currency_is_never_overwritten() -> None:
    row = {"currency": "USD", "total_assets": 1600.0}

    assert futu_sdk._account_to_dict(row, "HKD")["currency"] == "USD"


@pytest.mark.parametrize("reported", [None, "", "N/A"])
@pytest.mark.parametrize("requested", [None, "", "HKD"])
def test_no_row_maps_to_an_empty_currency(
    reported: str | None, requested: str | None
) -> None:
    """An empty currency is valued as USD downstream, so it must not appear."""
    row: dict[str, Any] = {"total_assets": 1600.0}
    if reported is not None:
        row["currency"] = reported

    assert futu_sdk._account_to_dict(row, requested)["currency"] != ""


def test_undetermined_currency_still_fails_valuation_closed() -> None:
    """Without a requested currency there is no label, and valuation refuses."""
    row = futu_sdk._account_to_dict({"currency": "N/A", "total_assets": 1600.0})

    assert row["currency"] == "N/A"
    with pytest.raises(PortfolioContractError):
        to_usd(Decimal("1600"), row["currency"], RATES)


def test_multi_currency_account_values_at_the_requested_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label follows the query, not the connection's market filter."""
    ctx = _RecordingTradeCtx([{"currency": "N/A", "total_assets": 1600.0}])
    monkeypatch.setattr(futu_sdk, "_trade_ctx", lambda cfg: ctx)
    monkeypatch.setattr(futu_sdk, "_require_futu", lambda: _FakeFutu)

    snapshot = futu_sdk.get_account_snapshot(
        futu_sdk.FutuConfig(acc_id=123, filter_trdmarket="US")
    )

    assert ctx.accinfo_kwargs["currency"] == futu_sdk.ACCOUNT_CURRENCY
    assert snapshot["assets"][0]["currency"] == futu_sdk.ACCOUNT_CURRENCY
    assert account_total_usd("futu", snapshot, RATES) == Decimal("200")
