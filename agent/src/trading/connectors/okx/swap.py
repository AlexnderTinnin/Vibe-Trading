"""Pure domain model and validation for OKX USDT perpetual swaps.

This module contains no network calls.  The SDK adapter supplies instrument and
account data, then uses these helpers to validate an explicit order intent and
convert quote/margin sizing into contract counts without floating-point drift.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Iterable, Literal, Mapping

SwapSizeUnit = Literal["contracts", "quote_ccy", "margin"]
SwapMarginMode = Literal["cross", "isolated"]
SwapPositionSide = Literal["net", "long", "short"]

SUPPORTED_SWAP_INSTRUMENTS = frozenset({"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
MAX_DEMO_LEVERAGE = Decimal("10")

_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SIZE_UNITS = {"contracts", "base_ccy", "quote_ccy", "margin"}
_MARGIN_MODES = {"cross", "isolated"}
_POSITION_SIDES = {"net", "long", "short"}
_ORDER_TYPES = {"market", "limit"}
_TRIGGER_PRICE_TYPES = {"last", "index", "mark"}


class SwapValidationError(ValueError):
    """Raised when a swap request is unsafe or ambiguous."""


def as_decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    """Parse a finite positive decimal without binary floating-point math."""
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise SwapValidationError(f"{name} must be a valid number") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        requirement = "non-negative" if allow_zero else "positive"
        raise SwapValidationError(f"{name} must be a finite {requirement} number")
    return parsed


def optional_decimal(value: Any, name: str) -> Decimal | None:
    """Parse an optional positive decimal."""
    if value in (None, ""):
        return None
    return as_decimal(value, name)


def decimal_string(value: Decimal) -> str:
    """Render a Decimal without exponent notation or insignificant zeroes."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` down to the nearest positive ``step``."""
    if step <= 0:
        raise SwapValidationError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def is_step_aligned(value: Decimal, step: Decimal) -> bool:
    """Return whether ``value`` is an exact multiple of ``step``."""
    if step <= 0:
        return False
    quotient = value / step
    return quotient == quotient.to_integral_value()


def validate_client_order_id(value: Any) -> str:
    """Validate the stable OKX client order id used for idempotency."""
    clean = str(value or "").strip()
    if not _CLIENT_ORDER_ID_RE.fullmatch(clean):
        raise SwapValidationError(
            "client_order_id must be 1-32 characters using only letters, digits, '-' or '_'"
        )
    return clean


def normalize_allowed_instruments(values: Iterable[Any] | str | None) -> tuple[str, ...]:
    """Normalize a config allowlist without allowing Phase-1 scope expansion."""
    if values is None:
        return tuple(sorted(SUPPORTED_SWAP_INSTRUMENTS))
    if isinstance(values, str):
        source = values.split(",")
    else:
        source = values
    normalized = tuple(dict.fromkeys(str(value).strip().upper() for value in source if str(value).strip()))
    if not normalized:
        raise SwapValidationError("allowed_swap_instruments must not be empty")
    unsupported = sorted(set(normalized) - SUPPORTED_SWAP_INSTRUMENTS)
    if unsupported:
        raise SwapValidationError(
            "Phase 1 supports only BTC-USDT-SWAP and ETH-USDT-SWAP; "
            f"unsupported allowlist entries: {', '.join(unsupported)}"
        )
    return normalized


@dataclass(frozen=True)
class SwapInstrument:
    """Normalized OKX product metadata required for safe contract sizing."""

    instrument_id: str
    instrument_type: str
    state: str
    base_currency: str
    quote_currency: str
    settle_currency: str
    contract_type: str
    contract_value: Decimal
    contract_value_currency: str
    lot_size: Decimal
    minimum_size: Decimal
    tick_size: Decimal
    maximum_market_size: Decimal | None = None
    maximum_limit_size: Decimal | None = None

    @classmethod
    def from_okx(
        cls,
        row: Mapping[str, Any],
        *,
        expected_instrument_id: str,
        allowed_instruments: Iterable[str] = SUPPORTED_SWAP_INSTRUMENTS,
    ) -> "SwapInstrument":
        """Validate and normalize one public ``SWAP`` instrument row."""
        expected = str(expected_instrument_id or "").strip().upper()
        allowed = {str(item).strip().upper() for item in allowed_instruments}
        if expected not in SUPPORTED_SWAP_INSTRUMENTS or expected not in allowed:
            raise SwapValidationError(f"swap instrument {expected or '<blank>'} is not allowed")

        instrument_id = str(row.get("instId") or "").strip().upper()
        if instrument_id != expected:
            raise SwapValidationError(
                f"instrument metadata mismatch: requested {expected}, received {instrument_id or '<blank>'}"
            )
        instrument_type = str(row.get("instType") or "").strip().upper()
        if instrument_type != "SWAP":
            raise SwapValidationError(f"{instrument_id} is not a SWAP instrument")
        state = str(row.get("state") or "").strip().lower()
        if state != "live":
            raise SwapValidationError(f"{instrument_id} is not tradable (state={state or 'unknown'})")

        parts = instrument_id.split("-")
        if len(parts) != 3 or parts[1:] != ["USDT", "SWAP"]:
            raise SwapValidationError(f"{instrument_id} is not a USDT-margined perpetual swap")
        base_currency = parts[0]
        quote_currency = parts[1]
        settle_currency = str(row.get("settleCcy") or "").strip().upper()
        contract_type = str(row.get("ctType") or "").strip().lower()
        contract_value_currency = str(row.get("ctValCcy") or "").strip().upper()
        if settle_currency != "USDT" or contract_type != "linear":
            raise SwapValidationError(f"{instrument_id} must be a linear USDT-settled swap")
        if contract_value_currency != base_currency:
            raise SwapValidationError(
                f"unsupported contract value currency {contract_value_currency or '<blank>'} for {instrument_id}"
            )

        return cls(
            instrument_id=instrument_id,
            instrument_type=instrument_type,
            state=state,
            base_currency=base_currency,
            quote_currency=quote_currency,
            settle_currency=settle_currency,
            contract_type=contract_type,
            contract_value=as_decimal(row.get("ctVal"), "ctVal"),
            contract_value_currency=contract_value_currency,
            lot_size=as_decimal(row.get("lotSz"), "lotSz"),
            minimum_size=as_decimal(row.get("minSz"), "minSz"),
            tick_size=as_decimal(row.get("tickSz"), "tickSz"),
            maximum_market_size=_optional_metadata_decimal(row.get("maxMktSz"), "maxMktSz"),
            maximum_limit_size=_optional_metadata_decimal(row.get("maxLmtSz"), "maxLmtSz"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with exact decimal strings."""
        return {
            "instrument_id": self.instrument_id,
            "instrument_type": self.instrument_type,
            "state": self.state,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "settle_currency": self.settle_currency,
            "contract_type": self.contract_type,
            "contract_value": decimal_string(self.contract_value),
            "contract_value_currency": self.contract_value_currency,
            "lot_size": decimal_string(self.lot_size),
            "minimum_size": decimal_string(self.minimum_size),
            "tick_size": decimal_string(self.tick_size),
            "maximum_market_size": (
                decimal_string(self.maximum_market_size) if self.maximum_market_size is not None else None
            ),
            "maximum_limit_size": (
                decimal_string(self.maximum_limit_size) if self.maximum_limit_size is not None else None
            ),
        }


def _optional_metadata_decimal(value: Any, name: str) -> Decimal | None:
    if value in (None, "", "0", 0):
        return None
    return as_decimal(value, name)


@dataclass(frozen=True)
class SwapOrderIntent:
    """Explicit derivative order intent before exchange metadata conversion."""

    instrument_id: str
    side: Literal["buy", "sell"]
    size: Decimal
    size_unit: SwapSizeUnit
    order_type: Literal["market", "limit"]
    margin_mode: SwapMarginMode
    position_side: SwapPositionSide
    leverage: Decimal
    client_order_id: str
    limit_price: Decimal | None
    reduce_only: bool
    take_profit: Decimal | None
    stop_loss: Decimal | None
    trigger_price_type: Literal["last", "index", "mark"]

    @classmethod
    def from_values(
        cls,
        *,
        instrument_id: Any,
        side: Any,
        size: Any,
        size_unit: Any,
        order_type: Any,
        margin_mode: Any,
        position_side: Any,
        leverage: Any,
        client_order_id: Any,
        limit_price: Any = None,
        reduce_only: bool = False,
        take_profit: Any = None,
        stop_loss: Any = None,
        trigger_price_type: Any = "mark",
        allowed_instruments: Iterable[str] = SUPPORTED_SWAP_INSTRUMENTS,
        max_leverage: Decimal = MAX_DEMO_LEVERAGE,
    ) -> "SwapOrderIntent":
        """Normalize untrusted tool/service arguments and reject ambiguity."""
        clean_instrument = str(instrument_id or "").strip().upper()
        allowed = {str(item).strip().upper() for item in allowed_instruments}
        if clean_instrument not in SUPPORTED_SWAP_INSTRUMENTS or clean_instrument not in allowed:
            raise SwapValidationError(f"swap instrument {clean_instrument or '<blank>'} is not allowed")

        clean_side = str(side or "").strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise SwapValidationError("side must be 'buy' or 'sell'")
        clean_unit = str(size_unit or "").strip().lower()
        if clean_unit not in _SIZE_UNITS:
            raise SwapValidationError("size_unit must be contracts, quote_ccy, or margin")
        if clean_unit == "base_ccy":
            clean_unit = "contracts"
        clean_order_type = str(order_type or "").strip().lower()
        if clean_order_type not in _ORDER_TYPES:
            raise SwapValidationError("order_type must be 'market' or 'limit'")
        clean_margin_mode = str(margin_mode or "").strip().lower()
        if clean_margin_mode not in _MARGIN_MODES:
            raise SwapValidationError("margin_mode must be 'cross' or 'isolated'")
        clean_position_side = str(position_side or "").strip().lower()
        if clean_position_side not in _POSITION_SIDES:
            raise SwapValidationError("position_side must be 'net', 'long', or 'short'")
        clean_trigger_type = str(trigger_price_type or "").strip().lower()
        if clean_trigger_type not in _TRIGGER_PRICE_TYPES:
            raise SwapValidationError("trigger_price_type must be 'last', 'index', or 'mark'")

        clean_leverage = as_decimal(leverage, "leverage")
        if clean_leverage < 1:
            raise SwapValidationError("leverage must be at least 1")
        if clean_leverage > max_leverage:
            raise SwapValidationError(
                f"leverage {decimal_string(clean_leverage)} exceeds configured maximum "
                f"{decimal_string(max_leverage)}"
            )

        clean_limit_price = optional_decimal(limit_price, "limit_price")
        if clean_order_type == "limit" and clean_limit_price is None:
            raise SwapValidationError("limit order requires limit_price")
        if clean_order_type == "market" and clean_limit_price is not None:
            raise SwapValidationError("market order must not include limit_price")

        clean_take_profit = optional_decimal(take_profit, "take_profit")
        clean_stop_loss = optional_decimal(stop_loss, "stop_loss")
        if bool(reduce_only) and (clean_take_profit is not None or clean_stop_loss is not None):
            raise SwapValidationError("reduce-only orders cannot attach new take-profit or stop-loss orders")

        if clean_position_side == "long":
            expected_side = "sell" if reduce_only else "buy"
            if clean_side != expected_side:
                action = "reduce" if reduce_only else "open/increase"
                raise SwapValidationError(f"{action} long requires side={expected_side}")
        elif clean_position_side == "short":
            expected_side = "buy" if reduce_only else "sell"
            if clean_side != expected_side:
                action = "reduce" if reduce_only else "open/increase"
                raise SwapValidationError(f"{action} short requires side={expected_side}")

        return cls(
            instrument_id=clean_instrument,
            side=clean_side,  # type: ignore[arg-type]
            size=as_decimal(size, "size"),
            size_unit=clean_unit,  # type: ignore[arg-type]
            order_type=clean_order_type,  # type: ignore[arg-type]
            margin_mode=clean_margin_mode,  # type: ignore[arg-type]
            position_side=clean_position_side,  # type: ignore[arg-type]
            leverage=clean_leverage,
            client_order_id=validate_client_order_id(client_order_id),
            limit_price=clean_limit_price,
            reduce_only=bool(reduce_only),
            take_profit=clean_take_profit,
            stop_loss=clean_stop_loss,
            trigger_price_type=clean_trigger_type,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ContractSizeResolution:
    """Auditable conversion from an explicit size unit to OKX contracts."""

    requested_size: Decimal
    requested_unit: SwapSizeUnit
    contracts: Decimal
    reference_price: Decimal
    estimated_notional_usdt: Decimal
    estimated_margin_usdt: Decimal
    leverage: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "requested_size": decimal_string(self.requested_size),
            "requested_unit": self.requested_unit,
            "contracts": decimal_string(self.contracts),
            "reference_price": decimal_string(self.reference_price),
            "estimated_notional_usdt": decimal_string(self.estimated_notional_usdt),
            "estimated_margin_usdt": decimal_string(self.estimated_margin_usdt),
            "leverage": decimal_string(self.leverage),
        }


def resolve_contract_size(
    intent: SwapOrderIntent,
    instrument: SwapInstrument,
    *,
    reference_price: Any,
) -> ContractSizeResolution:
    """Resolve contract count using current product metadata and exact decimals."""
    price = as_decimal(reference_price, "reference_price")
    if intent.limit_price is not None and not is_step_aligned(intent.limit_price, instrument.tick_size):
        raise SwapValidationError(
            f"limit_price is not aligned to tickSz {decimal_string(instrument.tick_size)}"
        )
    one_contract_notional = instrument.contract_value * price
    if one_contract_notional <= 0:
        raise SwapValidationError("contract notional must be positive")

    if intent.size_unit == "contracts":
        contracts = intent.size
        if not is_step_aligned(contracts, instrument.lot_size):
            raise SwapValidationError(
                f"contract size {decimal_string(contracts)} is not aligned to lotSz "
                f"{decimal_string(instrument.lot_size)}"
            )
    else:
        target_notional = intent.size
        if intent.size_unit == "margin":
            target_notional *= intent.leverage
        contracts = floor_to_step(target_notional / one_contract_notional, instrument.lot_size)

    if contracts < instrument.minimum_size:
        raise SwapValidationError(
            f"resolved contract size {decimal_string(contracts)} is below minSz "
            f"{decimal_string(instrument.minimum_size)}"
        )
    maximum = (
        instrument.maximum_market_size
        if intent.order_type == "market"
        else instrument.maximum_limit_size
    )
    if maximum is not None and contracts > maximum:
        raise SwapValidationError(
            f"resolved contract size {decimal_string(contracts)} exceeds product maximum "
            f"{decimal_string(maximum)}"
        )

    estimated_notional = contracts * one_contract_notional
    return ContractSizeResolution(
        requested_size=intent.size,
        requested_unit=intent.size_unit,
        contracts=contracts,
        reference_price=price,
        estimated_notional_usdt=estimated_notional,
        estimated_margin_usdt=estimated_notional / intent.leverage,
        leverage=intent.leverage,
    )


def build_attached_algo_orders(
    intent: SwapOrderIntent,
    instrument: SwapInstrument,
    *,
    reference_price: Any,
) -> list[dict[str, str]] | None:
    """Build and validate one exchange-native attached TP/SL payload."""
    if intent.take_profit is None and intent.stop_loss is None:
        return None
    reference = as_decimal(reference_price, "reference_price")
    is_long = intent.position_side == "long" or (
        intent.position_side == "net" and intent.side == "buy"
    )

    if intent.take_profit is not None:
        if not is_step_aligned(intent.take_profit, instrument.tick_size):
            raise SwapValidationError("take_profit is not aligned to instrument tickSz")
        if is_long and intent.take_profit <= reference:
            raise SwapValidationError("long take_profit must be above the reference price")
        if not is_long and intent.take_profit >= reference:
            raise SwapValidationError("short take_profit must be below the reference price")
    if intent.stop_loss is not None:
        if not is_step_aligned(intent.stop_loss, instrument.tick_size):
            raise SwapValidationError("stop_loss is not aligned to instrument tickSz")
        if is_long and intent.stop_loss >= reference:
            raise SwapValidationError("long stop_loss must be below the reference price")
        if not is_long and intent.stop_loss <= reference:
            raise SwapValidationError("short stop_loss must be above the reference price")

    attached: dict[str, str] = {
        "attachAlgoClOrdId": _derived_algo_client_order_id(intent.client_order_id),
    }
    if intent.take_profit is not None:
        attached.update(
            {
                "tpTriggerPx": decimal_string(intent.take_profit),
                "tpOrdPx": "-1",
                "tpTriggerPxType": intent.trigger_price_type,
            }
        )
    if intent.stop_loss is not None:
        attached.update(
            {
                "slTriggerPx": decimal_string(intent.stop_loss),
                "slOrdPx": "-1",
                "slTriggerPxType": intent.trigger_price_type,
            }
        )
    return [attached]


def _derived_algo_client_order_id(client_order_id: str) -> str:
    digest = hashlib.sha256(f"{client_order_id}:risk".encode("ascii")).hexdigest()[:8]
    prefix = client_order_id[:18]
    return f"{prefix}-risk-{digest}"[:32]
