"""Read-only mandate loader.

This module exposes exactly ONE public function, :func:`load_mandate`. There is
deliberately NO ``save_mandate`` / ``set_mandate`` here or in any module
importable by the agent loop or registered as a tool — mandate writes happen
only in the consent UX commit path (``src.live.mandate.commit``, owned by a
separate parcel), which is not reachable from ``loop.py`` / ``worker.py`` /
any ``BaseTool.execute()``. This mirrors the #142 swarm-config trust template:
the protected file is resolved at boot from a fixed user-side path, never from
caller input, agent tool args, or session ``variables`` (see
the live-trading SPEC §3 trust invariant, Mandate §2).

Loading is fail-closed: a missing file, malformed JSON, or a structurally
invalid record yields ``None`` so the enforcement gate (P5) denies all orders
rather than guessing at an unrecognized contract. ``schema_version`` is parsed
and returned as-is; the gate compares it against
:data:`~src.live.mandate.model.MANDATE_SCHEMA_VERSION` and fail-closes on a
mismatch (so an unknown future version is surfaced at the gate, not silently
coerced here).
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone

from src.live.mandate.model import (
    AssetClass,
    ConsentMeta,
    HardCaps,
    InstrumentType,
    Mandate,
    UniverseConstraint,
)
from src.live.paths import broker_dir

logger = logging.getLogger(__name__)

_MANDATE_FILENAME = "mandate.json"


def load_mandate(broker: str) -> Mandate | None:
    """Load the committed mandate for ``broker`` from the protected store.

    Reads ``<runtime_root>/live/<broker>/mandate.json`` (written 0600 by the
    consent commit path). Parsing is strict and fail-closed: any absent file,
    unreadable file, malformed JSON, or structurally invalid record returns
    ``None`` so the gate denies. ``expires_at`` and ``schema_version`` are
    carried through verbatim for the gate to evaluate.

    Args:
        broker: Broker key, e.g. ``"robinhood"``.

    Returns:
        The committed :class:`~src.live.mandate.model.Mandate`, or ``None`` when
        no valid mandate is on file.
    """
    path = broker_dir(broker) / _MANDATE_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("live mandate for %s is unreadable/invalid JSON: %s", broker, exc)
        return None
    try:
        return _parse_mandate(raw, expected_broker=broker)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("live mandate for %s failed structural validation: %s", broker, exc)
        return None


def _parse_mandate(raw: object, *, expected_broker: str | None = None) -> Mandate:
    """Build a :class:`Mandate` from a decoded JSON object (strict).

    Raises:
        KeyError: A required field is missing.
        TypeError: ``raw`` (or a nested section) is not an object.
        ValueError: An enum value or numeric field cannot be parsed.
    """
    if not isinstance(raw, dict):
        raise TypeError("mandate root must be a JSON object")

    schema_version = _strict_int(raw["schema_version"], "schema_version")
    caps = _require_dict(raw["hard_caps"], "hard_caps")
    universe = _require_dict(raw["universe"], "universe")
    consent = _require_dict(raw["consent"], "consent")
    instruments = tuple(
        InstrumentType(value)
        for value in _require_list(caps["allowed_instruments"], "allowed_instruments")
    )
    is_perpetual = InstrumentType.PERPETUAL_SWAP in instruments
    derivative_fields = {
        "max_position_notional_usd",
        "max_correlated_exposure_usd",
        "max_daily_loss_usd",
        "max_drawdown_pct",
        "min_liquidation_buffer_pct",
        "max_margin_utilization_pct",
        "max_consecutive_losses",
        "require_stop_loss",
    }
    if schema_version == 2 and is_perpetual:
        missing = sorted(derivative_fields - set(caps))
        if missing:
            raise ValueError(
                "perpetual mandate v2 is missing derivative caps: " + ", ".join(missing)
            )

    hard_caps = HardCaps(
        account_funding_usd=_strict_float(caps["account_funding_usd"], "account_funding_usd"),
        max_order_notional_usd=_strict_float(
            caps["max_order_notional_usd"], "max_order_notional_usd"
        ),
        max_total_exposure_usd=_strict_float(
            caps["max_total_exposure_usd"], "max_total_exposure_usd"
        ),
        max_leverage=_strict_float(caps["max_leverage"], "max_leverage"),
        allowed_instruments=instruments,
        max_trades_per_day=_strict_int(caps["max_trades_per_day"], "max_trades_per_day"),
        max_position_notional_usd=_strict_float(
            caps.get("max_position_notional_usd", 1.0e18),
            "max_position_notional_usd",
        ),
        max_correlated_exposure_usd=_strict_float(
            caps.get("max_correlated_exposure_usd", 1.0e18),
            "max_correlated_exposure_usd",
        ),
        max_daily_loss_usd=_strict_float(
            caps.get("max_daily_loss_usd", 1.0e18), "max_daily_loss_usd"
        ),
        max_drawdown_pct=_strict_float(
            caps.get("max_drawdown_pct", 1.0), "max_drawdown_pct"
        ),
        min_liquidation_buffer_pct=_strict_float(
            caps.get("min_liquidation_buffer_pct", 0.0), "min_liquidation_buffer_pct"
        ),
        max_margin_utilization_pct=_strict_float(
            caps.get("max_margin_utilization_pct", 1.0), "max_margin_utilization_pct"
        ),
        max_consecutive_losses=_strict_int(
            caps.get("max_consecutive_losses", 2_147_483_647), "max_consecutive_losses"
        ),
        require_stop_loss=_strict_bool(
            caps.get("require_stop_loss", False), "require_stop_loss"
        ),
    )
    universe_constraint = UniverseConstraint(
        asset_classes=tuple(
            AssetClass(value)
            for value in _require_list(universe["asset_classes"], "asset_classes")
        ),
        min_market_cap_usd=_opt_float(universe["min_market_cap_usd"], "min_market_cap_usd"),
        min_avg_daily_volume_usd=_opt_float(
            universe["min_avg_daily_volume_usd"], "min_avg_daily_volume_usd"
        ),
        exclude_symbols=_symbols(universe["exclude_symbols"], "exclude_symbols"),
        include_symbols=_symbols(universe.get("include_symbols", []), "include_symbols"),
    )
    consent_broker = str(consent["broker"]).strip().lower()
    consent_meta = ConsentMeta(
        created_at=str(consent["created_at"]),
        consent_token_sha256=str(consent["consent_token_sha256"]),
        broker=consent_broker,
        account_ref=str(consent["account_ref"]).strip(),
        expires_at=str(consent["expires_at"]),
    )
    _validate_sections(
        schema_version=schema_version,
        hard_caps=hard_caps,
        universe=universe_constraint,
        consent=consent_meta,
        expected_broker=expected_broker,
    )
    return Mandate(
        schema_version=schema_version,
        hard_caps=hard_caps,
        universe=universe_constraint,
        consent=consent_meta,
        # Optional per-mandate halt-behavior flag (SPEC §7.5 #6). Absent on an
        # old mandate.json → False (cancel-only, the safe default), keeping the
        # read backward-compatible. Read on a halt trip by src.live.runtime.flatten.
        flatten_on_halt=_strict_bool(raw.get("flatten_on_halt", False), "flatten_on_halt"),
    )


def _require_dict(value: object, field: str) -> dict:
    """Return ``value`` as a dict or raise ``TypeError`` naming ``field``."""
    if not isinstance(value, dict):
        raise TypeError(f"mandate field {field!r} must be a JSON object")
    return value


def _require_list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise TypeError(f"mandate field {field!r} must be a JSON array")
    return value


def _strict_float(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"mandate field {field!r} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"mandate field {field!r} must be finite")
    return parsed


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"mandate field {field!r} must be an integer")
    parsed = int(value)
    if isinstance(value, float) and value != parsed:
        raise ValueError(f"mandate field {field!r} must be an integer")
    return parsed


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"mandate field {field!r} must be a boolean")
    return value


def _symbols(value: object, field: str) -> tuple[str, ...]:
    rows = _require_list(value, field)
    symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in rows))
    if any(not symbol for symbol in symbols):
        raise ValueError(f"mandate field {field!r} contains a blank symbol")
    return symbols


def _opt_float(value: object, field: str) -> float | None:
    """Coerce an optional numeric field to ``float | None``."""
    return None if value is None else _strict_float(value, field)


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"mandate field {field!r} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"mandate field {field!r} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_sections(
    *,
    schema_version: int,
    hard_caps: HardCaps,
    universe: UniverseConstraint,
    consent: ConsentMeta,
    expected_broker: str | None,
) -> None:
    if schema_version <= 0:
        raise ValueError("mandate schema_version must be positive")
    if not hard_caps.allowed_instruments or not universe.asset_classes:
        raise ValueError("mandate instrument and asset-class allowlists must not be empty")
    positive_caps = {
        "account_funding_usd": hard_caps.account_funding_usd,
        "max_order_notional_usd": hard_caps.max_order_notional_usd,
        "max_total_exposure_usd": hard_caps.max_total_exposure_usd,
        "max_leverage": hard_caps.max_leverage,
        "max_position_notional_usd": hard_caps.max_position_notional_usd,
        "max_correlated_exposure_usd": hard_caps.max_correlated_exposure_usd,
        "max_daily_loss_usd": hard_caps.max_daily_loss_usd,
    }
    if any(value <= 0 for value in positive_caps.values()):
        raise ValueError("mandate quantitative caps must be positive")
    if hard_caps.max_leverage < 1 or hard_caps.max_trades_per_day < 1:
        raise ValueError("mandate leverage/trade-count caps are invalid")
    if not 0 < hard_caps.max_drawdown_pct <= 1:
        raise ValueError("mandate max_drawdown_pct must be in (0, 1]")
    if not 0 <= hard_caps.min_liquidation_buffer_pct < 1:
        raise ValueError("mandate min_liquidation_buffer_pct must be in [0, 1)")
    if not 0 < hard_caps.max_margin_utilization_pct <= 1:
        raise ValueError("mandate max_margin_utilization_pct must be in (0, 1]")
    if hard_caps.max_consecutive_losses < 1:
        raise ValueError("mandate max_consecutive_losses must be positive")
    for name, value in (
        ("min_market_cap_usd", universe.min_market_cap_usd),
        ("min_avg_daily_volume_usd", universe.min_avg_daily_volume_usd),
    ):
        if value is not None and value < 0:
            raise ValueError(f"mandate field {name!r} must be non-negative")
    if expected_broker is not None and consent.broker != expected_broker.strip().lower():
        raise ValueError("mandate consent broker does not match its store")
    if not consent.consent_token_sha256.strip():
        raise ValueError("mandate consent token must not be blank")
    created = _parse_timestamp(consent.created_at, "created_at")
    expires = _parse_timestamp(consent.expires_at, "expires_at")
    if InstrumentType.PERPETUAL_SWAP in hard_caps.allowed_instruments:
        if schema_version != 2:
            raise ValueError("perpetual mandates require schema v2")
        if AssetClass.CRYPTO not in universe.asset_classes:
            raise ValueError("perpetual mandates require the crypto asset class")
        if not universe.include_symbols:
            raise ValueError("perpetual mandates require an explicit symbol allowlist")
        if not hard_caps.require_stop_loss:
            raise ValueError("perpetual mandates must require stop losses")
        if not consent.account_ref:
            raise ValueError("perpetual mandates require a dedicated account reference")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", consent.consent_token_sha256):
            raise ValueError("perpetual mandate consent token must be a SHA-256 digest")
        if expires <= created:
            raise ValueError("perpetual mandate expiry must be later than creation")
