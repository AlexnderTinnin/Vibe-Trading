"""Incremental OHLCV loader for Binance's official public data archive."""

from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import pandas as pd
import requests

from backtest.loaders.base import (
    check_budget,
    incremental_cached_loader_fetch,
    positive_env_float,
    positive_env_int,
    retry_with_budget,
    validate_date_range,
)
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_MONTHLY_ARCHIVE_BASE = "https://data.binance.vision/data/spot/monthly/klines"
_DAILY_ARCHIVE_BASE = "https://data.binance.vision/data/spot/daily/klines"
_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
}
_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)
_REQUEST_TIMEOUT_S = positive_env_float("BINANCE_ARCHIVE_TIMEOUT_S", 20.0)
_FETCH_BUDGET_S = positive_env_float("BINANCE_ARCHIVE_FETCH_BUDGET_S", 240.0)
_MAX_WORKERS = positive_env_int("BINANCE_ARCHIVE_MAX_WORKERS", 6)


@register
class DataLoader:
    """Load immutable Binance spot archives and cache the merged time series."""

    name = "binance_archive"
    markets = {"crypto"}
    requires_auth = False

    def is_available(self) -> bool:
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        try:
            timeframe = _INTERVAL_MAP[interval]
        except KeyError as exc:
            raise ValueError(f"unsupported Binance archive interval: {interval}") from exc
        start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
        end_ms = int(
            (pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)).timestamp()
            * 1000
        )

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            symbol = _archive_symbol(code)
            try:
                frame = incremental_cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=timeframe,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    fields=fields,
                    fetch=lambda range_start, range_end, symbol=symbol: self._fetch_range(
                        symbol,
                        timeframe,
                        range_start,
                        range_end,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not discard the others
                logger.warning("Binance archive failed for %s: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                result[code] = frame
        return result

    @staticmethod
    def _fetch_range(
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> pd.DataFrame | None:
        deadline = time.monotonic() + _FETCH_BUDGET_S
        months = _month_tokens(start_ms, end_ms)

        def load(month: str) -> pd.DataFrame | None:
            label = f"Binance archive {symbol} {timeframe} {month}"
            check_budget(deadline, label, budget_s=_FETCH_BUDGET_S)
            return retry_with_budget(
                lambda: _download_archive_month(symbol, timeframe, month),
                transient=requests.RequestException,
                deadline=deadline,
                label=label,
            )

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(len(months), 1))) as pool:
            monthly = list(zip(months, pool.map(load, months)))
        frames = [frame for _, frame in monthly if frame is not None]
        missing_months = {month for month, frame in monthly if frame is None}
        last_period = pd.Timestamp(end_ms - 1, unit="ms", tz="UTC").tz_localize(None).to_period("M")
        daily_fallback_months = {str(last_period), str(last_period - 1)}
        days = [
            day
            for day in _day_tokens(start_ms, end_ms)
            if day[:7] in missing_months and day[:7] in daily_fallback_months
        ]

        def load_day(day: str) -> pd.DataFrame | None:
            label = f"Binance daily archive {symbol} {timeframe} {day}"
            check_budget(deadline, label, budget_s=_FETCH_BUDGET_S)
            return retry_with_budget(
                lambda: _download_archive_day(symbol, timeframe, day),
                transient=requests.RequestException,
                deadline=deadline,
                label=label,
            )

        if days:
            with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(days))) as pool:
                frames.extend(frame for frame in pool.map(load_day, days) if frame is not None)
        if not frames:
            return None
        merged = pd.concat(frames).sort_index()
        merged = merged.loc[~merged.index.duplicated(keep="last")]
        start = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        end = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        selected = merged.loc[(merged.index >= start) & (merged.index < end)]
        return selected if not selected.empty else None


def _archive_symbol(code: str) -> str:
    clean = str(code or "").strip().upper()
    if clean.endswith("-SWAP"):
        clean = clean.removesuffix("-SWAP")
    symbol = clean.replace("-", "").replace("/", "")
    if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
        raise ValueError(f"invalid Binance archive symbol: {code!r}")
    return symbol


def _month_tokens(start_ms: int, end_ms: int) -> list[str]:
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    last_inclusive = (
        pd.Timestamp(end_ms - 1, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    )
    return [str(period) for period in pd.period_range(start, last_inclusive, freq="M")]


def _day_tokens(start_ms: int, end_ms: int) -> list[str]:
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC").normalize()
    last_inclusive = pd.Timestamp(end_ms - 1, unit="ms", tz="UTC").normalize()
    return [timestamp.strftime("%Y-%m-%d") for timestamp in pd.date_range(start, last_inclusive)]


def _download_archive_month(
    symbol: str,
    timeframe: str,
    month: str,
) -> pd.DataFrame | None:
    filename = f"{symbol}-{timeframe}-{month}.zip"
    url = f"{_MONTHLY_ARCHIVE_BASE}/{symbol}/{timeframe}/{filename}"
    response = requests.get(url, timeout=_REQUEST_TIMEOUT_S)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return _parse_archive_zip(response.content, filename=filename)


def _download_archive_day(
    symbol: str,
    timeframe: str,
    day: str,
) -> pd.DataFrame | None:
    filename = f"{symbol}-{timeframe}-{day}.zip"
    url = f"{_DAILY_ARCHIVE_BASE}/{symbol}/{timeframe}/{filename}"
    response = requests.get(url, timeout=_REQUEST_TIMEOUT_S)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return _parse_archive_zip(response.content, filename=filename)


def _parse_archive_zip(payload: bytes, *, filename: str) -> pd.DataFrame:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"archive CRC failed for {bad_member}")
            members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError("archive must contain exactly one CSV")
            with archive.open(members[0]) as stream:
                frame = pd.read_csv(stream, header=None, names=_COLUMNS)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid Binance archive {filename}") from exc
    if frame.empty:
        return frame

    timestamps = pd.to_numeric(frame["open_time"], errors="coerce")
    unit = "us" if float(timestamps.dropna().median()) >= 100_000_000_000_000 else "ms"
    frame.index = pd.to_datetime(timestamps, unit=unit, utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[["open", "high", "low", "close", "volume"]].dropna().sort_index()
