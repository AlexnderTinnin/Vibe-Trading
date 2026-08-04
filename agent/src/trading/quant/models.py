"""Stable, broker-independent contracts for deterministic strategy output."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

SUPPORTED_INSTRUMENTS = frozenset({"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
TRADE_PLAN_SCHEMA_VERSION = 1


class TradePlanError(ValueError):
    """Raised when a strategy emits an ambiguous or unsafe plan."""


class TradeAction(str, Enum):
    HOLD = "hold"
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    ADD_LONG = "add_long"
    ADD_SHORT = "add_short"
    CLOSE = "close"
    UPDATE_STOP = "update_stop"


class ExecutionMode(str, Enum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    DEMO = "demo"


@dataclass(frozen=True)
class PositionView:
    instrument_id: str
    position_side: str
    contracts: float
    entry_price: float
    margin_mode: str = "cross"
    leverage: float = 1.0
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    opened_at: str | None = None
    initial_stop_loss_price: float | None = None
    entry_atr: float | None = None
    scale_count: int = 1

    @property
    def is_open(self) -> bool:
        return self.contracts != 0

    @property
    def direction(self) -> str:
        if self.position_side in ("long", "short"):
            return self.position_side
        return "long" if self.contracts > 0 else "short"


@dataclass(frozen=True)
class TradePlan:
    """Strategy decision with no OKX API parameter names or quantity guesses."""

    schema_version: int
    plan_id: str
    strategy_id: str
    run_id: str
    instrument_id: str
    signal_at: str
    data_as_of: str
    valid_until: str
    action: TradeAction
    confidence: float
    reference_price: float
    stop_loss_price: float | None
    take_profit_price: float | None
    leverage: float
    risk_budget_usdt: float
    reason_codes: tuple[str, ...]
    source_bar_count: int
    risk_budget_fraction: float | None = None
    target_notional_fraction: float | None = None
    max_portfolio_notional_fraction: float | None = None
    max_portfolio_risk_fraction: float | None = None

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        run_id: str,
        instrument_id: str,
        signal_at: datetime,
        data_as_of: datetime,
        action: TradeAction,
        confidence: float,
        reference_price: float,
        stop_loss_price: float | None,
        take_profit_price: float | None,
        leverage: float,
        risk_budget_usdt: float,
        reason_codes: Iterable[str],
        source_bar_count: int,
        risk_budget_fraction: float | None = None,
        target_notional_fraction: float | None = None,
        max_portfolio_notional_fraction: float | None = None,
        max_portfolio_risk_fraction: float | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> "TradePlan":
        signal = _aware(signal_at)
        data_time = _aware(data_as_of)
        material = {
            "strategy_id": strategy_id,
            "run_id": run_id,
            "instrument_id": instrument_id,
            "data_as_of": data_time.isoformat(),
            "action": action.value,
        }
        plan_id = "tp_" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()[:24]
        plan = cls(
            schema_version=TRADE_PLAN_SCHEMA_VERSION,
            plan_id=plan_id,
            strategy_id=strategy_id.strip(),
            run_id=run_id.strip(),
            instrument_id=instrument_id.strip().upper(),
            signal_at=signal.isoformat(),
            data_as_of=data_time.isoformat(),
            valid_until=(signal + ttl).isoformat(),
            action=action,
            confidence=float(confidence),
            reference_price=float(reference_price),
            stop_loss_price=None if stop_loss_price is None else float(stop_loss_price),
            take_profit_price=None if take_profit_price is None else float(take_profit_price),
            leverage=float(leverage),
            risk_budget_usdt=float(risk_budget_usdt),
            reason_codes=tuple(str(value) for value in reason_codes),
            source_bar_count=int(source_bar_count),
            risk_budget_fraction=(
                None if risk_budget_fraction is None else float(risk_budget_fraction)
            ),
            target_notional_fraction=(
                None if target_notional_fraction is None else float(target_notional_fraction)
            ),
            max_portfolio_notional_fraction=(
                None
                if max_portfolio_notional_fraction is None
                else float(max_portfolio_notional_fraction)
            ),
            max_portfolio_risk_fraction=(
                None
                if max_portfolio_risk_fraction is None
                else float(max_portfolio_risk_fraction)
            ),
        )
        plan.validate()
        return plan

    def validate(self) -> None:
        if self.schema_version != TRADE_PLAN_SCHEMA_VERSION:
            raise TradePlanError("unsupported TradePlan schema")
        if self.instrument_id not in SUPPORTED_INSTRUMENTS:
            raise TradePlanError("TradePlan instrument is outside the BTC/ETH perpetual universe")
        if not self.plan_id or not self.strategy_id or not self.run_id:
            raise TradePlanError("TradePlan identifiers must not be blank")
        if not 0.0 <= self.confidence <= 1.0:
            raise TradePlanError("confidence must be between 0 and 1")
        if self.reference_price <= 0 or not 1.0 <= self.leverage <= 10.0:
            raise TradePlanError("reference_price and leverage are outside supported bounds")
        if self.risk_budget_fraction is not None and not 0 < self.risk_budget_fraction <= 1:
            raise TradePlanError("risk_budget_fraction must be in (0, 1]")
        if self.target_notional_fraction is not None and not 0 < self.target_notional_fraction <= 10:
            raise TradePlanError("target_notional_fraction must be in (0, 10]")
        if (
            self.max_portfolio_notional_fraction is not None
            and not 0 < self.max_portfolio_notional_fraction <= 10
        ):
            raise TradePlanError("max_portfolio_notional_fraction must be in (0, 10]")
        if (
            self.max_portfolio_risk_fraction is not None
            and not 0 < self.max_portfolio_risk_fraction <= 1
        ):
            raise TradePlanError("max_portfolio_risk_fraction must be in (0, 1]")
        if self.is_risk_increasing:
            if self.stop_loss_price is None:
                raise TradePlanError("risk-increasing plans require a stop loss")
            if self.risk_budget_usdt <= 0 and self.risk_budget_fraction is None:
                raise TradePlanError("risk-increasing plans require a positive risk budget")
            if self.position_side == "long":
                if self.stop_loss_price >= self.reference_price:
                    raise TradePlanError("long stop loss must be below the reference price")
                if self.take_profit_price is not None and self.take_profit_price <= self.reference_price:
                    raise TradePlanError("long take profit must be above the reference price")
            elif self.stop_loss_price <= self.reference_price:
                raise TradePlanError("short stop loss must be above the reference price")
            elif self.take_profit_price is not None and self.take_profit_price >= self.reference_price:
                raise TradePlanError("short take profit must be below the reference price")
        elif self.action is TradeAction.UPDATE_STOP:
            if self.stop_loss_price is None:
                raise TradePlanError("stop updates require a stop loss price")
            if self.take_profit_price is not None:
                raise TradePlanError("stop updates must not change take profit")
            if any(
                (
                    self.risk_budget_usdt != 0,
                    self.risk_budget_fraction not in (None, 0),
                    self.target_notional_fraction not in (None, 0),
                    self.max_portfolio_notional_fraction not in (None, 0),
                    self.max_portfolio_risk_fraction not in (None, 0),
                )
            ):
                raise TradePlanError("stop updates must have zero risk and notional budgets")
        elif any(
            (
                self.risk_budget_usdt != 0,
                self.risk_budget_fraction not in (None, 0),
                self.target_notional_fraction not in (None, 0),
                self.max_portfolio_notional_fraction not in (None, 0),
                self.max_portfolio_risk_fraction not in (None, 0),
            )
        ):
            raise TradePlanError("risk-neutral plans must have zero risk and notional budgets")
        _parse_time(self.signal_at)
        data_time = _parse_time(self.data_as_of)
        if data_time > _parse_time(self.valid_until):
            raise TradePlanError("TradePlan validity ends before its source data")

    @property
    def is_opening(self) -> bool:
        return self.action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT)

    @property
    def is_scale_in(self) -> bool:
        return self.action in (TradeAction.ADD_LONG, TradeAction.ADD_SHORT)

    @property
    def is_risk_increasing(self) -> bool:
        return self.is_opening or self.is_scale_in

    @property
    def is_stop_update(self) -> bool:
        return self.action is TradeAction.UPDATE_STOP

    @property
    def position_side(self) -> str | None:
        if self.action in (TradeAction.OPEN_LONG, TradeAction.ADD_LONG):
            return "long"
        if self.action in (TradeAction.OPEN_SHORT, TradeAction.ADD_SHORT):
            return "short"
        return None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradePlanError(f"invalid TradePlan timestamp: {value}") from exc
    return _aware(parsed)
