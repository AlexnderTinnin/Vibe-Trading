"""Durable OKX perpetual state, reconciliation, and circuit breaking.

The store is written before every broker mutation and advanced only after a
verified response. An unknown write can therefore be reconciled after a crash
without ever resending it. This module accepts read callables only during
reconciliation; it has no broker write capability.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.live.enforcement import DerivativeRiskSnapshot
from src.live.paths import broker_dir

SWAP_RUNTIME_SCHEMA_VERSION = 2
_STATE_FILENAME = "swap_runtime_state.json"
_BREAKER_FILENAME = "swap_circuit_breaker.json"


class SwapStateError(RuntimeError):
    """Raised when durable swap state cannot be trusted."""


class SwapOrderStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    SUBMITTING = "submitting"
    ACCEPTED_UNVERIFIED = "accepted_unverified"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN_NEEDS_RECONCILE = "unknown_needs_reconcile"


_UNRESOLVED_STATUSES = frozenset(
    {
        SwapOrderStatus.SUBMITTING.value,
        SwapOrderStatus.ACCEPTED_UNVERIFIED.value,
        SwapOrderStatus.UNKNOWN_NEEDS_RECONCILE.value,
    }
)


@dataclass(frozen=True)
class SwapReconcileIssue:
    kind: str
    subject: str
    identity: str
    detail: str
    recorded: Mapping[str, Any] | None = None
    broker: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SwapReconcileReport:
    broker: str
    ts: str
    issues: tuple[SwapReconcileIssue, ...]
    state_persisted: bool
    had_prior_state: bool

    @property
    def is_safe(self) -> bool:
        return not self.issues

    @property
    def requires_halt(self) -> bool:
        return not self.is_safe


ReadPayload = Callable[[], object]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _state_path(broker: str) -> Path:
    return broker_dir(broker) / _STATE_FILENAME


def _breaker_path(broker: str) -> Path:
    return broker_dir(broker) / _BREAKER_FILENAME


def _empty_state(broker: str, *, environment: str = "demo") -> dict[str, Any]:
    return {
        "schema_version": SWAP_RUNTIME_SCHEMA_VERSION,
        "broker": broker,
        "environment": environment,
        "updated_at": _utc_now(),
        "account_configuration": {},
        "balance": {},
        "orders": [],
        "open_orders": [],
        "fills": [],
        "positions": [],
        "algo_orders": [],
        "risk": {
            "trading_day": "",
            "daily_realized_pnl_usd": 0.0,
            "peak_equity_usd": 0.0,
            "consecutive_losses": 0,
        },
    }


class SwapRuntimeStore:
    """Atomic state store for one broker environment."""

    def __init__(self, broker: str = "okx", *, environment: str = "demo") -> None:
        self.broker = broker.strip().lower()
        self.environment = environment.strip().lower()
        if self.environment not in ("demo", "live"):
            raise ValueError("environment must be demo or live")

    @property
    def path(self) -> Path:
        return _state_path(self.broker)

    def load(self, *, required: bool = False) -> dict[str, Any] | None:
        if not self.path.is_file():
            if required:
                raise SwapStateError("swap runtime state is missing")
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SwapStateError("swap runtime state is unreadable; manual reconciliation required") from exc
        if not isinstance(raw, dict):
            raise SwapStateError("swap runtime state root must be an object")
        if raw.get("schema_version") != SWAP_RUNTIME_SCHEMA_VERSION:
            raise SwapStateError("unsupported swap runtime state schema")
        if str(raw.get("broker") or "").lower() != self.broker:
            raise SwapStateError("swap runtime state broker mismatch")
        if str(raw.get("environment") or "").lower() != self.environment:
            raise SwapStateError("swap runtime state environment mismatch")
        for key in ("orders", "open_orders", "fills", "positions", "algo_orders"):
            if not isinstance(raw.get(key), list):
                raise SwapStateError(f"swap runtime state field {key} must be a list")
        return raw

    def save(self, state: Mapping[str, Any]) -> None:
        payload = dict(state)
        payload["schema_version"] = SWAP_RUNTIME_SCHEMA_VERSION
        payload["broker"] = self.broker
        payload["environment"] = self.environment
        payload["updated_at"] = _utc_now()
        _atomic_write(self.path, payload)

    def stage_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """Persist SUBMITTING before the SDK call; duplicate ids fail closed."""
        client_id = str(order.get("client_order_id") or "").strip()
        if not client_id:
            raise SwapStateError("client_order_id is required before submission")
        state = self.load() or _empty_state(self.broker, environment=self.environment)
        existing = next(
            (row for row in state["orders"] if str(row.get("client_order_id") or "") == client_id),
            None,
        )
        if existing is not None:
            raise SwapStateError(f"client_order_id {client_id!r} already exists in durable state")
        row = dict(order)
        row.update({"status": SwapOrderStatus.SUBMITTING.value, "staged_at": _utc_now()})
        state["orders"].append(row)
        self.save(state)
        return row

    def record_order_result(self, client_order_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        state = self.load(required=True)
        assert state is not None
        row = next(
            (item for item in state["orders"] if str(item.get("client_order_id") or "") == client_order_id),
            None,
        )
        if row is None:
            raise SwapStateError("cannot record a result for an unstaged order")
        verification = result.get("verification") if isinstance(result.get("verification"), Mapping) else {}
        if str(result.get("status") or "").lower() == "ok":
            observed = verification.get("order") if isinstance(verification, Mapping) else None
            status = _normalize_order_status(
                observed.get("status") if isinstance(observed, Mapping) else None,
                fallback=SwapOrderStatus.ACCEPTED_UNVERIFIED,
            )
        elif result.get("write_state") == SwapOrderStatus.UNKNOWN_NEEDS_RECONCILE.value:
            status = SwapOrderStatus.UNKNOWN_NEEDS_RECONCILE
        else:
            status = SwapOrderStatus.REJECTED
        row.update(
            {
                "status": status.value,
                "order_id": str(result.get("order_id") or row.get("order_id") or ""),
                "result_recorded_at": _utc_now(),
                "verification_status": str(verification.get("status") or ""),
            }
        )
        self.save(state)
        if status is SwapOrderStatus.UNKNOWN_NEEDS_RECONCILE:
            trip_swap_breaker(self.broker, "order outcome unknown", client_order_id=client_order_id)
        return dict(row)

    def risk_snapshot(self) -> dict[str, Any]:
        state = self.load() or _empty_state(self.broker, environment=self.environment)
        return dict(state.get("risk") or {})

    def derivative_risk_snapshot(self, *, position_side: str) -> DerivativeRiskSnapshot:
        """Build the risk-engine input from the last clean broker snapshot."""
        state = self.load(required=True)
        assert state is not None
        risk = dict(state.get("risk") or {})
        equity = float(risk.get("equity_usd") or 0.0)
        peak = float(risk.get("peak_equity_usd") or 0.0)
        if equity <= 0 or peak <= 0:
            raise SwapStateError("equity risk baseline is unavailable")
        wanted = position_side.strip().lower()
        correlated = 0.0
        margin_used = 0.0
        for row in state["positions"]:
            qty = _decimal(row, "quantity", "pos", "qty")
            if qty == 0:
                continue
            side = str(row.get("position_side") or row.get("side") or "net").lower()
            direction = side
            if side == "net":
                direction = "long" if qty > 0 else "short"
            notional = abs(_decimal(row, "notional_usd", "notionalUsd", "notional"))
            if direction == wanted or (wanted == "net" and side == "net"):
                correlated += float(notional)
            margin_used += float(abs(_decimal(row, "margin", "imr")))
        return DerivativeRiskSnapshot(
            equity_usd=equity,
            peak_equity_usd=peak,
            daily_realized_pnl_usd=float(risk.get("daily_realized_pnl_usd") or 0.0),
            correlated_exposure_usd=correlated,
            margin_used_usd=margin_used,
            consecutive_losses=int(risk.get("consecutive_losses") or 0),
        )


def _normalize_order_status(value: object, *, fallback: SwapOrderStatus) -> SwapOrderStatus:
    clean = str(value or "").strip().lower()
    aliases = {
        "live": SwapOrderStatus.OPEN,
        "open": SwapOrderStatus.OPEN,
        "partially_filled": SwapOrderStatus.PARTIALLY_FILLED,
        "partially-filled": SwapOrderStatus.PARTIALLY_FILLED,
        "filled": SwapOrderStatus.FILLED,
        "canceled": SwapOrderStatus.CANCELLED,
        "cancelled": SwapOrderStatus.CANCELLED,
        "rejected": SwapOrderStatus.REJECTED,
    }
    return aliases.get(clean, fallback)


def trip_swap_breaker(broker: str, reason: str, **context: Any) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "broker": broker.strip().lower(),
        "tripped": True,
        "tripped_at": _utc_now(),
        "reason": str(reason),
        "context": dict(context),
        "manual_clear_required": True,
    }
    _atomic_write(_breaker_path(broker), payload)
    return payload


def swap_breaker_status(broker: str) -> dict[str, Any]:
    path = _breaker_path(broker)
    if not path.is_file():
        return {"broker": broker.strip().lower(), "tripped": False}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SwapStateError("swap circuit-breaker state is unreadable (fail-closed)") from exc
    if not isinstance(raw, dict) or raw.get("tripped") is not True:
        raise SwapStateError("swap circuit-breaker state is invalid (fail-closed)")
    return raw


def clear_swap_breaker(broker: str, *, authorized_by: str, note: str) -> bool:
    """Manual surface operation; never exposed as an agent tool."""
    if not authorized_by.strip() or not note.strip():
        raise ValueError("authorized_by and note are required to clear the breaker")
    path = _breaker_path(broker)
    if not path.is_file():
        return False
    cleared = path.with_name(f"{path.name}.cleared-{int(datetime.now().timestamp())}")
    os.replace(path, cleared)
    _atomic_write(
        cleared.with_suffix(cleared.suffix + ".approval.json"),
        {"authorized_by": authorized_by, "note": note, "cleared_at": _utc_now()},
    )
    return True


def _rows(payload: object, *keys: str) -> list[dict[str, Any]]:
    value = payload
    if isinstance(value, Mapping):
        if str(value.get("status") or "ok").lower() == "error":
            raise SwapStateError(str(value.get("error") or "broker read failed"))
        for key in keys:
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            if isinstance(value.get("data"), list):
                value = value["data"]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SwapStateError("broker read did not return a row list")
    rows = [dict(item) for item in value if isinstance(item, Mapping)]
    if len(rows) != len(value):
        raise SwapStateError("broker read contains malformed rows")
    return rows


def _account(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SwapStateError("account read did not return an object")
    if str(payload.get("status") or "ok").lower() == "error":
        raise SwapStateError(str(payload.get("error") or "account read failed"))
    for key in ("account", "account_configuration"):
        if isinstance(payload.get(key), Mapping):
            return dict(payload[key])
    return dict(payload)


def _identity(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _position_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("symbol") or row.get("instId") or "").upper(),
        str(row.get("position_side") or row.get("side") or row.get("posSide") or "net").lower(),
        str(row.get("margin_mode") or row.get("mgnMode") or "").lower(),
    )


def _decimal(row: Mapping[str, Any], *keys: str) -> Decimal:
    raw: object = 0
    for key in keys:
        if key in row:
            raw = row[key]
            break
    try:
        value = Decimal(str(raw or 0))
    except InvalidOperation as exc:
        raise SwapStateError("broker returned an invalid numeric value") from exc
    if not value.is_finite():
        raise SwapStateError("broker returned a non-finite numeric value")
    return value


def _known_order_ids(state: Mapping[str, Any]) -> set[str]:
    known: set[str] = set()
    for row in state.get("orders", []):
        if not isinstance(row, Mapping):
            continue
        for key in ("order_id", "client_order_id"):
            value = str(row.get(key) or "")
            if value:
                known.add(value)
    return known


def _equity_usd(balance: Mapping[str, Any]) -> float:
    for key in ("total_equity", "totalEq", "equity", "account_value"):
        if balance.get(key) not in (None, ""):
            value = float(_decimal(balance, key))
            if value > 0:
                return value
    details = balance.get("details")
    if isinstance(details, list):
        total = sum(float(_decimal(row, "equity", "eq")) for row in details if isinstance(row, Mapping))
        if total > 0:
            return total
    raise SwapStateError("positive account equity is unavailable")


def _fill_datetime(row: Mapping[str, Any]) -> datetime | None:
    raw = str(row.get("time") or row.get("ts") or "").strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _risk_metrics(
    prior: Mapping[str, Any] | None,
    balance: Mapping[str, Any],
    fills: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    equity = _equity_usd(balance)
    prior_risk = dict(prior.get("risk") or {}) if prior else {}
    peak = max(equity, float(prior_risk.get("peak_equity_usd") or 0.0))
    daily_pnl = 0.0
    realized: list[tuple[datetime, float]] = []
    for row in fills:
        ts = _fill_datetime(row)
        if ts is None:
            continue
        pnl = float(_decimal(row, "realized_pnl", "fillPnl", "pnl"))
        fee = float(_decimal(row, "fee"))
        net = pnl + fee
        realized.append((ts, net))
        if ts.date() == today:
            daily_pnl += net
    consecutive = 0
    for _, pnl in sorted(realized, key=lambda item: item[0]):
        if pnl < 0:
            consecutive += 1
        elif pnl > 0:
            consecutive = 0
    return {
        "trading_day": today.isoformat(),
        "equity_usd": equity,
        "peak_equity_usd": peak,
        "daily_realized_pnl_usd": daily_pnl,
        "consecutive_losses": consecutive,
    }


def reconcile_swap(
    broker: str,
    *,
    read_account_configuration: ReadPayload,
    read_balance: ReadPayload,
    read_positions: ReadPayload,
    read_open_orders: ReadPayload,
    read_fills: ReadPayload,
    read_algo_orders: ReadPayload,
    environment: str = "demo",
) -> SwapReconcileReport:
    """Diff account, orders, fills, exact hedge positions, and TP/SL state."""
    store = SwapRuntimeStore(broker, environment=environment)
    try:
        prior = store.load()
        account_configuration = _account(read_account_configuration())
        balance = _account(read_balance())
        positions = _rows(read_positions(), "positions")
        open_orders = _rows(read_open_orders(), "open_orders", "orders")
        fills = _rows(read_fills(), "fills", "executions")
        algo_orders = _rows(read_algo_orders(), "algo_orders", "orders")
    except Exception as exc:
        trip_swap_breaker(broker, "swap reconciliation read/state failure", error=str(exc))
        raise

    issues: list[SwapReconcileIssue] = []
    had_prior = prior is not None
    if prior is None:
        for subject, rows in (
            ("position", [row for row in positions if _decimal(row, "quantity", "pos", "qty") != 0]),
            ("order", open_orders),
            ("fill", fills),
            ("algo_order", algo_orders),
        ):
            for row in rows:
                ident = _identity(row, "order_id", "algo_id", "trade_id", "position_id", "symbol", "instId")
                issues.append(
                    SwapReconcileIssue(
                        kind="cold_start_exposure",
                        subject=subject,
                        identity=ident or "<unknown>",
                        detail="broker state exists without a durable local baseline",
                        broker=row,
                    )
                )
    else:
        known = _known_order_ids(prior)
        broker_order_ids = {
            value
            for row in open_orders
            for value in (
                _identity(row, "order_id", "ordId"),
                _identity(row, "client_order_id", "clOrdId"),
            )
            if value
        }
        broker_fill_order_ids = {
            value
            for row in fills
            for value in (
                _identity(row, "order_id", "ordId"),
                _identity(row, "client_order_id", "clOrdId"),
            )
            if value
        }
        for row in prior["orders"]:
            status = str(row.get("status") or "")
            identities = {
                value
                for value in (
                    _identity(row, "order_id"),
                    _identity(row, "client_order_id"),
                )
                if value
            }
            if status in _UNRESOLVED_STATUSES and not identities.intersection(
                broker_order_ids | broker_fill_order_ids
            ):
                issues.append(
                    SwapReconcileIssue(
                        kind="unknown_order",
                        subject="order",
                        identity=next(iter(identities), "<unknown>"),
                        detail="submitted order is absent from both open orders and fills",
                        recorded=row,
                    )
                )
        for row in open_orders:
            ids = {
                _identity(row, "order_id", "ordId"),
                _identity(row, "client_order_id", "clOrdId"),
            } - {""}
            if not ids.intersection(known):
                issues.append(
                    SwapReconcileIssue(
                        kind="unknown_order",
                        subject="order",
                        identity=next(iter(ids), "<unknown>"),
                        detail="broker has an open order absent from durable state",
                        broker=row,
                    )
                )
        prior_fill_ids = {
            _identity(row, "trade_id", "fill_id") for row in prior["fills"]
        } - {""}
        for row in fills:
            fill_id = _identity(row, "trade_id", "fill_id")
            order_id = _identity(row, "order_id", "ordId", "client_order_id", "clOrdId")
            if fill_id not in prior_fill_ids and order_id not in known:
                issues.append(
                    SwapReconcileIssue(
                        kind="unknown_fill",
                        subject="fill",
                        identity=fill_id or order_id or "<unknown>",
                        detail="fill cannot be linked to a durable submitted order",
                        broker=row,
                    )
                )
        prior_positions = {
            _position_key(row): _decimal(row, "quantity", "pos", "qty")
            for row in prior["positions"]
        }
        current_positions = {
            _position_key(row): _decimal(row, "quantity", "pos", "qty")
            for row in positions
        }
        if prior_positions != current_positions:
            new_known_fill = any(
                _identity(row, "order_id", "ordId", "client_order_id", "clOrdId") in known
                and _identity(row, "trade_id", "fill_id") not in prior_fill_ids
                for row in fills
            )
            if not new_known_fill:
                issues.append(
                    SwapReconcileIssue(
                        kind="position_mismatch",
                        subject="position",
                        identity="portfolio",
                        detail="symbol/position-side/margin-mode quantities changed without a known fill",
                        recorded={str(key): str(value) for key, value in prior_positions.items()},
                        broker={str(key): str(value) for key, value in current_positions.items()},
                    )
                )
        expected_algo_ids = {
            str(algo.get("attachAlgoClOrdId") or "")
            for order in prior["orders"]
            for algo in order.get("attached_tp_sl", [])
            if isinstance(algo, Mapping)
        } - {""}
        broker_algo_ids = {
            _identity(row, "client_algo_order_id", "algo_client_order_id", "algoClOrdId", "attachAlgoClOrdId")
            for row in algo_orders
        } - {""}
        missing = expected_algo_ids - broker_algo_ids
        if missing and any(value != 0 for value in current_positions.values()):
            issues.append(
                SwapReconcileIssue(
                    kind="missing_protection",
                    subject="algo_order",
                    identity=",".join(sorted(missing)),
                    detail="an expected TP/SL child order is missing while its position remains open",
                )
            )

    ts = _utc_now()
    if issues:
        trip_swap_breaker(
            broker,
            "swap reconciliation unsafe",
            issues=[{"kind": item.kind, "subject": item.subject, "identity": item.identity} for item in issues],
        )
        return SwapReconcileReport(broker, ts, tuple(issues), False, had_prior)

    state = prior or _empty_state(broker, environment=environment)
    state.update(
        {
            "account_configuration": account_configuration,
            "balance": balance,
            "open_orders": open_orders,
            "fills": fills,
            "positions": positions,
            "algo_orders": algo_orders,
            "risk": _risk_metrics(prior, balance, fills),
        }
    )
    store.save(state)
    return SwapReconcileReport(broker, ts, (), True, had_prior)
