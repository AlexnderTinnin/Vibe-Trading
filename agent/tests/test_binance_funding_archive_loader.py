"""Regression tests for Binance USD-M funding-rate archives."""

from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from backtest.loaders import binance_funding_archive as funding_loader

pytestmark = pytest.mark.unit


def _zip_csv(timestamp: int, rate: float = 0.0001) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "BTCUSDT-fundingRate.csv",
            "calc_time,funding_interval_hours,last_funding_rate\n"
            f"{timestamp},8,{rate}\n",
        )
    return payload.getvalue()


def test_funding_parser_normalizes_actual_rate() -> None:
    frame = funding_loader._parse_archive_zip(
        _zip_csv(1_700_000_000_000, -0.000125),
        filename="test.zip",
    )

    assert frame.index[0] == pd.Timestamp("2023-11-14 22:13:20", tz="UTC")
    assert frame.iloc[0].to_dict() == {
        "funding_rate": -0.000125,
        "interval_hours": 8.0,
    }


def test_funding_loader_maps_swap_symbol_and_uses_incremental_cache(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_cache(**kwargs):  # noqa: ANN003
        seen.update(kwargs)
        return kwargs["fetch"](kwargs["start_ms"], kwargs["end_ms"])

    def fake_range(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        seen["archive_symbol"] = symbol
        index = pd.DatetimeIndex([pd.Timestamp(start_ms, unit="ms", tz="UTC")])
        return pd.DataFrame({"funding_rate": [0.0001], "interval_hours": [8]}, index=index)

    monkeypatch.setattr(funding_loader, "incremental_cached_loader_fetch", fake_cache)
    monkeypatch.setattr(funding_loader.FundingRateLoader, "_fetch_range", staticmethod(fake_range))

    result = funding_loader.FundingRateLoader().fetch(
        ["BTC-USDT-SWAP"], "2020-01-01", "2020-01-02"
    )

    assert seen["source"] == "binance_funding_archive"
    assert seen["timeframe"] == "8h"
    assert seen["archive_symbol"] == "BTCUSDT"
    assert len(result["BTC-USDT-SWAP"]) == 1
