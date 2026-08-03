"""Incremental loader for official Binance USD-M perpetual funding archives."""

from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

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

logger = logging.getLogger(__name__)

_ARCHIVE_BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
_COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")
_REQUEST_TIMEOUT_S = positive_env_float("BINANCE_ARCHIVE_TIMEOUT_S", 20.0)
_FETCH_BUDGET_S = positive_env_float("BINANCE_ARCHIVE_FETCH_BUDGET_S", 240.0)
_MAX_WORKERS = positive_env_int("BINANCE_ARCHIVE_MAX_WORKERS", 6)


class FundingRateLoader:
    """Fetch immutable USD-M funding events into the shared Parquet cache."""

    name = "binance_funding_archive"

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
        end_ms = int(
            (pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000
        )
        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            symbol = _archive_symbol(code)
            try:
                frame = incremental_cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe="8h",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    fields=None,
                    fetch=lambda range_start, range_end, symbol=symbol: self._fetch_range(
                        symbol,
                        range_start,
                        range_end,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not discard the others
                logger.warning("Binance funding archive failed for %s: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                result[code] = frame
        return result

    @staticmethod
    def _fetch_range(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame | None:
        deadline = time.monotonic() + _FETCH_BUDGET_S
        months = _month_tokens(start_ms, end_ms)

        def load(month: str) -> pd.DataFrame | None:
            label = f"Binance funding archive {symbol} {month}"
            check_budget(deadline, label, budget_s=_FETCH_BUDGET_S)
            return retry_with_budget(
                lambda: _download_archive_month(symbol, month),
                transient=requests.RequestException,
                deadline=deadline,
                label=label,
            )

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(len(months), 1))) as pool:
            frames = [frame for frame in pool.map(load, months) if frame is not None]
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
        raise ValueError(f"invalid Binance funding symbol: {code!r}")
    return symbol


def _month_tokens(start_ms: int, end_ms: int) -> list[str]:
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    last = pd.Timestamp(end_ms - 1, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    return [str(period) for period in pd.period_range(start, last, freq="M")]


def _download_archive_month(symbol: str, month: str) -> pd.DataFrame | None:
    filename = f"{symbol}-fundingRate-{month}.zip"
    url = f"{_ARCHIVE_BASE}/{symbol}/{filename}"
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
                frame = pd.read_csv(stream)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid Binance funding archive {filename}") from exc
    if frame.empty:
        return frame
    if not set(_COLUMNS).issubset(frame.columns):
        raise ValueError(f"Binance funding archive {filename} has an unexpected schema")
    timestamps = pd.to_numeric(frame["calc_time"], errors="coerce")
    unit = "us" if float(timestamps.dropna().median()) >= 100_000_000_000_000 else "ms"
    frame.index = pd.to_datetime(timestamps, unit=unit, utc=True, errors="coerce")
    frame["funding_rate"] = pd.to_numeric(frame["last_funding_rate"], errors="coerce")
    frame["interval_hours"] = pd.to_numeric(
        frame["funding_interval_hours"], errors="coerce"
    )
    return frame[["funding_rate", "interval_hours"]].dropna().sort_index()
