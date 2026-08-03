"""Fail-closed OKX perpetual execution gate used by the deterministic runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from src.live.audit import LiveActionEvent, write_live_action
from src.live.daily_count import increment_daily_count, read_daily_count
from src.live.enforcement import DerivativeRiskSnapshot, OrderIntent, check_mandate
from src.live.halt import halt_flag_set
from src.live.mandate.model import MANDATE_SCHEMA_VERSION, AssetClass, InstrumentType, Mandate
from src.live.runtime.swap_state import (
    SwapRuntimeStore,
    SwapStateError,
    swap_breaker_status,
    trip_swap_breaker,
)
from src.trading.connectors.okx.swap import (
    SwapInstrument,
    SwapOrderIntent,
    build_attached_algo_orders,
    decimal_string,
    resolve_contract_size,
)

logger = logging.getLogger(__name__)

_BREAKER_LIMITS = frozenset(
    {
        "max_daily_loss_usd",
        "max_drawdown_pct",
        "max_consecutive_losses",
        "min_liquidation_buffer_pct",
        "derivative_risk_snapshot",
    }
)


def execute_swap_order(
    *,
    broker: str,
    mandate: Mandate,
    sdk_module: Any,
    config: Any,
    swap_intent: SwapOrderIntent,
    instrument: SwapInstrument,
    reference_price: float | str,
    positions: object,
    balance: object,
    derivative_risk: DerivativeRiskSnapshot | None,
    planned_liquidation_price: float | str | None,
    session_id: str = "quant-runtime",
    store: SwapRuntimeStore | None = None,
) -> dict[str, Any]:
    """Validate, durably stage, submit once, verify, and persist one order.

    Live writes remain structurally disabled here. Phase 4's promotion gate can
    grant eligibility, but enabling a real SDK mutation is a separate manual
    release decision and is intentionally absent from this implementation.
    """
    key = (broker or "").strip().lower()
    state_store = store or SwapRuntimeStore(key, environment="demo")
    reason = _preflight_reason(key, mandate, config)
    if reason is not None:
        return _blocked(key, reason, session_id=session_id, mandate=mandate)

    try:
        if swap_breaker_status(key).get("tripped") is True:
            return _blocked(
                key,
                "swap circuit breaker is tripped; manual reconciliation and clear required",
                session_id=session_id,
                mandate=mandate,
            )
    except SwapStateError as exc:
        return _blocked(key, str(exc), session_id=session_id, mandate=mandate)

    try:
        sizing = resolve_contract_size(swap_intent, instrument, reference_price=reference_price)
        attached = build_attached_algo_orders(
            swap_intent,
            instrument,
            reference_price=reference_price,
        ) or []
        normalized = OrderIntent(
            symbol=swap_intent.instrument_id,
            side=swap_intent.side,
            notional_usd=float(sizing.estimated_notional_usdt),
            quantity=float(sizing.contracts),
            instrument_type=InstrumentType.PERPETUAL_SWAP,
            asset_class=AssetClass.CRYPTO,
            contract_value=float(instrument.contract_value),
            reference_price=float(sizing.reference_price),
            leverage=float(swap_intent.leverage),
            reduce_only=swap_intent.reduce_only,
            position_side=swap_intent.position_side,
            margin_mode=swap_intent.margin_mode,
            stop_loss_price=(
                float(swap_intent.stop_loss) if swap_intent.stop_loss is not None else None
            ),
            liquidation_price=(
                float(planned_liquidation_price)
                if planned_liquidation_price not in (None, "")
                else None
            ),
            client_order_id=swap_intent.client_order_id,
        )
    except Exception as exc:  # validation only; no write has occurred
        return _blocked(key, str(exc), session_id=session_id, mandate=mandate)

    breach = check_mandate(
        mandate,
        normalized,
        positions,
        balance,
        broker=key,
        remote_tool="place_swap_order",
        daily_count=read_daily_count(key),
        derivative_risk=derivative_risk,
    )
    if breach is not None:
        if breach.limit in _BREAKER_LIMITS:
            trip_swap_breaker(
                key,
                f"risk limit breached: {breach.limit}",
                attempted_value=breach.attempted_value,
                limit_value=breach.limit_value,
            )
        return _blocked(
            key,
            breach.detail or f"order breaches {breach.limit}",
            session_id=session_id,
            mandate=mandate,
            breach={
                "limit": breach.limit,
                "limit_value": breach.limit_value,
                "attempted_value": breach.attempted_value,
                "kind": breach.kind,
            },
        )

    staged = {
        "client_order_id": swap_intent.client_order_id,
        "symbol": swap_intent.instrument_id,
        "side": swap_intent.side,
        "position_side": swap_intent.position_side,
        "margin_mode": swap_intent.margin_mode,
        "reduce_only": swap_intent.reduce_only,
        "contracts": decimal_string(sizing.contracts),
        "notional_usdt": decimal_string(sizing.estimated_notional_usdt),
        "leverage": decimal_string(swap_intent.leverage),
        "attached_tp_sl": attached,
    }
    try:
        state_store.stage_order(staged)
    except Exception as exc:
        trip_swap_breaker(key, "could not durably stage swap order", error=str(exc))
        blocked = _blocked(
            key,
            f"durable order staging failed: {exc}",
            session_id=session_id,
            mandate=mandate,
        )
        blocked["safety_event"] = (
            "duplicate_order" if "already exists" in str(exc).lower() else "reconcile_unsafe"
        )
        return blocked

    request = {
        "symbol": swap_intent.instrument_id,
        "side": swap_intent.side,
        "size": decimal_string(swap_intent.size),
        "size_unit": swap_intent.size_unit,
        "margin_mode": swap_intent.margin_mode,
        "position_side": swap_intent.position_side,
        "leverage": decimal_string(swap_intent.leverage),
        "client_order_id": swap_intent.client_order_id,
        "order_type": swap_intent.order_type,
        "limit_price": (
            decimal_string(swap_intent.limit_price) if swap_intent.limit_price is not None else None
        ),
        "reduce_only": swap_intent.reduce_only,
        "take_profit": (
            decimal_string(swap_intent.take_profit) if swap_intent.take_profit is not None else None
        ),
        "stop_loss": (
            decimal_string(swap_intent.stop_loss) if swap_intent.stop_loss is not None else None
        ),
        "trigger_price_type": swap_intent.trigger_price_type,
    }
    try:
        result = sdk_module.place_swap_order(config, **request)
    except Exception as exc:  # outcome may be unknown; never retry
        result = {
            "status": "error",
            "error": str(exc),
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
            "client_order_id": swap_intent.client_order_id,
        }

    try:
        persisted = state_store.record_order_result(swap_intent.client_order_id, result)
    except Exception as exc:
        trip_swap_breaker(key, "post-write state persistence failed", error=str(exc))
        result = {
            **dict(result),
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
            "state_tracking": {"status": "error", "error": str(exc)},
        }
    else:
        result = {**dict(result), "state_tracking": persisted}

    if str(result.get("status") or "").lower() == "ok":
        increment_daily_count(key)
    _audit_result(
        key,
        session_id,
        mandate,
        normalized,
        request,
        result,
    )
    return result


def execute_swap_stop_update(
    *,
    broker: str,
    mandate: Mandate,
    sdk_module: Any,
    config: Any,
    symbol: str,
    position_side: str,
    algo_order: Mapping[str, Any],
    current_stop_loss: float | str,
    new_stop_loss: float | str,
    reference_price: float | str,
    request_id: str,
    trigger_price_type: str = "mark",
    session_id: str = "quant-runtime",
    store: SwapRuntimeStore | None = None,
) -> dict[str, Any]:
    """Durably stage, submit once, and verify a strictly tighter Demo stop."""
    key = broker.strip().lower()
    reason = _preflight_reason(key, mandate, config)
    if reason is not None:
        return _blocked(
            key,
            reason,
            session_id=session_id,
            mandate=mandate,
            remote_tool="amend_swap_stop_loss",
        )
    try:
        if swap_breaker_status(key).get("tripped") is True:
            return _blocked(
                key,
                "swap circuit breaker is tripped; manual reconciliation and clear required",
                session_id=session_id,
                mandate=mandate,
                remote_tool="amend_swap_stop_loss",
            )
    except SwapStateError as exc:
        return _blocked(
            key,
            str(exc),
            session_id=session_id,
            mandate=mandate,
            remote_tool="amend_swap_stop_loss",
        )

    clean_symbol = symbol.strip().upper()
    direction = position_side.strip().lower()
    algo_id = str(algo_order.get("algo_id") or algo_order.get("algoId") or "").strip()
    try:
        current = _positive_decimal(current_stop_loss, "current_stop_loss")
        proposed = _positive_decimal(new_stop_loss, "new_stop_loss")
        reference = _positive_decimal(reference_price, "reference_price")
        observed = _positive_decimal(
            algo_order.get("stop_loss_trigger", algo_order.get("slTriggerPx")),
            "algo stop loss",
        )
        if clean_symbol not in mandate.universe.include_symbols:
            raise ValueError("stop update instrument is outside the mandate symbol allowlist")
        if str(algo_order.get("symbol") or algo_order.get("instId") or "").upper() != clean_symbol:
            raise ValueError("stop algo order does not match the position instrument")
        if not algo_id:
            raise ValueError("stop algo order id is unavailable")
        if observed != current:
            raise ValueError("durable position stop differs from the selected broker algo order")
        if direction == "long":
            if proposed <= current:
                raise ValueError("long stop update must strictly tighten the existing stop")
            if proposed >= reference:
                raise ValueError("long stop update is already crossed by the execution quote")
        elif direction == "short":
            if proposed >= current:
                raise ValueError("short stop update must strictly tighten the existing stop")
            if proposed <= reference:
                raise ValueError("short stop update is already crossed by the execution quote")
        else:
            raise ValueError("stop update position side must resolve to long or short")
    except ValueError as exc:
        return _blocked(
            key,
            str(exc),
            session_id=session_id,
            mandate=mandate,
            remote_tool="amend_swap_stop_loss",
        )

    state_store = store or SwapRuntimeStore(key, environment="demo")
    staged = {
        "request_id": request_id,
        "symbol": clean_symbol,
        "position_side": direction,
        "algo_id": algo_id,
        "old_stop_loss": decimal_string(current),
        "new_stop_loss": decimal_string(proposed),
        "trigger_price_type": trigger_price_type,
        "action": "tighten_stop_loss",
    }
    try:
        state_store.stage_stop_amendment(staged)
    except Exception as exc:
        trip_swap_breaker(key, "could not durably stage stop amendment", error=str(exc))
        blocked = _blocked(
            key,
            f"durable stop amendment staging failed: {exc}",
            session_id=session_id,
            mandate=mandate,
            remote_tool="amend_swap_stop_loss",
        )
        blocked["safety_event"] = (
            "duplicate_order" if "already exists" in str(exc).lower() else "reconcile_unsafe"
        )
        return blocked

    request = {
        "symbol": clean_symbol,
        "algo_id": algo_id,
        "stop_loss": decimal_string(proposed),
        "request_id": request_id,
        "trigger_price_type": trigger_price_type,
    }
    try:
        result = sdk_module.amend_swap_stop_loss(config, **request)
    except Exception as exc:  # broker outcome may be unknown; never retry
        result = {
            "status": "error",
            "error": str(exc),
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
            "request_id": request_id,
            "algo_id": algo_id,
        }
    result = dict(result)
    verification = result.get("verification")
    verified_stop = False
    if isinstance(verification, Mapping):
        try:
            verified_stop = (
                str(verification.get("status") or "").lower() == "verified"
                and _positive_decimal(
                    verification.get("observed_stop_loss"),
                    "observed_stop_loss",
                )
                == proposed
            )
        except ValueError:
            verified_stop = False
    if result.get("status") == "ok" and not verified_stop:
        result["write_state"] = "unknown_needs_reconcile"
        result["retry_safe"] = False

    try:
        persisted = state_store.record_stop_amendment_result(request_id, result)
    except Exception as exc:
        trip_swap_breaker(key, "post-amendment state persistence failed", error=str(exc))
        result.update(
            {
                "write_state": "unknown_needs_reconcile",
                "retry_safe": False,
                "state_tracking": {"status": "error", "error": str(exc)},
            }
        )
    else:
        result["state_tracking"] = persisted
    _audit_stop_update(key, session_id, mandate, staged, request, result)
    return result


def execute_swap_close(
    *,
    broker: str,
    mandate: Mandate,
    sdk_module: Any,
    config: Any,
    symbol: str,
    margin_mode: str,
    position_side: str,
    leverage: float | str,
    client_order_id: str,
    session_id: str = "quant-runtime",
    store: SwapRuntimeStore | None = None,
) -> dict[str, Any]:
    """Durably stage and submit one exact whole-position close, once."""
    key = broker.strip().lower()
    reason = _preflight_reason(key, mandate, config)
    if reason is not None:
        return _blocked(key, reason, session_id=session_id, mandate=mandate)
    state_store = store or SwapRuntimeStore(key, environment="demo")
    staged = {
        "client_order_id": client_order_id,
        "symbol": symbol.strip().upper(),
        "position_side": position_side.strip().lower(),
        "margin_mode": margin_mode.strip().lower(),
        "leverage": str(leverage),
        "reduce_only": True,
        "action": "close_entire_position",
    }
    try:
        state_store.stage_order(staged)
    except Exception as exc:
        trip_swap_breaker(key, "could not durably stage swap close", error=str(exc))
        blocked = _blocked(
            key,
            f"durable close staging failed: {exc}",
            session_id=session_id,
            mandate=mandate,
        )
        blocked["safety_event"] = (
            "duplicate_order" if "already exists" in str(exc).lower() else "reconcile_unsafe"
        )
        return blocked
    request = {
        "symbol": staged["symbol"],
        "margin_mode": staged["margin_mode"],
        "position_side": staged["position_side"],
        "leverage": staged["leverage"],
        "client_order_id": client_order_id,
        "auto_cancel": False,
    }
    try:
        result = sdk_module.close_swap_position(config, **request)
    except Exception as exc:  # broker outcome may be unknown
        result = {
            "status": "error",
            "error": str(exc),
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
            "client_order_id": client_order_id,
        }
    try:
        tracked = state_store.record_order_result(client_order_id, result)
    except Exception as exc:
        trip_swap_breaker(key, "post-close state persistence failed", error=str(exc))
        result = {
            **dict(result),
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
            "state_tracking": {"status": "error", "error": str(exc)},
        }
    else:
        result = {**dict(result), "state_tracking": tracked}
    try:
        write_live_action(
            LiveActionEvent(
                kind="order_placed" if result.get("status") == "ok" else "order_rejected",
                session_id=session_id,
                outcome="accepted" if result.get("status") == "ok" else "error",
                server=key,
                remote_tool="close_swap_position",
                intent_normalized=(
                    f"close entire {staged['symbol']} {staged['position_side']} "
                    f"({staged['margin_mode']})"
                ),
                mandate_snapshot_ref=mandate.consent.consent_token_sha256,
                broker_request=request,
                broker_response=dict(result),
                gate_decision={"allowed": True, "risk_reducing": True},
                error=None if result.get("status") == "ok" else str(result.get("error") or "close failed"),
            )
        )
    except Exception:
        logger.warning("failed to audit swap close", exc_info=True)
    return result


def _preflight_reason(broker: str, mandate: Mandate, config: Any) -> str | None:
    if not bool(getattr(config, "is_demo", False)):
        return "live perpetual SDK writes remain disabled; promotion eligibility does not enable execution"
    if mandate.schema_version != MANDATE_SCHEMA_VERSION:
        return "perpetual orders require mandate schema v2"
    if mandate.consent.broker.strip().lower() != broker:
        return "mandate broker does not match the execution channel"
    if InstrumentType.PERPETUAL_SWAP not in mandate.hard_caps.allowed_instruments:
        return "mandate does not permit perpetual swaps"
    if halt_flag_set(broker) or halt_flag_set(None):
        return "trading halt is active"
    try:
        expires = datetime.fromisoformat(mandate.consent.expires_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "mandate expiry is invalid"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires:
        return "mandate has expired"
    return None


def _blocked(
    broker: str,
    reason: str,
    *,
    session_id: str,
    mandate: Mandate,
    breach: Mapping[str, Any] | None = None,
    remote_tool: str = "place_swap_order",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "blocked",
        "broker": broker,
        "reason": reason,
        "retry_safe": True,
    }
    if breach is not None:
        result["breach"] = dict(breach)
    try:
        result["live_action"] = write_live_action(
            LiveActionEvent(
                kind="breach" if breach else "order_rejected",
                session_id=session_id,
                outcome="blocked",
                server=broker,
                remote_tool=remote_tool,
                mandate_snapshot_ref=mandate.consent.consent_token_sha256,
                gate_decision={"allowed": False, "reason": reason, "breach": dict(breach or {})},
                error=reason,
            )
        )
    except Exception:
        logger.warning("failed to audit blocked swap order", exc_info=True)
    return result


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return parsed


def _audit_stop_update(
    broker: str,
    session_id: str,
    mandate: Mandate,
    staged: Mapping[str, Any],
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> None:
    known = response.get("write_state") != "unknown_needs_reconcile"
    ok = str(response.get("status") or "").lower() == "ok" and known
    try:
        write_live_action(
            LiveActionEvent(
                kind="order_placed" if ok else "order_rejected",
                session_id=session_id,
                outcome="accepted" if ok else "error",
                server=broker,
                remote_tool="amend_swap_stop_loss",
                intent_normalized=(
                    f"tighten {staged['symbol']} {staged['position_side']} stop "
                    f"from {staged['old_stop_loss']} to {staged['new_stop_loss']}"
                ),
                mandate_snapshot_ref=mandate.consent.consent_token_sha256,
                broker_request=dict(request),
                broker_response=dict(response),
                gate_decision={"allowed": True, "risk_reducing": True},
                error=None if ok else str(response.get("error") or "stop amendment unverified"),
            )
        )
    except Exception:
        logger.warning("failed to audit stop amendment", exc_info=True)


def _audit_result(
    broker: str,
    session_id: str,
    mandate: Mandate,
    intent: OrderIntent,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> None:
    ok = str(response.get("status") or "").lower() == "ok"
    try:
        write_live_action(
            LiveActionEvent(
                kind="order_placed" if ok else "order_rejected",
                session_id=session_id,
                outcome="accepted" if ok else "error",
                server=broker,
                remote_tool="place_swap_order",
                intent_normalized=(
                    f"{intent.side} {intent.quantity:g} contracts {intent.symbol} "
                    f"{intent.position_side} reduce_only={intent.reduce_only}"
                ),
                mandate_snapshot_ref=mandate.consent.consent_token_sha256,
                broker_request=dict(request),
                broker_response=dict(response),
                gate_decision={"allowed": True, "schema_version": mandate.schema_version},
                error=None if ok else str(response.get("error") or "swap order failed"),
            )
        )
    except Exception:
        logger.warning("failed to audit swap order result", exc_info=True)
