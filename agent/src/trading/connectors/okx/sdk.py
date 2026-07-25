"""OKX connector via the optional ``python-okx`` SDK.

Wraps ``AccountAPI`` (account/positions), ``TradeAPI`` (orders/fills) and
``MarketAPI``/``PublicAPI`` (quotes, candles and instruments). Spot orders keep
the generic connector contract; Phase-1 perpetual writes are separately typed,
restricted to BTC/ETH USDT swaps, and hard-gated to OKX Demo.

Paper-vs-live is selected by the SDK ``flag`` (``"1"`` demo/paper sets the
``x-simulated-trading`` header, ``"0"`` live) and reinforced by OKX's separate
demo key namespace. OKX returns NO field echoing demo/live, so there is no hard
self-verifying guard: the discriminator is the configured flag plus UID pinning.
When ``expected_uid`` is set, :func:`check_status` requires the
account config endpoint to return a matching ``uid``; an API error, missing UID,
or mismatch fails the check. The guard marker recorded on every payload is
``header_flag+uid_pin``. The selected profile is recorded as ``paper`` on every
payload and never flipped implicitly.
"""

from __future__ import annotations

import json
import logging
import math
import os
import ssl
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from src.config.paths import get_runtime_root
from src.trading.connectors.okx.swap import (
    MAX_DEMO_LEVERAGE,
    ContractSizeResolution,
    SwapInstrument,
    SwapOrderIntent,
    SwapValidationError,
    as_decimal,
    build_attached_algo_orders,
    decimal_string,
    normalize_allowed_instruments,
    resolve_contract_size,
    validate_client_order_id,
)

CONFIG_FILENAME = "okx.json"
LIVE_CONFIG_FILENAME = "okx-live.json"
READ_MAX_ATTEMPTS = 3
READ_RETRY_BACKOFF_SECONDS = (0.25, 0.75)

logger = logging.getLogger(__name__)

#: Profiles this connector understands and their default account environment.
PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

DEFAULT_HOST = "https://www.okx.com"

#: SDK ``flag`` per environment: ``"1"`` demo/paper, ``"0"`` live.
_ENVIRONMENT_FLAGS = {"paper": "1", "live": "0"}


class OKXDependencyError(RuntimeError):
    """Raised when the optional ``python-okx`` package is not installed."""


class OKXConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


@dataclass(frozen=True)
class OKXConfig:
    """OKX connector connection settings.

    Args:
        api_key: OKX API key (demo and live use different keys).
        api_secret: OKX API secret key.
        passphrase: OKX API passphrase set at key creation.
        profile: ``paper``, ``live-readonly`` or ``live``.
        host: REST host (default ``https://www.okx.com``).
        expected_uid: Optional account UID to pin in ``check_status``.
        proxy: Optional HTTP(S) proxy URL. Runtime also honors ``OKX_PROXY``,
            ``HTTPS_PROXY`` and ``ALL_PROXY`` without persisting them.
        timeout: Network timeout in seconds.
        readonly: Connector profile metadata retained for compatibility.
        allowed_swap_instruments: Phase-1 USDT swap allowlist. It may narrow,
            but never widen, the built-in BTC/ETH scope.
        max_swap_leverage: Local Demo swap leverage ceiling (maximum 10x).
    """

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    profile: str = "paper"
    host: str = DEFAULT_HOST
    expected_uid: str = ""
    proxy: str = ""
    timeout: float = 15.0
    readonly: bool = True
    allowed_swap_instruments: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    max_swap_leverage: float = 10.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "OKXConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise OKXConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        try:
            allowed_swaps = normalize_allowed_instruments(payload.get("allowed_swap_instruments"))
        except SwapValidationError as exc:
            raise OKXConfigError(str(exc)) from exc
        try:
            max_swap_leverage = float(payload.get("max_swap_leverage", 10.0))
        except (TypeError, ValueError) as exc:
            raise OKXConfigError("max_swap_leverage must be a number from 1 to 10") from exc
        if not math.isfinite(max_swap_leverage) or not 1 <= max_swap_leverage <= float(MAX_DEMO_LEVERAGE):
            raise OKXConfigError("max_swap_leverage must be a finite number from 1 to 10")

        try:
            raw_timeout = payload.get("timeout", 15.0)
            timeout = float(15.0 if raw_timeout in (None, "") else raw_timeout)
        except (TypeError, ValueError) as exc:
            raise OKXConfigError("timeout must be a positive finite number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise OKXConfigError("timeout must be a positive finite number")

        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            api_secret=str(payload.get("api_secret") or "").strip(),
            passphrase=str(payload.get("passphrase") or "").strip(),
            profile=profile,
            host=str(payload.get("host") or DEFAULT_HOST).strip(),
            expected_uid=str(payload.get("expected_uid") or "").strip(),
            proxy=str(payload.get("proxy") or "").strip(),
            timeout=timeout,
            readonly=bool(payload.get("readonly", True)),
            allowed_swap_instruments=allowed_swaps,
            max_swap_leverage=max_swap_leverage,
        )

    def with_overrides(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        profile: str | None = None,
        host: str | None = None,
        expected_uid: str | None = None,
        proxy: str | None = None,
    ) -> "OKXConfig":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if api_key is not None:
            payload["api_key"] = api_key
        if api_secret is not None:
            payload["api_secret"] = api_secret
        if passphrase is not None:
            payload["passphrase"] = passphrase
        if profile is not None:
            payload["profile"] = profile
        if host is not None:
            payload["host"] = host
        if expected_uid is not None:
            payload["expected_uid"] = expected_uid
        if proxy is not None:
            payload["proxy"] = proxy
        return OKXConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def flag(self) -> str:
        """Return the OKX SDK flag (``"1"`` demo/paper, ``"0"`` live)."""
        return _ENVIRONMENT_FLAGS.get(self.environment, "1")

    @property
    def is_demo(self) -> bool:
        """Return whether this profile targets the OKX demo (paper) environment."""
        return self.flag == "1"


_OVERRIDE_KEYS = ("api_key", "api_secret", "passphrase", "profile", "host", "expected_uid", "proxy")


def build_config(profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> "OKXConfig":
    """Resolve config: saved file ← profile defaults ← CLI overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = OKXConfig.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    """Return the user-level OKX config path."""
    return get_runtime_root() / CONFIG_FILENAME


def live_config_path() -> Path:
    """Return the isolated user-level OKX live read-only config path."""
    return get_runtime_root() / LIVE_CONFIG_FILENAME


def _load_config_file(path: Path, *, default_profile: str) -> OKXConfig:
    if not path.exists():
        return OKXConfig(profile=default_profile)
    try:
        return OKXConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise OKXConfigError(f"invalid OKX config at {path}: {exc}") from exc


def load_config() -> OKXConfig:
    """Load OKX settings from ``~/.vibe-trading/okx.json``."""
    return _load_config_file(config_path(), default_profile="paper")


def _validate_live_readonly_config(config: OKXConfig) -> None:
    if config.profile != "live-readonly" or config.is_demo:
        raise OKXConfigError("stored OKX live configuration must use the live-readonly profile")
    if not config.readonly:
        raise OKXConfigError("stored OKX live configuration must remain read-only")
    if config.host != DEFAULT_HOST:
        raise OKXConfigError("stored OKX live configuration must use the official OKX host")


def load_live_config() -> OKXConfig:
    """Load the isolated live read-only settings from ``okx-live.json``."""
    config = _load_config_file(live_config_path(), default_profile="live-readonly")
    _validate_live_readonly_config(config)
    return config


def _save_config_file(config: OKXConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def save_config(config: OKXConfig) -> Path:
    """Persist OKX settings with owner-only permissions."""
    return _save_config_file(config, config_path())


def save_live_config(config: OKXConfig) -> Path:
    """Persist an isolated live read-only config with owner-only permissions."""
    _validate_live_readonly_config(config)
    return _save_config_file(config, live_config_path())


def okx_available() -> bool:
    """Return whether the optional ``python-okx`` SDK can be imported."""
    try:
        _require_okx()
        return True
    except OKXDependencyError:
        return False


def check_status(config: OKXConfig | None = None) -> dict[str, Any]:
    """Check SDK readiness, config completeness, and account identity.

    Returns a JSON-serializable health report. Does not place or mutate any
    broker state. When ``expected_uid`` is set, the account config endpoint must
    return a matching UID or the check fails closed.
    """
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "python-okx", "installed": okx_available()},
        "paper_guard": "header_flag+uid_pin",
        "flag": cfg.flag,
    }

    if cfg.flag not in ("0", "1"):
        report["status"] = "error"
        report["error"] = f"invalid OKX flag {cfg.flag!r}; expected '0' (live) or '1' (demo)."
        return report

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"OKX connector not configured: missing {', '.join(missing)}."
        return report

    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = "Optional dependency missing: install with `pip install python-okx`."
        return report

    try:
        snapshot = get_account_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    uid = None
    if cfg.expected_uid:
        try:
            account = _account_client(cfg)
            resp = _safe_read_call(account, "get_account_config")
            rows = _checked_read_rows(resp, "account configuration")
            if not rows:
                raise OKXConfigError("OKX returned no account configuration")
            uid = _first(rows[0], ("uid",))
            if uid in (None, ""):
                raise OKXConfigError(
                    "OKX account configuration did not include a UID; identity cannot be verified"
                )
            if str(uid) != cfg.expected_uid:
                raise OKXConfigError(
                    "UID mismatch: the configured OKX Demo UID does not match the authenticated account"
                )
        except Exception as exc:  # noqa: BLE001 - health report fails closed
            report["status"] = "error"
            report["error"] = str(exc)
            return report

    report["account"] = {
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "uid": str(uid) if uid is not None else None,
        "total_equity": snapshot.get("account", {}).get("total_equity"),
    }
    return report


def get_system_status(*, config: OKXConfig | None = None) -> dict[str, Any]:
    """Read OKX maintenance incidents used by the 24x7 runner pause gate."""
    cfg = config or load_config()
    try:
        rows = _checked_read_rows(_safe_read_call(_status_client(cfg), "status"), "system status")
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_system_status")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "incidents": [dict(row) for row in rows if isinstance(row, Mapping)],
    }


def get_account_snapshot(config: OKXConfig | None = None) -> dict[str, Any]:
    """Fetch account balance for the configured account."""
    cfg = config or load_config()
    account = _account_client(cfg)
    resp = _safe_read_call(account, "get_account_balance")
    rows = _checked_read_rows(resp, "account balance")
    summary = rows[0] if rows else {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "account": {
            "total_equity": _first(summary, ("totalEq",)),
            "details": [_balance_detail_to_dict(item) for item in _as_iter(_obj_get(summary, "details"))],
        },
    }


def get_positions(config: OKXConfig | None = None) -> dict[str, Any]:
    """Fetch current positions for the configured account."""
    cfg = config or load_config()
    account = _account_client(cfg)
    resp = _safe_read_call(account, "get_positions")
    rows = [_position_to_dict(item) for item in _checked_read_rows(resp, "positions")]
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "positions": rows,
    }


def get_open_orders(config: OKXConfig | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """Fetch open orders and, optionally, recent fills."""
    cfg = config or load_config()
    trade = _trade_client(cfg)
    resp = _safe_read_call(trade, "get_order_list")
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "open_orders": [_order_to_dict(item) for item in _checked_read_rows(resp, "open orders")],
    }
    if include_executions:
        fills = _safe_read_call(trade, "get_fills")
        result["executions"] = [
            _fill_to_dict(item) for item in _checked_read_rows(fills, "fills")
        ]
    return result


def get_quote(symbol: str, *, config: OKXConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a top-of-book ticker snapshot for ``symbol``."""
    cfg = config or load_config()
    market = _market_client(cfg)
    clean = symbol.strip().upper()
    resp = _safe_read_call(market, "get_ticker", instId=clean)
    rows = _extract_data(resp)
    payload = _quote_to_dict(rows[0]) if rows else {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": clean,
        "quote": payload,
    }


#: Canonical period token → OKX ``bar`` parameter.
_BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W", "1M": "1M",
}


def get_historical_bars(
    symbol: str,
    *,
    config: OKXConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical OHLCV candlesticks for ``symbol`` (``period`` canonical)."""
    cfg = config or load_config()
    market = _market_client(cfg)
    clean = symbol.strip().upper()
    bar = _BAR_MAP.get(period.strip(), "1D")
    resp = _safe_read_call(market, "get_candlesticks", instId=clean, bar=bar, limit=str(int(limit)))
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": clean,
        "period": period,
        "bar": bar,
        "bars": [_candle_to_dict(item) for item in _extract_data(resp)],
    }


# ---------------------------------------------------------------------------
# USDT perpetual swap reads
# ---------------------------------------------------------------------------


def get_swap_instrument(symbol: str, *, config: OKXConfig | None = None) -> dict[str, Any]:
    """Read and validate Phase-1 contract metadata from OKX public data."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    try:
        instrument = _load_swap_instrument(cfg, clean_symbol)
    except Exception as exc:  # noqa: BLE001 - read tool returns a clean envelope
        return _order_error(cfg, str(exc), symbol=clean_symbol, operation="get_swap_instrument")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "instrument": instrument.to_dict(),
    }


def get_account_configuration(config: OKXConfig | None = None) -> dict[str, Any]:
    """Read the OKX account level and position mode used by swap validation."""
    cfg = config or load_config()
    missing = _missing_fields(cfg)
    if missing:
        return _order_error(cfg, f"OKX connector not configured: missing {', '.join(missing)}.")
    try:
        resp = _safe_read_call(_account_client(cfg), "get_account_config")
        rows = _checked_read_rows(resp, "account configuration")
        if not rows:
            raise OKXConfigError(_resp_message(resp) or "OKX returned no account configuration")
        row = rows[0]
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_account_configuration")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "account_configuration": _account_configuration_to_dict(row),
    }


def get_swap_leverage(
    symbol: str,
    *,
    margin_mode: str,
    config: OKXConfig | None = None,
) -> dict[str, Any]:
    """Read current leverage for one allowed USDT perpetual swap."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    clean_margin_mode = str(margin_mode or "").strip().lower()
    if clean_margin_mode not in ("cross", "isolated"):
        return _order_error(cfg, "margin_mode must be 'cross' or 'isolated'", symbol=clean_symbol)
    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol
        )
    try:
        _load_swap_instrument(cfg, clean_symbol)
        rows = _read_swap_leverage_rows(cfg, clean_symbol, clean_margin_mode)
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), symbol=clean_symbol, operation="get_swap_leverage")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": clean_symbol,
        "margin_mode": clean_margin_mode,
        "leverage": rows,
    }


def get_order(
    symbol: str,
    *,
    config: OKXConfig | None = None,
    order_id: str | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Read one OKX order by exchange id or stable client order id."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    clean_order_id = str(order_id or "").strip()
    clean_client_id = str(client_order_id or "").strip()
    if bool(clean_order_id) == bool(clean_client_id):
        return _order_error(
            cfg,
            "exactly one of order_id or client_order_id is required",
            symbol=clean_symbol,
        )
    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol
        )
    try:
        trade = _trade_client(cfg)
        resp = trade.get_order(
            instId=clean_symbol,
            **({"ordId": clean_order_id} if clean_order_id else {"clOrdId": clean_client_id}),
        )
        rows = _extract_data(resp)
        if not rows:
            raise OKXConfigError(_resp_message(resp) or "OKX returned no order data")
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), symbol=clean_symbol, operation="get_order")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "order": _order_to_dict(rows[0]),
    }


def get_swap_positions(config: OKXConfig | None = None) -> dict[str, Any]:
    """Read all SWAP positions without hiding unexpected instruments."""
    cfg = config or load_config()
    try:
        rows = _checked_read_rows(
            _safe_read_call(_account_client(cfg), "get_positions", instType="SWAP"),
            "swap positions",
        )
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_swap_positions")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "positions": [_position_to_dict(row) for row in rows],
    }


def get_swap_open_orders(config: OKXConfig | None = None) -> dict[str, Any]:
    """Read all pending SWAP orders for reconciliation."""
    cfg = config or load_config()
    try:
        rows = _checked_read_rows(
            _safe_read_call(_trade_client(cfg), "get_order_list", instType="SWAP"),
            "swap open orders",
        )
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_swap_open_orders")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "open_orders": [_order_to_dict(row) for row in rows],
    }


def get_swap_fills(config: OKXConfig | None = None) -> dict[str, Any]:
    """Read recent SWAP fills used to resolve crash-time order ambiguity."""
    cfg = config or load_config()
    try:
        rows = _checked_read_rows(
            _safe_read_call(_trade_client(cfg), "get_fills", instType="SWAP", limit="100"),
            "swap fills",
        )
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_swap_fills")
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "fills": [_fill_to_dict(row) for row in rows],
    }


def get_swap_algo_orders(config: OKXConfig | None = None) -> dict[str, Any]:
    """Read pending SWAP TP/SL and OCO orders for protection reconciliation."""
    cfg = config or load_config()
    collected: list[Any] = []
    try:
        trade = _trade_client(cfg)
        for order_type in ("conditional", "oco"):
            response = _safe_read_call(
                trade,
                "order_algos_list",
                ordType=order_type,
                instType="SWAP",
            )
            collected.extend(_checked_read_rows(response, f"swap {order_type} algo orders"))
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), operation="get_swap_algo_orders")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in collected:
        normalized = _algo_order_to_dict(item)
        identity = str(normalized.get("algo_id") or normalized.get("client_algo_order_id") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        rows.append(normalized)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "algo_orders": rows,
    }


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


def place_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | str | None = None,
    notional: float | str | None = None,
    order_type: str = "market",
    limit_price: float | str | None = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Place a spot order on the configured OKX account.

    The demo-vs-live target is selected entirely by the configured profile flag
    (``"1"`` demo, ``"0"`` live); this connector merely executes against whatever
    environment the flag selects.

    Args:
        config: Resolved connector config; loaded from disk when ``None``.
        symbol: Instrument id, e.g. ``"BTC-USDT"`` (uppercased before sending).
        side: ``"buy"`` or ``"sell"``.
        quantity: Base-currency size. Exactly one of ``quantity``/``notional``.
        notional: Quote-currency amount (market orders only). Exactly one of
            ``quantity``/``notional``.
        order_type: ``"market"`` or ``"limit"``. Limit orders require
            ``limit_price`` and must be sized with ``quantity``.
        limit_price: Limit price (required and only used for limit orders).
        time_in_force: Accepted for interface symmetry; OKX spot ``cash`` orders
            do not take a standalone TIF field, so this is recorded but not sent.

    Returns:
        On success ``{"status": "ok", "order_id", "symbol", "side", "profile",
        ...}``. On any validation or broker error a fail-closed
        ``{"status": "error", "error": ...}`` payload (no order is sent unless
        every precondition passes).
    """
    cfg = config or load_config()

    clean_side = str(side or "").strip().lower()
    if clean_side not in ("buy", "sell"):
        return _order_error(cfg, "side must be 'buy' or 'sell'", symbol=symbol, side=side)

    clean_type = str(order_type or "").strip().lower()
    if clean_type not in ("market", "limit"):
        return _order_error(cfg, "order_type must be 'market' or 'limit'", symbol=symbol, side=clean_side)

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return _order_error(cfg, "symbol is required", symbol=symbol, side=clean_side)
    if clean_symbol.endswith("-SWAP"):
        return _order_error(
            cfg,
            "swap instruments require place_swap_order with explicit size unit, margin mode, position side and leverage",
            symbol=clean_symbol,
            side=clean_side,
        )

    has_qty = quantity is not None
    has_notional = notional is not None
    if has_qty == has_notional:
        return _order_error(
            cfg, "exactly one of quantity or notional is required", symbol=clean_symbol, side=clean_side
        )

    # Limit orders are priced and always sized in base currency.
    if clean_type == "limit":
        if limit_price is None:
            return _order_error(cfg, "limit order requires limit_price", symbol=clean_symbol, side=clean_side)
        if not has_qty:
            return _order_error(
                cfg, "limit order must be sized with quantity (base size)", symbol=clean_symbol, side=clean_side
            )

    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol, side=clean_side
        )

    # Build the OKX request. Spot trading uses tdMode="cash".
    params: dict[str, Any] = {
        "instId": clean_symbol,
        "tdMode": "cash",
        "side": clean_side,
        "ordType": clean_type,
    }
    if has_qty:
        params["sz"] = str(quantity)
    else:
        # Market order sized by quote-currency notional: OKX expects the quote
        # amount in ``sz`` plus ``tgtCcy="quote_ccy"`` so it is not mistaken for
        # a base-currency size. (notional is only reachable for market orders
        # here: limit orders reject above unless quantity is supplied.)
        params["sz"] = str(notional)
        params["tgtCcy"] = "quote_ccy"
    if clean_type == "limit":
        params["px"] = str(limit_price)

    try:
        trade = _trade_client(cfg)
        # Call the SDK directly (NOT via _safe_read_call): a write must fail closed on
        # any signature drift, never silently re-invoke with stripped arguments.
        resp = trade.place_order(**params)
    except OKXDependencyError as exc:
        return _order_error(cfg, str(exc), symbol=clean_symbol, side=clean_side)
    except Exception as exc:  # noqa: BLE001 - broker/network failures fail closed
        return _order_error(cfg, str(exc), symbol=clean_symbol, side=clean_side)

    return _order_result(
        cfg,
        resp,
        symbol=clean_symbol,
        side=clean_side,
        order_type=clean_type,
        time_in_force=str(time_in_force or "").strip().lower(),
    )


def cancel_order(
    config: OKXConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel a resting order on the configured OKX account.

    Args:
        config: Resolved connector config; loaded from disk when ``None``.
        order_id: OKX order id (``ordId``) to cancel.
        symbol: Instrument id of the order. OKX requires ``instId`` to cancel,
            so this is mandatory; ``None`` fails closed.

    Returns:
        On success ``{"status": "ok", "order_id", "symbol", "profile", ...}``.
        On any validation or broker error a fail-closed
        ``{"status": "error", "error": ...}`` payload.
    """
    cfg = config or load_config()

    clean_id = str(order_id or "").strip()
    if not clean_id:
        return _order_error(cfg, "OKX cancel requires order_id", symbol=symbol)

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return _order_error(cfg, "OKX cancel requires symbol", order_id=clean_id)

    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol, order_id=clean_id
        )

    try:
        trade = _trade_client(cfg)
        # Direct SDK call (not _safe_read_call): writes must fail closed, not retry stripped.
        resp = trade.cancel_order(instId=clean_symbol, ordId=clean_id)
    except OKXDependencyError as exc:
        return _order_error(cfg, str(exc), symbol=clean_symbol, order_id=clean_id)
    except Exception as exc:  # noqa: BLE001 - broker/network failures fail closed
        return _order_error(cfg, str(exc), symbol=clean_symbol, order_id=clean_id)

    return _order_result(cfg, resp, symbol=clean_symbol, action="cancel", requested_order_id=clean_id)


def cancel_swap_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    order_id: str,
) -> dict[str, Any]:
    """Cancel one ordinary SWAP order in Demo, once, then verify its state."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    clean_order_id = str(order_id or "").strip()
    try:
        _require_demo_swap_write(cfg)
        _require_configured(cfg)
        _load_swap_instrument(cfg, clean_symbol)
        if not clean_order_id:
            raise SwapValidationError("order_id is required")
        trade = _trade_client(cfg)
        resp = trade.cancel_order(instId=clean_symbol, ordId=clean_order_id)
    except (SwapValidationError, OKXConfigError, OKXDependencyError) as exc:
        return _order_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="cancel_swap_order",
            order_id=clean_order_id,
            retry_safe=True,
        )
    except Exception as exc:  # request may have reached OKX
        return _unknown_write_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="cancel_swap_order",
            order_id=clean_order_id,
        )
    result = _order_result(
        cfg,
        resp,
        symbol=clean_symbol,
        operation="cancel_swap_order",
        requested_order_id=clean_order_id,
    )
    result["retry_safe"] = False
    if result.get("status") == "ok":
        result["verification"] = _verify_order(
            trade,
            clean_symbol,
            order_id=clean_order_id,
            client_order_id="",
        )
    return result


def cancel_swap_algo_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    algo_id: str,
) -> dict[str, Any]:
    """Cancel one pending SWAP TP/SL algo in Demo with no automatic retry."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    clean_algo_id = str(algo_id or "").strip()
    try:
        _require_demo_swap_write(cfg)
        _require_configured(cfg)
        _load_swap_instrument(cfg, clean_symbol)
        if not clean_algo_id:
            raise SwapValidationError("algo_id is required")
        trade = _trade_client(cfg)
        resp = trade.cancel_algo_order([{"instId": clean_symbol, "algoId": clean_algo_id}])
    except (SwapValidationError, OKXConfigError, OKXDependencyError) as exc:
        return _order_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="cancel_swap_algo_order",
            algo_id=clean_algo_id,
            retry_safe=True,
        )
    except Exception as exc:  # request may have reached OKX
        return _unknown_write_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="cancel_swap_algo_order",
            algo_id=clean_algo_id,
        )
    result = _mutation_result(
        cfg,
        resp,
        symbol=clean_symbol,
        operation="cancel_swap_algo_order",
        algo_id=clean_algo_id,
    )
    result["retry_safe"] = False
    return result


def set_swap_leverage(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    leverage: float | str,
    margin_mode: str,
    position_side: str,
) -> dict[str, Any]:
    """Set and verify leverage for an allowed swap in OKX Demo only."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    try:
        _require_demo_swap_write(cfg)
        _require_configured(cfg)
        desired = as_decimal(leverage, "leverage")
        if desired < 1:
            raise SwapValidationError("leverage must be at least 1")
        configured_max = as_decimal(cfg.max_swap_leverage, "max_swap_leverage")
        if desired > configured_max:
            raise SwapValidationError(
                f"leverage {decimal_string(desired)} exceeds configured maximum "
                f"{decimal_string(configured_max)}"
            )
        _load_swap_instrument(cfg, clean_symbol)
        account_configuration = _load_account_configuration(cfg)
        clean_margin_mode = _validate_margin_mode(margin_mode)
        leverage_pos_side = _leverage_position_parameter(
            account_configuration,
            clean_margin_mode,
            position_side,
        )
    except Exception as exc:  # noqa: BLE001
        return _order_error(cfg, str(exc), symbol=clean_symbol, operation="set_swap_leverage")

    params: dict[str, str] = {
        "instId": clean_symbol,
        "lever": decimal_string(desired),
        "mgnMode": clean_margin_mode,
    }
    if leverage_pos_side is not None:
        params["posSide"] = leverage_pos_side

    try:
        account = _account_client(cfg)
        resp = account.set_leverage(**params)
    except Exception as exc:  # noqa: BLE001 - outcome may be unknown after transport failure
        return _unknown_write_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="set_swap_leverage",
        )

    result = _mutation_result(
        cfg,
        resp,
        symbol=clean_symbol,
        operation="set_swap_leverage",
        margin_mode=clean_margin_mode,
        position_side=str(position_side or "").strip().lower(),
        requested_leverage=decimal_string(desired),
    )
    if result.get("status") != "ok":
        result["retry_safe"] = False
        return result

    try:
        rows = _read_swap_leverage_rows(cfg, clean_symbol, clean_margin_mode)
        verified = _select_order_leverage(
            rows,
            position_mode=account_configuration["position_mode"],
            margin_mode=clean_margin_mode,
            position_side=(
                "long" if position_side == "both" else str(position_side or "").strip().lower()
            ),
        )
        matches = verified == desired
        verification = {
            "status": "verified" if matches else "mismatch",
            "expected_leverage": decimal_string(desired),
            "observed_leverage": decimal_string(verified),
            "rows": rows,
        }
    except Exception as exc:  # noqa: BLE001 - write was accepted; never imply retry
        verification = {"status": "unknown", "error": str(exc)}
    result["verification"] = verification
    result["retry_safe"] = False
    return result


def place_swap_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    side: str,
    size: float | str,
    size_unit: str,
    margin_mode: str,
    position_side: str,
    leverage: float | str,
    client_order_id: str,
    order_type: str = "market",
    limit_price: float | str | None = None,
    reduce_only: bool = False,
    take_profit: float | str | None = None,
    stop_loss: float | str | None = None,
    trigger_price_type: str = "mark",
) -> dict[str, Any]:
    """Place an explicitly-sized USDT perpetual order in OKX Demo.

    The function queries product metadata, account/position mode, leverage and
    positions before writing.  ``quote_ccy`` and ``margin`` amounts are converted
    to contracts using live ``ctVal``/``lotSz`` rather than a hard-coded BTC/ETH
    face value.
    """
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    try:
        _require_demo_swap_write(cfg)
        _require_configured(cfg)
        intent = SwapOrderIntent.from_values(
            instrument_id=clean_symbol,
            side=side,
            size=size,
            size_unit=size_unit,
            order_type=order_type,
            margin_mode=margin_mode,
            position_side=position_side,
            leverage=leverage,
            client_order_id=client_order_id,
            limit_price=limit_price,
            reduce_only=reduce_only,
            take_profit=take_profit,
            stop_loss=stop_loss,
            trigger_price_type=trigger_price_type,
            allowed_instruments=cfg.allowed_swap_instruments,
            max_leverage=as_decimal(cfg.max_swap_leverage, "max_swap_leverage"),
        )
        instrument = _load_swap_instrument(cfg, intent.instrument_id)
        account_configuration = _load_account_configuration(cfg)
        _validate_order_position_mode(intent, account_configuration)
        trade = _trade_client(cfg)
        lookup_state, existing, lookup_error = _lookup_client_order(
            trade,
            intent.instrument_id,
            intent.client_order_id,
        )
        if lookup_state == "found":
            return _idempotent_order_result(cfg, intent, existing)
        if lookup_state != "absent":
            raise SwapValidationError(
                f"cannot prove client_order_id is unused; order blocked: {lookup_error or 'lookup failed'}"
            )

        leverage_rows = _read_swap_leverage_rows(cfg, intent.instrument_id, intent.margin_mode)
        observed_leverage = _select_order_leverage(
            leverage_rows,
            position_mode=account_configuration["position_mode"],
            margin_mode=intent.margin_mode,
            position_side=intent.position_side,
        )
        if observed_leverage != intent.leverage:
            raise SwapValidationError(
                f"confirmed leverage {decimal_string(intent.leverage)} does not match OKX leverage "
                f"{decimal_string(observed_leverage)}; set and verify leverage first"
            )

        position_rows = _read_swap_position_rows(cfg, intent.instrument_id)
        matching_position = _validate_position_for_order(intent, position_rows)
        reference_price = (
            intent.limit_price
            if intent.limit_price is not None
            else _read_last_price(cfg, intent.instrument_id)
        )
        resolution = resolve_contract_size(intent, instrument, reference_price=reference_price)
        if intent.reduce_only:
            _validate_reduce_size(resolution, matching_position)
        attached_algos = build_attached_algo_orders(
            intent,
            instrument,
            reference_price=reference_price,
        )
    except Exception as exc:  # noqa: BLE001 - all failures above occur before write
        return _order_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="place_swap_order",
            client_order_id=str(client_order_id or "").strip(),
            retry_safe=True,
        )

    params: dict[str, Any] = {
        "instId": intent.instrument_id,
        "tdMode": intent.margin_mode,
        "side": intent.side,
        "ordType": intent.order_type,
        "sz": decimal_string(resolution.contracts),
        "clOrdId": intent.client_order_id,
    }
    if account_configuration["position_mode"] == "long_short_mode":
        params["posSide"] = intent.position_side
    if intent.limit_price is not None:
        params["px"] = decimal_string(intent.limit_price)
    if intent.reduce_only:
        params["reduceOnly"] = "true"
    if attached_algos:
        params["attachAlgoOrds"] = attached_algos

    try:
        resp = trade.place_order(**params)
    except Exception as exc:  # noqa: BLE001 - request may have reached OKX
        return _unknown_write_error(
            cfg,
            str(exc),
            symbol=intent.instrument_id,
            operation="place_swap_order",
            client_order_id=intent.client_order_id,
            sizing=resolution.to_dict(),
        )

    result = _order_result(
        cfg,
        resp,
        symbol=intent.instrument_id,
        side=intent.side,
        order_type=intent.order_type,
        margin_mode=intent.margin_mode,
        position_side=intent.position_side,
        leverage=decimal_string(intent.leverage),
        reduce_only=intent.reduce_only,
        sizing=resolution.to_dict(),
        instrument=instrument.to_dict(),
        attached_tp_sl=attached_algos or [],
    )
    result["retry_safe"] = False
    if result.get("status") != "ok":
        return result
    result["verification"] = _verify_order(
        trade,
        intent.instrument_id,
        order_id=str(result.get("order_id") or ""),
        client_order_id=intent.client_order_id,
    )
    return result


def close_swap_position(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    margin_mode: str,
    position_side: str,
    leverage: float | str,
    client_order_id: str,
    auto_cancel: bool = False,
) -> dict[str, Any]:
    """Close one entire swap position at market in OKX Demo and verify it."""
    cfg = config or load_config()
    clean_symbol = str(symbol or "").strip().upper()
    clean_position_side = str(position_side or "").strip().lower()
    try:
        _require_demo_swap_write(cfg)
        _require_configured(cfg)
        _load_swap_instrument(cfg, clean_symbol)
        clean_margin_mode = _validate_margin_mode(margin_mode)
        clean_client_id = validate_client_order_id(client_order_id)
        desired_leverage = as_decimal(leverage, "leverage")
        if desired_leverage < 1:
            raise SwapValidationError("leverage must be at least 1")
        if desired_leverage > as_decimal(cfg.max_swap_leverage, "max_swap_leverage"):
            raise SwapValidationError("leverage exceeds configured maximum")
        account_configuration = _load_account_configuration(cfg)
        _validate_close_position_side(account_configuration, clean_position_side)
        leverage_rows = _read_swap_leverage_rows(cfg, clean_symbol, clean_margin_mode)
        observed_leverage = _select_order_leverage(
            leverage_rows,
            position_mode=account_configuration["position_mode"],
            margin_mode=clean_margin_mode,
            position_side=clean_position_side,
        )
        if observed_leverage != desired_leverage:
            raise SwapValidationError(
                f"confirmed leverage {decimal_string(desired_leverage)} does not match OKX leverage "
                f"{decimal_string(observed_leverage)}"
            )
        position_rows = _read_swap_position_rows(cfg, clean_symbol)
        position = _find_position(position_rows, clean_position_side, clean_margin_mode)
        if position is None or _position_contracts(position) == 0:
            raise SwapValidationError(
                f"no open {clean_position_side} position found for {clean_symbol}"
            )
        trade = _trade_client(cfg)
        lookup_state, existing, lookup_error = _lookup_client_order(
            trade,
            clean_symbol,
            clean_client_id,
        )
        if lookup_state == "found":
            return {
                "status": "ok",
                "profile": cfg.profile,
                "is_demo": cfg.is_demo,
                "paper_guard": "header_flag+uid_pin",
                "symbol": clean_symbol,
                "client_order_id": clean_client_id,
                "idempotent_replay": True,
                "order": _order_to_dict(existing),
                "retry_safe": False,
            }
        if lookup_state != "absent":
            raise SwapValidationError(
                f"cannot prove client_order_id is unused; close blocked: {lookup_error or 'lookup failed'}"
            )
    except Exception as exc:  # noqa: BLE001 - all failures above occur before write
        return _order_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="close_swap_position",
            client_order_id=str(client_order_id or "").strip(),
            retry_safe=True,
        )

    params: dict[str, Any] = {
        "instId": clean_symbol,
        "mgnMode": clean_margin_mode,
        "autoCxl": "true" if bool(auto_cancel) else "false",
        "clOrdId": clean_client_id,
    }
    if account_configuration["position_mode"] == "long_short_mode":
        params["posSide"] = clean_position_side
    try:
        resp = trade.close_positions(**params)
    except Exception as exc:  # noqa: BLE001 - request may have reached OKX
        return _unknown_write_error(
            cfg,
            str(exc),
            symbol=clean_symbol,
            operation="close_swap_position",
            client_order_id=clean_client_id,
        )

    result = _mutation_result(
        cfg,
        resp,
        symbol=clean_symbol,
        operation="close_swap_position",
        client_order_id=clean_client_id,
        margin_mode=clean_margin_mode,
        position_side=clean_position_side,
        leverage=decimal_string(desired_leverage),
        auto_cancel=bool(auto_cancel),
        closed_contracts=decimal_string(abs(_position_contracts(position))),
    )
    result["retry_safe"] = False
    if result.get("status") != "ok":
        return result
    result["verification"] = _verify_closed_position(
        cfg,
        clean_symbol,
        clean_position_side,
        clean_margin_mode,
    )
    return result


def _order_error(cfg: OKXConfig, message: str, **extra: Any) -> dict[str, Any]:
    """Build a fail-closed error payload carrying profile/guard context."""
    payload: dict[str, Any] = {
        "status": "error",
        "error": message,
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _order_result(cfg: OKXConfig, resp: Any, *, symbol: str, **extra: Any) -> dict[str, Any]:
    """Interpret an OKX order/cancel response, failing closed on any non-zero code.

    Success requires both the outer ``code == "0"`` and the first data row's
    ``sCode == "0"``; otherwise the row's ``sMsg`` (or a generic message) is
    surfaced as an error.
    """
    rows = _extract_data(resp)
    if not rows:
        message = _resp_message(resp) or "OKX returned no order data"
        return _order_error(cfg, message, symbol=symbol, **extra)

    row = rows[0]
    s_code = str(_first(row, ("sCode",)) or "")
    if s_code != "0":
        message = str(_first(row, ("sMsg",)) or _resp_message(resp) or f"OKX rejected order (sCode={s_code or '?'})")
        return _order_error(cfg, message, symbol=symbol, **extra)

    order_id = str(_first(row, ("ordId",)) or "")
    result: dict[str, Any] = {
        "status": "ok",
        "order_id": order_id,
        "symbol": symbol,
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "client_order_id": _first(row, ("clOrdId",)),
    }
    for key, value in extra.items():
        result[key] = value
    return result


def _resp_message(resp: Any) -> str:
    """Pull a top-level OKX error message (``msg``) when present."""
    if isinstance(resp, Mapping):
        return str(resp.get("msg") or "")
    return ""


def _checked_read_rows(resp: Any, subject: str) -> list[Any]:
    """Require an explicit OKX success envelope for authenticated reads."""
    if not isinstance(resp, Mapping):
        raise OKXConfigError(f"OKX returned a malformed {subject} response")
    code = str(resp.get("code") or "")
    if code != "0":
        message = _resp_message(resp) or f"failed to read OKX {subject}"
        suffix = f" (OKX code {code})" if code else ""
        raise OKXConfigError(f"{message}{suffix}")
    rows = resp.get("data")
    if not isinstance(rows, (list, tuple)):
        raise OKXConfigError(f"OKX returned malformed {subject} data")
    return list(rows)


# ---------------------------------------------------------------------------
# Swap validation, conversion, idempotency, and verification helpers
# ---------------------------------------------------------------------------


def _require_demo_swap_write(cfg: OKXConfig) -> None:
    if not cfg.is_demo:
        raise SwapValidationError(
            "OKX perpetual writes are Phase-1 Demo only; live swap trading remains disabled"
        )


def _require_configured(cfg: OKXConfig) -> None:
    missing = _missing_fields(cfg)
    if missing:
        raise OKXConfigError(f"OKX connector not configured: missing {', '.join(missing)}.")


def _validate_margin_mode(value: Any) -> str:
    clean = str(value or "").strip().lower()
    if clean not in ("cross", "isolated"):
        raise SwapValidationError("margin_mode must be 'cross' or 'isolated'")
    return clean


def _load_swap_instrument(cfg: OKXConfig, symbol: str) -> SwapInstrument:
    clean_symbol = str(symbol or "").strip().upper()
    public = _public_client(cfg)
    resp = _safe_read_call(public, "get_instruments", instType="SWAP", instId=clean_symbol)
    rows = _extract_data(resp)
    if not rows:
        raise SwapValidationError(
            _resp_message(resp) or f"OKX returned no SWAP metadata for {clean_symbol or '<blank>'}"
        )
    row = rows[0]
    if not isinstance(row, Mapping):
        raise SwapValidationError("OKX returned malformed instrument metadata")
    return SwapInstrument.from_okx(
        row,
        expected_instrument_id=clean_symbol,
        allowed_instruments=cfg.allowed_swap_instruments,
    )


def _load_account_configuration(cfg: OKXConfig) -> dict[str, str | None]:
    account = _account_client(cfg)
    resp = _safe_read_call(account, "get_account_config")
    rows = _checked_read_rows(resp, "account configuration")
    if not rows:
        raise OKXConfigError(_resp_message(resp) or "OKX returned no account configuration")
    return _account_configuration_to_dict(rows[0])


def _account_configuration_to_dict(row: Any) -> dict[str, str | None]:
    position_mode = str(_first(row, ("posMode",)) or "").strip().lower()
    if position_mode not in ("net_mode", "long_short_mode"):
        raise OKXConfigError(f"unsupported or missing OKX position mode: {position_mode or 'unknown'}")
    return {
        "uid": str(_first(row, ("uid",)) or "") or None,
        "account_level": str(_first(row, ("acctLv",)) or "") or None,
        "position_mode": position_mode,
        "auto_loan": str(_first(row, ("autoLoan",)) or "") or None,
        "greeks_type": str(_first(row, ("greeksType",)) or "") or None,
    }


def _leverage_position_parameter(
    account_configuration: Mapping[str, Any],
    margin_mode: str,
    position_side: str,
) -> str | None:
    mode = str(account_configuration.get("position_mode") or "").strip().lower()
    side = str(position_side or "").strip().lower()
    if str(account_configuration.get("account_level") or "") == "4" and margin_mode == "cross":
        raise SwapValidationError("portfolio-margin accounts cannot set cross swap leverage")
    if mode == "net_mode":
        if side != "net":
            raise SwapValidationError("net_mode leverage requires position_side='net'")
        return None
    if mode != "long_short_mode":
        raise SwapValidationError(f"unsupported OKX position mode: {mode or 'unknown'}")
    if margin_mode == "isolated":
        if side not in ("long", "short"):
            raise SwapValidationError(
                "long_short_mode isolated leverage requires position_side='long' or 'short'"
            )
        return side
    if side != "both":
        raise SwapValidationError("long_short_mode cross leverage applies to both sides; use position_side='both'")
    return None


def _read_swap_leverage_rows(cfg: OKXConfig, symbol: str, margin_mode: str) -> list[dict[str, Any]]:
    account = _account_client(cfg)
    resp = account.get_leverage(mgnMode=margin_mode, instId=symbol)
    rows = _extract_data(resp)
    if not rows:
        raise OKXConfigError(_resp_message(resp) or f"OKX returned no leverage for {symbol}")
    return [_leverage_to_dict(row) for row in rows]


def _leverage_to_dict(row: Any) -> dict[str, Any]:
    leverage = as_decimal(_first(row, ("lever",)), "lever")
    return {
        "symbol": str(_first(row, ("instId",)) or "").strip().upper(),
        "margin_mode": str(_first(row, ("mgnMode",)) or "").strip().lower(),
        "position_side": str(_first(row, ("posSide",)) or "net").strip().lower(),
        "leverage": decimal_string(leverage),
    }


def _select_order_leverage(
    rows: list[dict[str, Any]],
    *,
    position_mode: str,
    margin_mode: str,
    position_side: str,
) -> Any:
    filtered = [
        row
        for row in rows
        if str(row.get("margin_mode") or margin_mode).lower() == margin_mode
    ]
    if not filtered:
        raise SwapValidationError("OKX leverage response does not match the requested margin mode")
    if position_mode == "net_mode":
        preferred = [row for row in filtered if str(row.get("position_side") or "net") in ("", "net")]
    else:
        preferred = [row for row in filtered if str(row.get("position_side") or "") == position_side]
    candidates = preferred or filtered
    values = {as_decimal(row.get("leverage"), "leverage") for row in candidates}
    if len(values) != 1:
        raise SwapValidationError("OKX returned ambiguous leverage values for the requested position")
    return next(iter(values))


def _validate_order_position_mode(
    intent: SwapOrderIntent,
    account_configuration: Mapping[str, Any],
) -> None:
    mode = str(account_configuration.get("position_mode") or "").strip().lower()
    if mode == "net_mode" and intent.position_side != "net":
        raise SwapValidationError("OKX account is in net_mode; position_side must be 'net'")
    if mode == "long_short_mode" and intent.position_side not in ("long", "short"):
        raise SwapValidationError(
            "OKX account is in long_short_mode; position_side must be 'long' or 'short'"
        )
    if mode not in ("net_mode", "long_short_mode"):
        raise SwapValidationError(f"unsupported OKX position mode: {mode or 'unknown'}")


def _validate_close_position_side(
    account_configuration: Mapping[str, Any],
    position_side: str,
) -> None:
    mode = str(account_configuration.get("position_mode") or "").strip().lower()
    if mode == "net_mode" and position_side != "net":
        raise SwapValidationError("OKX account is in net_mode; position_side must be 'net'")
    if mode == "long_short_mode" and position_side not in ("long", "short"):
        raise SwapValidationError(
            "OKX account is in long_short_mode; position_side must be 'long' or 'short'"
        )
    if mode not in ("net_mode", "long_short_mode"):
        raise SwapValidationError(f"unsupported OKX position mode: {mode or 'unknown'}")


def _read_swap_position_rows(cfg: OKXConfig, symbol: str) -> list[Any]:
    account = _account_client(cfg)
    resp = account.get_positions(instType="SWAP", instId=symbol)
    if not isinstance(resp, Mapping) or str(resp.get("code")) != "0":
        raise OKXConfigError(_resp_message(resp) or f"failed to read OKX positions for {symbol}")
    rows = resp.get("data")
    if not isinstance(rows, (list, tuple)):
        raise OKXConfigError("OKX returned malformed position data")
    return [row for row in rows if str(_first(row, ("instId",)) or "").strip().upper() == symbol]


def _position_contracts(position: Any) -> Any:
    raw = _first(position, ("pos", "quantity", "qty"), 0)
    try:
        value = as_decimal(abs(Decimal(str(raw))), "position size", allow_zero=True)
    except (InvalidOperation, ValueError) as exc:
        raise SwapValidationError("OKX returned an invalid position size") from exc
    try:
        signed = Decimal(str(raw))
    except InvalidOperation as exc:
        raise SwapValidationError("OKX returned an invalid position size") from exc
    if not signed.is_finite():
        raise SwapValidationError("OKX returned an invalid position size")
    return -value if signed < 0 else value


def _find_position(rows: list[Any], position_side: str, margin_mode: str | None = None) -> Any | None:
    side = str(position_side or "").strip().lower()
    eligible = rows
    if margin_mode is not None:
        eligible = [
            row
            for row in rows
            if str(_first(row, ("mgnMode", "margin_mode")) or "").strip().lower() == margin_mode
        ]
    if side == "net":
        matches = [
            row
            for row in eligible
            if str(_first(row, ("posSide",)) or "net").strip().lower() in ("", "net")
        ]
    else:
        matches = [
            row
            for row in eligible
            if str(_first(row, ("posSide",)) or "").strip().lower() == side
        ]
    nonzero = [row for row in matches if _position_contracts(row) != 0]
    if len(nonzero) > 1:
        raise SwapValidationError("OKX returned multiple matching non-zero positions")
    return nonzero[0] if nonzero else (matches[0] if len(matches) == 1 else None)


def _validate_position_for_order(intent: SwapOrderIntent, rows: list[Any]) -> Any | None:
    position = _find_position(rows, intent.position_side, intent.margin_mode)
    contracts = _position_contracts(position) if position is not None else 0
    if intent.reduce_only:
        if position is None or contracts == 0:
            raise SwapValidationError(
                f"reduce-only order has no matching {intent.position_side} position"
            )
        if intent.position_side == "net":
            if contracts > 0 and intent.side != "sell":
                raise SwapValidationError("reducing a net long position requires side='sell'")
            if contracts < 0 and intent.side != "buy":
                raise SwapValidationError("reducing a net short position requires side='buy'")
    elif intent.position_side == "net" and contracts != 0:
        reverses = (contracts > 0 and intent.side == "sell") or (contracts < 0 and intent.side == "buy")
        if reverses:
            raise SwapValidationError(
                "non-reduce order would close or reverse the existing net position; reduce it explicitly first"
            )
    return position


def _validate_reduce_size(resolution: ContractSizeResolution, position: Any | None) -> None:
    if position is None:
        raise SwapValidationError("reduce-only order has no matching position")
    available = abs(_position_contracts(position))
    if resolution.contracts > available:
        raise SwapValidationError(
            f"reduce-only size {decimal_string(resolution.contracts)} exceeds position size "
            f"{decimal_string(available)}"
        )


def _read_last_price(cfg: OKXConfig, symbol: str) -> Any:
    market = _market_client(cfg)
    resp = _safe_read_call(market, "get_ticker", instId=symbol)
    rows = _extract_data(resp)
    if not rows:
        raise OKXConfigError(_resp_message(resp) or f"OKX returned no ticker for {symbol}")
    return as_decimal(_first(rows[0], ("last",)), "last price")


def _lookup_client_order(trade: Any, symbol: str, client_order_id: str) -> tuple[str, Any | None, str | None]:
    try:
        resp = trade.get_order(instId=symbol, clOrdId=client_order_id)
    except Exception as exc:  # noqa: BLE001 - inability to prove absence blocks the write
        return "error", None, str(exc)
    if not isinstance(resp, Mapping):
        return "error", None, "OKX returned a malformed order lookup response"
    code = str(resp.get("code") or "")
    if code == "51603":
        return "absent", None, None
    if code != "0":
        return "error", None, _resp_message(resp) or f"OKX order lookup failed (code={code or '?'})"
    rows = resp.get("data")
    if isinstance(rows, (list, tuple)) and rows:
        return "found", rows[0], None
    return "error", None, "OKX order lookup did not prove whether the client id exists"


def _idempotent_order_result(cfg: OKXConfig, intent: SwapOrderIntent, existing: Any) -> dict[str, Any]:
    order = _order_to_dict(existing)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": intent.instrument_id,
        "order_id": order.get("order_id"),
        "client_order_id": intent.client_order_id,
        "idempotent_replay": True,
        "order": order,
        "verification": {"status": "verified", "order": order},
        "retry_safe": False,
    }


def _verify_order(
    trade: Any,
    symbol: str,
    *,
    order_id: str,
    client_order_id: str,
) -> dict[str, Any]:
    try:
        kwargs = {"ordId": order_id} if order_id else {"clOrdId": client_order_id}
        resp = trade.get_order(instId=symbol, **kwargs)
        rows = _extract_data(resp)
        if not rows:
            return {
                "status": "unknown",
                "error": _resp_message(resp) or "accepted order is not yet visible",
            }
        return {"status": "verified", "order": _order_to_dict(rows[0])}
    except Exception as exc:  # noqa: BLE001 - accepted write must not be retried
        return {"status": "unknown", "error": str(exc)}


def _verify_closed_position(
    cfg: OKXConfig,
    symbol: str,
    position_side: str,
    margin_mode: str,
) -> dict[str, Any]:
    try:
        rows = _read_swap_position_rows(cfg, symbol)
        position = _find_position(rows, position_side, margin_mode)
        remaining = abs(_position_contracts(position)) if position is not None else 0
        if remaining == 0:
            return {"status": "verified", "remaining_contracts": "0"}
        return {
            "status": "pending",
            "remaining_contracts": decimal_string(remaining),
            "position": _position_to_dict(position),
        }
    except Exception as exc:  # noqa: BLE001 - close was accepted; never imply retry
        return {"status": "unknown", "error": str(exc)}


def _mutation_result(cfg: OKXConfig, resp: Any, *, symbol: str, operation: str, **extra: Any) -> dict[str, Any]:
    if not isinstance(resp, Mapping) or str(resp.get("code")) != "0":
        return _order_error(
            cfg,
            _resp_message(resp) or f"OKX rejected {operation}",
            symbol=symbol,
            operation=operation,
            **extra,
        )
    rows = resp.get("data")
    if not isinstance(rows, (list, tuple)) or not rows:
        return _order_error(
            cfg,
            f"OKX returned no data for {operation}",
            symbol=symbol,
            operation=operation,
            **extra,
        )
    row = rows[0]
    s_code = str(_first(row, ("sCode",)) or "0")
    if s_code != "0":
        return _order_error(
            cfg,
            str(_first(row, ("sMsg",)) or f"OKX rejected {operation} (sCode={s_code})"),
            symbol=symbol,
            operation=operation,
            **extra,
        )
    payload: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": symbol,
        "operation": operation,
        "accepted": True,
        "broker_response": dict(row) if isinstance(row, Mapping) else row,
    }
    order_id = str(_first(row, ("ordId",)) or "")
    if order_id:
        payload["order_id"] = order_id
    for key, value in extra.items():
        payload[key] = value
    return payload


def _unknown_write_error(cfg: OKXConfig, message: str, **extra: Any) -> dict[str, Any]:
    return _order_error(
        cfg,
        message,
        write_state="unknown_needs_reconcile",
        retry_safe=False,
        **extra,
    )


# ---------------------------------------------------------------------------
# SDK plumbing
# ---------------------------------------------------------------------------


def _require_okx() -> ModuleType:
    try:
        import okx  # type: ignore
    except ModuleNotFoundError as exc:
        raise OKXDependencyError("python-okx is not installed; run `pip install python-okx`.") from exc
    return okx


def _account_client(cfg: OKXConfig):
    _require_okx()
    from okx.Account import AccountAPI  # type: ignore

    return _configure_http_client(
        AccountAPI(
            cfg.api_key,
            cfg.api_secret,
            cfg.passphrase,
            flag=cfg.flag,
            domain=cfg.host,
            proxy=_proxy_for_config(cfg),
        ),
        cfg,
    )


def _trade_client(cfg: OKXConfig):
    _require_okx()
    from okx.Trade import TradeAPI  # type: ignore

    return _configure_http_client(
        TradeAPI(
            cfg.api_key,
            cfg.api_secret,
            cfg.passphrase,
            flag=cfg.flag,
            domain=cfg.host,
            proxy=_proxy_for_config(cfg),
        ),
        cfg,
    )


def _market_client(cfg: OKXConfig):
    _require_okx()
    from okx.MarketData import MarketAPI  # type: ignore

    return _configure_http_client(
        MarketAPI(
            cfg.api_key or "-1",
            cfg.api_secret or "-1",
            cfg.passphrase or "-1",
            flag=cfg.flag,
            domain=cfg.host,
            proxy=_proxy_for_config(cfg),
        ),
        cfg,
    )


def _public_client(cfg: OKXConfig):
    _require_okx()
    from okx.PublicData import PublicAPI  # type: ignore

    return _configure_http_client(
        PublicAPI(
            cfg.api_key or "-1",
            cfg.api_secret or "-1",
            cfg.passphrase or "-1",
            flag=cfg.flag,
            domain=cfg.host,
            proxy=_proxy_for_config(cfg),
        ),
        cfg,
    )


def _status_client(cfg: OKXConfig):
    _require_okx()
    from okx.Status import StatusAPI  # type: ignore

    return _configure_http_client(
        StatusAPI(
            cfg.api_key or "-1",
            cfg.api_secret or "-1",
            cfg.passphrase or "-1",
            flag=cfg.flag,
            domain=cfg.host,
            proxy=_proxy_for_config(cfg),
        ),
        cfg,
    )


def _proxy_for_config(cfg: OKXConfig) -> str | None:
    """Resolve an explicit or conventional environment proxy for OKX HTTP."""
    for value in (
        cfg.proxy,
        os.getenv("OKX_PROXY", ""),
        os.getenv("HTTPS_PROXY", ""),
        os.getenv("https_proxy", ""),
        os.getenv("ALL_PROXY", ""),
        os.getenv("all_proxy", ""),
    ):
        clean = str(value or "").strip()
        if clean:
            return clean
    return None


def _configure_http_client(client: Any, cfg: OKXConfig) -> Any:
    """Apply the configured timeout omitted by python-okx's constructors."""
    import httpx

    client.timeout = httpx.Timeout(cfg.timeout)
    return client


def _missing_fields(cfg: OKXConfig) -> list[str]:
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.api_secret:
        missing.append("api_secret")
    if not cfg.passphrase:
        missing.append("passphrase")
    return missing


def _public_config(cfg: OKXConfig) -> dict[str, Any]:
    """Config snapshot with secrets redacted."""
    data = asdict(cfg)
    if data.get("api_secret"):
        data["api_secret"] = "***redacted***"
    if data.get("passphrase"):
        data["passphrase"] = "***redacted***"
    if data.get("api_key"):
        data["api_key"] = data["api_key"][:4] + "***"
    if data.get("proxy"):
        data["proxy"] = "***configured***"
    data["flag"] = cfg.flag
    data["is_demo"] = cfg.is_demo
    return data


# ---------------------------------------------------------------------------
# Defensive field extraction (python-okx returns ``{"code":"0","data":[...]}``)
# ---------------------------------------------------------------------------


def _as_iter(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _obj_get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = _obj_get(obj, name, None)
        if value is not None:
            return value
    return default


def _extract_data(resp: Any) -> list[Any]:
    """Return the ``data`` list from an OKX response, or ``[]`` defensively.

    python-okx returns a dict shaped ``{"code": "0", "data": [...]}`` on success.
    Anything else (a non-zero code, a non-dict, a missing/non-list ``data``) is
    treated as empty rather than raising, matching the reference connectors'
    defensive extraction posture.
    """
    if isinstance(resp, Mapping):
        if str(resp.get("code")) != "0":
            return []
        data = resp.get("data")
        return list(data) if isinstance(data, (list, tuple)) else []
    return _as_iter(resp)


def _balance_detail_to_dict(item: Any) -> dict[str, Any]:
    return {
        "currency": _first(item, ("ccy",)),
        "equity": _first(item, ("eq",)),
        "available": _first(item, ("availBal",)),
        "cash_balance": _first(item, ("cashBal",)),
        "frozen": _first(item, ("frozenBal",)),
    }


def _position_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "instrument_type": _first(item, ("instType",)),
        "position_id": str(_first(item, ("posId",)) or ""),
        "side": str(_first(item, ("posSide",)) or ""),
        "quantity": _first(item, ("pos",)),
        "available_quantity": _first(item, ("availPos",)),
        "average_cost": _first(item, ("avgPx",)),
        "mark_price": _first(item, ("markPx",)),
        "notional_usd": _first(item, ("notionalUsd",)),
        "unrealized_pnl": _first(item, ("upl",)),
        "unrealized_pnl_ratio": _first(item, ("uplRatio",)),
        "realized_pnl": _first(item, ("realizedPnl",)),
        "margin_mode": _first(item, ("mgnMode",)),
        "margin": _first(item, ("margin", "imr")),
        "maintenance_margin": _first(item, ("mmr",)),
        "margin_ratio": _first(item, ("mgnRatio",)),
        "liquidation_price": _first(item, ("liqPx",)),
        "leverage": _first(item, ("lever",)),
    }


def _order_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "order_id": str(_first(item, ("ordId",)) or ""),
        "client_order_id": _first(item, ("clOrdId",)),
        "price": _first(item, ("px",)),
        "quantity": _first(item, ("sz",)),
        "order_type": _first(item, ("ordType",)),
        "side": str(_first(item, ("side",)) or ""),
        "position_side": str(_first(item, ("posSide",)) or ""),
        "margin_mode": str(_first(item, ("tdMode",)) or ""),
        "reduce_only": str(_first(item, ("reduceOnly",)) or "").lower() == "true",
        "status": str(_first(item, ("state",)) or ""),
        "filled_qty": _first(item, ("fillSz",)),
        "avg_fill_price": _first(item, ("avgPx",)),
        "acc_filled_qty": _first(item, ("accFillSz",)),
    }


def _fill_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "order_id": str(_first(item, ("ordId",)) or ""),
        "trade_id": str(_first(item, ("tradeId",)) or ""),
        "fill_price": _first(item, ("fillPx",)),
        "fill_qty": _first(item, ("fillSz",)),
        "side": str(_first(item, ("side",)) or ""),
        "position_side": str(_first(item, ("posSide",)) or ""),
        "fee": _first(item, ("fee",)),
        "realized_pnl": _first(item, ("fillPnl", "pnl")),
        "time": str(_first(item, ("ts",)) or ""),
    }


def _algo_order_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "algo_id": str(_first(item, ("algoId",)) or ""),
        "client_algo_order_id": _first(item, ("algoClOrdId", "attachAlgoClOrdId")),
        "order_id": str(_first(item, ("ordId",)) or ""),
        "order_type": str(_first(item, ("ordType",)) or ""),
        "side": str(_first(item, ("side",)) or ""),
        "position_side": str(_first(item, ("posSide",)) or ""),
        "margin_mode": str(_first(item, ("tdMode",)) or ""),
        "quantity": _first(item, ("sz",)),
        "take_profit_trigger": _first(item, ("tpTriggerPx",)),
        "stop_loss_trigger": _first(item, ("slTriggerPx",)),
        "state": str(_first(item, ("state",)) or ""),
    }


def _quote_to_dict(item: Any) -> dict[str, Any]:
    return {
        "last": _first(item, ("last",)),
        "ask": _first(item, ("askPx",)),
        "ask_size": _first(item, ("askSz",)),
        "bid": _first(item, ("bidPx",)),
        "bid_size": _first(item, ("bidSz",)),
        "open_24h": _first(item, ("open24h",)),
        "high_24h": _first(item, ("high24h",)),
        "low_24h": _first(item, ("low24h",)),
        "volume_24h": _first(item, ("vol24h",)),
        "time": str(_first(item, ("ts",)) or ""),
    }


def _candle_to_dict(item: Any) -> dict[str, Any]:
    """Map an OKX candle array ``[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]``.

    OKX candlesticks come back as positional arrays rather than dicts, so this
    indexes defensively and tolerates short rows.
    """
    row = list(item) if isinstance(item, (list, tuple)) else []

    def at(idx: int) -> Any:
        return row[idx] if idx < len(row) else None

    # ``confirm`` is always the LAST element across OKX's 7- and 9-field candle
    # shapes; read it positionally from the tail rather than a fixed index.
    return {
        "time": str(at(0) or ""),
        "open": at(1),
        "high": at(2),
        "low": at(3),
        "close": at(4),
        "volume": at(5),
        "volume_ccy": at(6),
        "confirm": row[-1] if row else None,
    }


def _safe_read_call(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call one idempotent SDK read with bounded transport retries.

    python-okx signatures vary across versions (some read methods accept extra
    keyword filters, some do not). We try the richer call first and fall back to
    the no-arg form. Only transport failures are retried; broker/business errors
    and every write path remain single-attempt.
    """
    fn = getattr(obj, name, None)
    if fn is None:
        return None

    def invoke() -> Any:
        try:
            return fn(*args, **kwargs)
        except TypeError:
            try:
                return fn(*args)
            except TypeError:
                return fn()

    for attempt in range(READ_MAX_ATTEMPTS):
        try:
            return invoke()
        except Exception as exc:
            if not _retryable_read_error(exc) or attempt + 1 >= READ_MAX_ATTEMPTS:
                raise
            logger.warning(
                "transient OKX read failure in %s (%s); retrying attempt %s/%s",
                name,
                type(exc).__name__,
                attempt + 2,
                READ_MAX_ATTEMPTS,
            )
            time.sleep(READ_RETRY_BACKOFF_SECONDS[attempt])

    raise RuntimeError("unreachable OKX read retry state")


def _retryable_read_error(exc: Exception) -> bool:
    """Return whether retrying an idempotent read is safe for this exception."""
    import httpx

    return isinstance(
        exc,
        (httpx.TransportError, ssl.SSLError, ConnectionError, TimeoutError),
    )
