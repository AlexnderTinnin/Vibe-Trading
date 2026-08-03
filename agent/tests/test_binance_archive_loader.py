"""Regression tests for the official Binance archive loader."""

from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

import backtest.loaders.binance_archive as archive_loader

pytestmark = pytest.mark.unit


def _zip_csv(open_time: int) -> bytes:
    row = f"{open_time},100,102,99,101,12,0,0,1,0,0,0\n"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-1d.csv", row)
    return payload.getvalue()


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (1_500_000_000_000, "2017-07-14 02:40:00+00:00"),
        (1_750_000_000_000_000, "2025-06-15 15:06:40+00:00"),
    ],
)
def test_archive_parser_supports_millisecond_and_microsecond_timestamps(
    timestamp: int,
    expected: str,
) -> None:
    frame = archive_loader._parse_archive_zip(_zip_csv(timestamp), filename="test.zip")

    assert len(frame) == 1
    assert str(frame.index[0]) == expected
    assert frame.iloc[0].to_dict() == {
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 12,
    }


def test_archive_loader_maps_swap_symbol_and_fetches_intersecting_months(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_month(symbol: str, timeframe: str, month: str) -> pd.DataFrame:
        calls.append((symbol, timeframe, month))
        index = pd.date_range(f"{month}-01", periods=31, freq="1D", tz="UTC")
        return pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
            },
            index=index,
        )

    def passthrough_cache(*, fetch, start_ms, end_ms, **kwargs):  # noqa: ANN001, ANN003
        return fetch(start_ms, end_ms)

    monkeypatch.setattr(archive_loader, "_download_archive_month", fake_month)
    monkeypatch.setattr(
        archive_loader,
        "incremental_cached_loader_fetch",
        passthrough_cache,
    )

    result = archive_loader.DataLoader().fetch(
        ["BTC-USDT-SWAP"],
        "2017-08-17",
        "2017-09-02",
        interval="1D",
    )

    frame = result["BTC-USDT-SWAP"]
    assert calls == [
        ("BTCUSDT", "1d", "2017-08"),
        ("BTCUSDT", "1d", "2017-09"),
    ]
    assert frame.index.min() == pd.Timestamp("2017-08-17", tz="UTC")
    assert frame.index.max() == pd.Timestamp("2017-09-02", tz="UTC")


def test_archive_loader_rejects_unsupported_interval() -> None:
    with pytest.raises(ValueError, match="unsupported Binance archive interval"):
        archive_loader.DataLoader().fetch(
            ["BTC-USDT-SWAP"],
            "2026-01-01",
            "2026-01-02",
            interval="2H",
        )


def test_archive_range_falls_back_to_daily_files_for_unpublished_month(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        archive_loader,
        "_download_archive_month",
        lambda symbol, timeframe, month: None,
    )

    def fake_day(symbol: str, timeframe: str, day: str) -> pd.DataFrame:
        calls.append(day)
        index = pd.DatetimeIndex([pd.Timestamp(day, tz="UTC")])
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            index=index,
        )

    monkeypatch.setattr(archive_loader, "_download_archive_day", fake_day)
    start_ms = int(pd.Timestamp("2026-08-01", tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp("2026-08-04", tz="UTC").timestamp() * 1000)

    frame = archive_loader.DataLoader._fetch_range("BTCUSDT", "1d", start_ms, end_ms)

    assert sorted(calls) == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert frame is not None and len(frame) == 3
