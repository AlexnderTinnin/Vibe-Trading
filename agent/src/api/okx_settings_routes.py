"""Authenticated OKX Demo and live credential settings routes for the Web UI."""

from __future__ import annotations

import sys as _sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.live.promotion import LIVE_SWAP_WRITES_COMPILED
from src.trading.connectors.okx.sdk import (
    DEFAULT_HOST,
    OKXConfig,
    OKXConfigError,
    check_status,
    load_config,
    load_live_config,
    okx_available,
    save_config,
    save_live_config,
)


SwapInstrumentId = Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


class OKXPaperConfigStatus(BaseModel):
    """Public OKX Demo configuration state. No credential value is returned."""

    broker: Literal["okx"] = "okx"
    profile: str
    environment: Literal["demo", "live"]
    is_demo: bool
    configured: bool
    credentials_configured: bool
    api_key_configured: bool
    api_secret_configured: bool
    passphrase_configured: bool
    expected_uid: str
    allowed_swap_instruments: List[SwapInstrumentId]
    max_swap_leverage: float
    proxy_configured: bool
    sdk_installed: bool
    runner_running: bool
    missing_fields: List[str]


class UpdateOKXPaperConfigRequest(BaseModel):
    """Replace or retain the project-local OKX Demo credentials."""

    model_config = ConfigDict(extra="forbid")

    api_key: Optional[SecretStr] = None
    api_secret: Optional[SecretStr] = None
    passphrase: Optional[SecretStr] = None
    expected_uid: str = ""
    allowed_swap_instruments: List[SwapInstrumentId] = Field(
        default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        min_length=1,
        max_length=2,
    )
    max_swap_leverage: float = Field(10.0, ge=1, le=10, allow_inf_nan=False)
    proxy: Optional[SecretStr] = None
    clear_proxy: bool = False


class OKXLiveConfigStatus(BaseModel):
    """Public isolated OKX live read-only configuration state."""

    broker: Literal["okx"] = "okx"
    profile: Literal["live-readonly"] = "live-readonly"
    environment: Literal["live"] = "live"
    is_demo: Literal[False] = False
    configured: bool
    credentials_configured: bool
    api_key_configured: bool
    api_secret_configured: bool
    passphrase_configured: bool
    expected_uid: str
    proxy_configured: bool
    sdk_installed: bool
    live_swap_writes_compiled: bool
    missing_fields: List[str]


class UpdateOKXLiveConfigRequest(BaseModel):
    """Replace or retain isolated project-local OKX live credentials."""

    model_config = ConfigDict(extra="forbid")

    api_key: Optional[SecretStr] = None
    api_secret: Optional[SecretStr] = None
    passphrase: Optional[SecretStr] = None
    expected_uid: str = ""
    proxy: Optional[SecretStr] = None
    clear_proxy: bool = False


class OKXConnectionCheckResponse(BaseModel):
    """Sanitized result of a read-only OKX connection check."""

    broker: Literal["okx"] = "okx"
    ok: bool
    status: Literal["ok", "error"]
    message: str
    is_demo: bool
    uid_verified: bool
    total_equity: Optional[str] = None
    checked_at: str


AuthDep = Callable[..., Awaitable[Any] | Any]


def _host() -> Any:
    return _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")


def _runner_running() -> bool:
    host = _host()
    runners = getattr(host, "_quant_runners", {}) if host is not None else {}
    return runners.get("okx") is not None


def _load_config_or_http_error() -> OKXConfig:
    try:
        return load_config()
    except OKXConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored OKX configuration is invalid; replace it from the Settings page.",
        ) from exc


def _load_live_config_or_http_error() -> OKXConfig:
    try:
        return load_live_config()
    except OKXConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored OKX live configuration is invalid; replace it from the Settings page.",
        ) from exc


def _build_config_status(config: OKXConfig) -> OKXPaperConfigStatus:
    api_key_configured = bool(config.api_key)
    api_secret_configured = bool(config.api_secret)
    passphrase_configured = bool(config.passphrase)
    credentials_configured = all(
        (api_key_configured, api_secret_configured, passphrase_configured)
    )
    missing_fields: list[str] = []
    if not api_key_configured:
        missing_fields.append("api_key")
    if not api_secret_configured:
        missing_fields.append("api_secret")
    if not passphrase_configured:
        missing_fields.append("passphrase")
    if not config.expected_uid:
        missing_fields.append("expected_uid")
    if not config.is_demo:
        missing_fields.append("demo_profile")

    return OKXPaperConfigStatus(
        profile=config.profile,
        environment="demo" if config.is_demo else "live",
        is_demo=config.is_demo,
        configured=credentials_configured and bool(config.expected_uid) and config.is_demo,
        credentials_configured=credentials_configured,
        api_key_configured=api_key_configured,
        api_secret_configured=api_secret_configured,
        passphrase_configured=passphrase_configured,
        expected_uid=config.expected_uid,
        allowed_swap_instruments=list(config.allowed_swap_instruments),
        max_swap_leverage=config.max_swap_leverage,
        proxy_configured=bool(config.proxy),
        sdk_installed=okx_available(),
        runner_running=_runner_running(),
        missing_fields=missing_fields,
    )


def _build_live_config_status(config: OKXConfig) -> OKXLiveConfigStatus:
    api_key_configured = bool(config.api_key)
    api_secret_configured = bool(config.api_secret)
    passphrase_configured = bool(config.passphrase)
    credentials_configured = all(
        (api_key_configured, api_secret_configured, passphrase_configured)
    )
    missing_fields: list[str] = []
    if not api_key_configured:
        missing_fields.append("api_key")
    if not api_secret_configured:
        missing_fields.append("api_secret")
    if not passphrase_configured:
        missing_fields.append("passphrase")
    if not config.expected_uid:
        missing_fields.append("expected_uid")
    if config.profile != "live-readonly" or config.is_demo or not config.readonly:
        missing_fields.append("live_readonly_profile")

    return OKXLiveConfigStatus(
        configured=credentials_configured
        and bool(config.expected_uid)
        and config.profile == "live-readonly"
        and not config.is_demo
        and config.readonly,
        credentials_configured=credentials_configured,
        api_key_configured=api_key_configured,
        api_secret_configured=api_secret_configured,
        passphrase_configured=passphrase_configured,
        expected_uid=config.expected_uid,
        proxy_configured=bool(config.proxy),
        sdk_installed=okx_available(),
        live_swap_writes_compiled=LIVE_SWAP_WRITES_COMPILED,
        missing_fields=missing_fields,
    )


def _secret_value(value: Optional[SecretStr]) -> str:
    return value.get_secret_value().strip() if value is not None else ""


def _load_live_config_for_update(payload: UpdateOKXLiveConfigRequest) -> OKXConfig:
    try:
        return load_live_config()
    except OKXConfigError as exc:
        supplied = (
            _secret_value(payload.api_key),
            _secret_value(payload.api_secret),
            _secret_value(payload.passphrase),
        )
        if all(supplied):
            return OKXConfig(profile="live-readonly")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Stored OKX live configuration is invalid; enter API Key, Secret, "
                "and Passphrase together to replace it"
            ),
        ) from exc


def _validate_plain_value(value: str, *, label: str, max_length: int) -> str:
    clean = value.strip()
    if not clean:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(clean) > max_length or any(ord(char) < 32 for char in clean):
        raise HTTPException(status_code=400, detail=f"{label} is invalid")
    return clean


def _resolve_credentials(
    payload: UpdateOKXPaperConfigRequest | UpdateOKXLiveConfigRequest,
    current: OKXConfig,
    *,
    expected_profile: str,
) -> tuple[str, str, str]:
    supplied = (
        _secret_value(payload.api_key),
        _secret_value(payload.api_secret),
        _secret_value(payload.passphrase),
    )
    supplied_count = sum(bool(value) for value in supplied)
    if supplied_count not in (0, 3):
        raise HTTPException(
            status_code=400,
            detail="API Key, Secret, and Passphrase must be provided together",
        )

    if supplied_count == 0:
        existing = (current.api_key, current.api_secret, current.passphrase)
        if current.profile != expected_profile or not all(existing):
            raise HTTPException(
                status_code=400,
                detail="API Key, Secret, and Passphrase are required for initial setup",
            )
        return existing

    labels = ("API Key", "Secret", "Passphrase")
    limits = (256, 512, 512)
    return tuple(
        _validate_plain_value(value, label=label, max_length=limit)
        for value, label, limit in zip(supplied, labels, limits)
    )  # type: ignore[return-value]


def _resolve_proxy(
    payload: UpdateOKXPaperConfigRequest | UpdateOKXLiveConfigRequest,
    current: OKXConfig,
) -> str:
    if payload.clear_proxy:
        return ""
    value = _secret_value(payload.proxy)
    if not value:
        return current.proxy
    if len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise HTTPException(status_code=400, detail="Proxy URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Proxy URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None and not 1 <= port <= 65535
    ):
        raise HTTPException(
            status_code=400,
            detail="Proxy URL must be an HTTP(S) proxy such as http://127.0.0.1:7890",
        )
    return value


def _redact_message(message: object, config: OKXConfig) -> str:
    clean = str(message or "OKX connection check failed")
    for secret in (config.api_key, config.api_secret, config.passphrase, config.proxy):
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return clean[:1000]


def _check_connection(
    config: OKXConfig,
    *,
    expected_demo: bool,
    environment_label: str,
) -> OKXConnectionCheckResponse:
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        report = check_status(config)
    except Exception as exc:  # noqa: BLE001 - sanitized below
        return OKXConnectionCheckResponse(
            ok=False,
            status="error",
            message=_redact_message(exc, config),
            is_demo=config.is_demo,
            uid_verified=False,
            checked_at=checked_at,
        )

    if report.get("status") != "ok":
        return OKXConnectionCheckResponse(
            ok=False,
            status="error",
            message=_redact_message(report.get("error"), config),
            is_demo=config.is_demo,
            uid_verified=False,
            checked_at=checked_at,
        )

    account = report.get("account") if isinstance(report.get("account"), dict) else {}
    broker_uid = str(account.get("uid") or "")
    uid_verified = bool(broker_uid) and broker_uid == config.expected_uid
    environment_verified = account.get("is_demo") is expected_demo and config.is_demo is expected_demo
    if not uid_verified:
        return OKXConnectionCheckResponse(
            ok=False,
            status="error",
            message=f"Connected, but the OKX {environment_label} UID could not be verified",
            is_demo=config.is_demo,
            uid_verified=False,
            checked_at=checked_at,
        )
    if not environment_verified:
        return OKXConnectionCheckResponse(
            ok=False,
            status="error",
            message=f"The connector did not pass the OKX {environment_label} environment guard",
            is_demo=config.is_demo,
            uid_verified=True,
            checked_at=checked_at,
        )

    total_equity = account.get("total_equity")
    return OKXConnectionCheckResponse(
        ok=True,
        status="ok",
        message=f"OKX {environment_label} connection and UID verified",
        is_demo=config.is_demo,
        uid_verified=True,
        total_equity=str(total_equity) if total_equity is not None else None,
        checked_at=checked_at,
    )


def register_okx_settings_routes(
    app: FastAPI,
    require_local_or_auth: AuthDep | None = None,
    require_settings_write_auth: AuthDep | None = None,
) -> None:
    """Mount isolated Demo and live OKX credential settings routes."""
    host = _host()
    if host is None:
        raise RuntimeError(
            "register_okx_settings_routes: api_server module not in sys.modules"
        )
    if require_local_or_auth is None:
        require_local_or_auth = host.require_local_or_auth
    if require_settings_write_auth is None:
        require_settings_write_auth = host.require_settings_write_auth

    @app.get(
        "/quant/okx/config/status",
        response_model=OKXPaperConfigStatus,
        dependencies=[Depends(require_local_or_auth)],
    )
    async def get_okx_paper_config_status():
        """Return configuration state without returning credential values."""
        return _build_config_status(_load_config_or_http_error())

    @app.put(
        "/quant/okx/config",
        response_model=OKXPaperConfigStatus,
        dependencies=[Depends(require_settings_write_auth)],
    )
    async def update_okx_paper_config(payload: UpdateOKXPaperConfigRequest):
        """Persist a Demo-only OKX connector configuration."""
        if _runner_running():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Stop the OKX quant runner before changing its configuration",
            )

        current = _load_config_or_http_error()
        api_key, api_secret, passphrase = _resolve_credentials(
            payload,
            current,
            expected_profile="paper",
        )
        expected_uid = _validate_plain_value(
            payload.expected_uid,
            label="OKX Demo UID",
            max_length=128,
        )
        proxy = _resolve_proxy(payload, current)

        try:
            config = OKXConfig.from_mapping(
                {
                    **asdict(current),
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "passphrase": passphrase,
                    "profile": "paper",
                    "host": DEFAULT_HOST,
                    "expected_uid": expected_uid,
                    "proxy": proxy,
                    "readonly": False,
                    "allowed_swap_instruments": payload.allowed_swap_instruments,
                    "max_swap_leverage": payload.max_swap_leverage,
                }
            )
            save_config(config)
        except OKXConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not save the OKX Demo configuration",
            ) from exc
        return _build_config_status(config)

    @app.post(
        "/quant/okx/config/check",
        response_model=OKXConnectionCheckResponse,
        dependencies=[Depends(require_settings_write_auth)],
    )
    async def check_okx_paper_connection():
        """Verify Demo credentials and UID using read-only account endpoints."""
        config = _load_config_or_http_error()
        public_status = _build_config_status(config)
        if not public_status.configured:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Complete the OKX Demo configuration before testing the connection",
            )

        return _check_connection(
            config,
            expected_demo=True,
            environment_label="Demo",
        )

    @app.get(
        "/quant/okx/live/config/status",
        response_model=OKXLiveConfigStatus,
        dependencies=[Depends(require_local_or_auth)],
    )
    async def get_okx_live_config_status():
        """Return isolated live configuration state without credential values."""
        return _build_live_config_status(_load_live_config_or_http_error())

    @app.put(
        "/quant/okx/live/config",
        response_model=OKXLiveConfigStatus,
        dependencies=[Depends(require_settings_write_auth)],
    )
    async def update_okx_live_config(payload: UpdateOKXLiveConfigRequest):
        """Persist an isolated live read-only OKX connector configuration."""
        current = _load_live_config_for_update(payload)
        api_key, api_secret, passphrase = _resolve_credentials(
            payload,
            current,
            expected_profile="live-readonly",
        )
        expected_uid = _validate_plain_value(
            payload.expected_uid,
            label="OKX Live UID",
            max_length=128,
        )
        proxy = _resolve_proxy(payload, current)

        try:
            config = OKXConfig.from_mapping(
                {
                    **asdict(current),
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "passphrase": passphrase,
                    "profile": "live-readonly",
                    "host": DEFAULT_HOST,
                    "expected_uid": expected_uid,
                    "proxy": proxy,
                    "readonly": True,
                }
            )
            save_live_config(config)
        except OKXConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not save the OKX live configuration",
            ) from exc
        return _build_live_config_status(config)

    @app.post(
        "/quant/okx/live/config/check",
        response_model=OKXConnectionCheckResponse,
        dependencies=[Depends(require_settings_write_auth)],
    )
    async def check_okx_live_connection():
        """Verify live credentials and UID using read-only account endpoints."""
        config = _load_live_config_or_http_error()
        public_status = _build_live_config_status(config)
        if not public_status.configured:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Complete the OKX live configuration before testing the connection",
            )
        return _check_connection(
            config,
            expected_demo=False,
            environment_label="live",
        )
