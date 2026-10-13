"""Read-only mStock Type B REST transport with explicit unmapped account values.

Primary contracts: https://tradingapi.mstock.com/docs/v1/typeB/User/ and
https://tradingapi.mstock.com/docs/v1/typeB/Portfolio/ . Holdings and net positions
are separate broker books; their overlap is not inferred. Fund limits are not
cash or net liquidation. This connector cannot enter portfolio aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

BASE_URL = "https://api.mstock.trade/openapi/typeb"
_OVERRIDE_KEYS = ("api_key", "access_token")


class MStockAPIError(RuntimeError):
    """A bounded, credential-free failure message from a read operation."""


@dataclass(frozen=True)
class MStockConfig:
    """Session credentials supplied by the connection's OS credential vault."""

    api_key: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    environment: str = "live"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> MStockConfig:
        """Build the only supported environment; never simulate an account."""
        values = dict(data or {})
        if values.get("environment", "live") != "live":
            raise ValueError("mStock supports only live read-only connections")
        for key in _OVERRIDE_KEYS:
            if not isinstance(values.get(key, ""), str):
                raise ValueError(f"{key} must be a string")
        return cls(api_key=values.get("api_key", "").strip(), access_token=values.get("access_token", "").strip())


def build_config(
    profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> MStockConfig:
    """Resolve profile settings and approved overrides, without a secret file."""
    values = dict(profile_config or {})
    values.update({key: value for key, value in dict(overrides or {}).items() if key in _OVERRIDE_KEYS})
    return MStockConfig.from_mapping(values)


def _read_records(config: MStockConfig, resource: str) -> list[dict[str, Any]]:
    if not config.api_key or not config.access_token:
        raise MStockAPIError("Configure the mStock API key and current session access token in connection settings.")
    # A fixed GET-only resource vocabulary excludes logout and all order APIs.
    if resource not in {"portfolio/holdings", "portfolio/positions", "user/fundsummary"}:
        raise MStockAPIError("Unsupported mStock read resource")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            response = client.get(
                f"{BASE_URL}/{resource}",
                headers={
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": config.api_key,
                    "Authorization": f"Bearer {config.access_token}",
                    "Accept": "application/json",
                },
            )
            if response.status_code in {401, 403}:
                raise MStockAPIError(
                    "mStock session rejected. Obtain a fresh session token and update the saved credentials."
                )
            if response.status_code != 200:
                raise MStockAPIError(
                    f"mStock read failed (HTTP {response.status_code}); retry after checking broker availability."
                )
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Exception strings may contain headers or a broker response with secrets.
        raise MStockAPIError(
            "mStock read failed: invalid response or network error. Check the session and retry."
        ) from exc
    if not isinstance(payload, dict) or not (payload.get("status") is True or payload.get("status") == "true"):
        raise MStockAPIError("mStock rejected the read. Check or renew the session token before retrying.")
    if payload.get("errorcode") not in (None, ""):
        raise MStockAPIError("mStock returned an error code for the read; check the session and retry.")
    records = payload.get("data")
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise MStockAPIError("mStock returned an unmapped data shape; no empty account is inferred.")
    return records


def get_positions(config: MStockConfig | None = None) -> dict[str, Any]:
    """Read both books separately, without inventing ticker identity or netting.

    Returns:
        The broker's holdings and net-position rows. These are deliberately not
        a canonical ``positions`` array: tradingsymbol can be a company name,
        and overlapping delivery positions must not be counted twice.
    """
    cfg = config or build_config()
    holdings = _read_records(cfg, "portfolio/holdings")
    net_positions = _read_records(cfg, "portfolio/positions")
    return {
        "status": "ok",
        "holdings": holdings,
        "net_positions": net_positions,
        "portfolio_eligible": False,
        "valuation_basis": "broker_books_unaggregated",
        "note": "Holdings and net positions are separate books. Names are not resolved tickers; do not sum overlapping books.",
    }


def get_account_snapshot(config: MStockConfig | None = None) -> dict[str, Any]:
    """Return raw fund limits with no invented cash/equity account summary."""
    return {
        "status": "ok",
        "fund_limits": _read_records(config or build_config(), "user/fundsummary"),
        "portfolio_eligible": False,
        "valuation_basis": "margin_limits_not_net_liquidation",
        "note": "Available balance, collateral and utilized margin are not mapped to cash or net assets.",
    }


def check_status(config: MStockConfig | None = None) -> dict[str, Any]:
    """Verify the read-only connection without exposing credentials or holdings."""
    try:
        get_positions(config)
    except MStockAPIError as exc:
        return {"status": "error", "error": str(exc), "portfolio_eligible": False}
    return {"status": "ok", "portfolio_eligible": False, "read_verified": True}


def _unsupported(*args, **kwargs) -> dict[str, Any]:
    return {"status": "unsupported", "error": "This mStock read-only connector does not implement this operation."}


get_open_orders = _unsupported
get_quote = _unsupported
get_historical_bars = _unsupported


def place_order(*args, config: MStockConfig | None = None, **kwargs):
    """Refuse every order before any network operation."""
    if config is None or config.environment != "paper":
        raise MStockAPIError("mStock order placement is disabled for every environment")
    raise MStockAPIError("mStock has no paper order implementation")


def cancel_order(*args, config: MStockConfig | None = None, **kwargs):
    """Refuse cancellation before any network operation."""
    if config is None or config.environment != "paper":
        raise MStockAPIError("mStock order cancellation is disabled for every environment")
    raise MStockAPIError("mStock has no paper cancellation implementation")
