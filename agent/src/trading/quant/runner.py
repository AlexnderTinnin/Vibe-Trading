"""24x7 deterministic OKX Demo runner; no LLM or free-text order parsing."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from src.live.halt import halt_flag_set
from src.live.mandate.model import AssetClass, MANDATE_SCHEMA_VERSION, InstrumentType, Mandate
from src.live.mandate.store import load_mandate
from src.live.paths import broker_dir
from src.live.promotion import record_demo_event
from src.live.runtime.liveness import write_heartbeat
from src.live.runtime.scheduler import Job, Scheduler
from src.live.runtime.swap_state import (
    SwapRuntimeStore,
    SwapStateError,
    reconcile_swap,
    swap_breaker_status,
)
from src.live.swap_order_gate import (
    execute_swap_close,
    execute_swap_order,
    execute_swap_stop_update,
)
from src.trading.connectors.okx.swap import SwapInstrument, SwapOrderIntent, floor_to_step
from src.trading.quant.costs import PerpetualCostModel
from src.trading.quant.models import ExecutionMode, PositionView, TradeAction
from src.trading.quant.sizing import size_trade_plan
from src.trading.quant.strategy import RegimeSwitchingStrategy

STATUS_SCHEMA_VERSION = 1
_STATUS_FILENAME = "quant_runner_status.json"
_BAR_PERIOD_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
    "1M": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True)
class CryptoRunnerConfig:
    broker: str = "okx"
    mode: ExecutionMode = ExecutionMode.DEMO
    instruments: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    bar_period: str = "4h"
    bar_limit: int = 300
    daily_bar_period: str = "1d"
    daily_bar_limit: int = 120
    max_data_age_seconds: int = 5 * 60 * 60
    max_quote_age_seconds: int = 30
    tick_interval_seconds: int = 60
    maintenance_pause_before_seconds: int = 15 * 60
    margin_mode: str = "cross"

    def validate(self) -> None:
        if self.mode is ExecutionMode.BACKTEST:
            raise ValueError("use PerpetualBacktester for backtest mode")
        if set(self.instruments) - {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
            raise ValueError("runner supports only BTC/ETH USDT perpetuals")
        if not self.instruments or self.bar_limit < 60 or self.daily_bar_limit < 91:
            raise ValueError("runner instrument set/bar_limit is invalid")
        if self.bar_period not in _BAR_PERIOD_SECONDS or self.daily_bar_period not in _BAR_PERIOD_SECONDS:
            raise ValueError("runner bar period is unsupported")
        if min(
            self.max_data_age_seconds,
            self.max_quote_age_seconds,
            self.tick_interval_seconds,
        ) <= 0:
            raise ValueError("runner timing settings must be positive")
        if self.margin_mode not in ("cross", "isolated"):
            raise ValueError("margin_mode must be cross or isolated")


class CryptoStrategyRunner:
    """One deterministic tick: reconcile, evaluate, size, execute, verify."""

    def __init__(
        self,
        *,
        sdk_module: Any,
        sdk_config: Any,
        strategy: Any | None = None,
        config: CryptoRunnerConfig | None = None,
        mandate_loader: Callable[[str], Mandate | None] = load_mandate,
        clock: Callable[[], datetime] | None = None,
        system_status_reader: Callable[[], object] | None = None,
        store: SwapRuntimeStore | None = None,
        cost_model: PerpetualCostModel | None = None,
    ) -> None:
        self.config = config or CryptoRunnerConfig()
        self.config.validate()
        self.sdk = sdk_module
        self.sdk_config = sdk_config
        if self.config.mode is ExecutionMode.DEMO and not bool(getattr(sdk_config, "is_demo", False)):
            raise ValueError("Demo runner requires an OKX demo SDK config")
        self.strategy = strategy or RegimeSwitchingStrategy()
        self._load_mandate = mandate_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._system_status_reader = system_status_reader or self._read_system_status
        self.store = store or SwapRuntimeStore(self.config.broker, environment="demo")
        self.cost_model = cost_model or PerpetualCostModel()
        self._scheduler: Scheduler | None = None

    def run_once(self) -> dict[str, Any]:
        now = _aware(self._clock())
        write_heartbeat(f"{self.config.broker}-quant", now_ms=int(now.timestamp() * 1000))
        run_id = f"qr_{int(now.timestamp())}"

        blocked = self._safety_precheck(now)
        if blocked is not None:
            return self._finish(run_id, now, "blocked", reason=blocked)
        mandate = self._load_mandate(self.config.broker)
        assert mandate is not None

        try:
            report = reconcile_swap(
                self.config.broker,
                read_account_configuration=lambda: self.sdk.get_account_configuration(self.sdk_config),
                read_balance=lambda: self.sdk.get_account_snapshot(self.sdk_config),
                read_positions=lambda: self.sdk.get_swap_positions(self.sdk_config),
                read_open_orders=lambda: self.sdk.get_swap_open_orders(self.sdk_config),
                read_fills=lambda: self.sdk.get_swap_fills(self.sdk_config),
                read_algo_orders=lambda: self.sdk.get_swap_algo_orders(self.sdk_config),
                environment="demo",
            )
        except Exception as exc:
            return self._finish(run_id, now, "reconcile_error", reason=str(exc))
        if report.requires_halt:
            return self._finish(
                run_id,
                now,
                "reconcile_unsafe",
                reason="broker state differs from durable swap state",
                reconcile=_report_dict(report),
            )

        maintenance_reason = _maintenance_reason(
            self._system_status_reader(),
            now,
            self.config.maintenance_pause_before_seconds,
        )
        if maintenance_reason:
            return self._finish(run_id, now, "maintenance_pause", reason=maintenance_reason)

        state = self.store.load(required=True)
        assert state is not None
        positions = list(state["positions"])
        algo_orders = list(state["algo_orders"])
        account_configuration = dict(state["account_configuration"])
        position_mode = str(account_configuration.get("position_mode") or "")
        if position_mode not in ("net_mode", "long_short_mode"):
            return self._finish(run_id, now, "blocked", reason="unsupported OKX position mode")

        plans: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []
        for symbol in self.config.instruments:
            try:
                bars = self._read_bars(
                    symbol,
                    period=self.config.bar_period,
                    limit=self.config.bar_limit,
                )
                position = _position_view(positions, symbol, algo_orders=algo_orders)
                strategy_inputs: dict[str, Any] = {}
                if bool(getattr(self.strategy, "requires_daily_bars", False)):
                    strategy_inputs["daily_bars"] = self._read_bars(
                        symbol,
                        period=self.config.daily_bar_period,
                        limit=self.config.daily_bar_limit,
                    )
                plan = self.strategy.evaluate(
                    instrument_id=symbol,
                    bars=bars,
                    run_id=run_id,
                    now=now,
                    position=position,
                    **strategy_inputs,
                )
                plans.append(plan.to_dict())
                # OKX candle timestamps identify the bar's opening time. Measure
                # freshness from the close of the confirmed bar, not its open.
                bar_closed_at = _parse_time(plan.data_as_of) + timedelta(
                    seconds=_BAR_PERIOD_SECONDS[self.config.bar_period]
                )
                data_age = (now - bar_closed_at).total_seconds()
                if data_age < -5 or data_age > self.config.max_data_age_seconds:
                    raise ValueError(f"{symbol} confirmed market data is stale")
                if plan.action is TradeAction.HOLD:
                    continue
                if plan.action is TradeAction.UPDATE_STOP:
                    if position is None or position.stop_loss_price is None:
                        raise ValueError(f"{symbol} has no protected position to update")
                    algo_order = _stop_algo_order(algo_orders, symbol)
                    instrument = self._read_instrument(symbol)
                    protective_side = "sell" if position.direction == "long" else "buy"
                    execution_reference = self._read_execution_reference(
                        symbol,
                        side=protective_side,
                        now=now,
                    )
                    stop_loss = _align_trigger_price(
                        plan.stop_loss_price,
                        tick_size=instrument.tick_size,
                        reference_price=Decimal(str(execution_reference)),
                    )
                    if stop_loss is None:
                        raise ValueError("stop update has no stop-loss price")
                    _validate_tighter_stop(
                        direction=position.direction,
                        current=Decimal(str(position.stop_loss_price)),
                        proposed=stop_loss,
                        reference=Decimal(str(execution_reference)),
                    )
                    request_id = _client_order_id(plan.plan_id, "stop")
                    if self.config.mode is ExecutionMode.SHADOW:
                        executions.append(
                            {
                                "status": "shadow",
                                "plan_id": plan.plan_id,
                                "action": plan.action.value,
                                "would_execute": True,
                                "algo_id": str(
                                    algo_order.get("algo_id") or algo_order.get("algoId")
                                ),
                                "old_stop_loss": str(position.stop_loss_price),
                                "new_stop_loss": str(stop_loss),
                            }
                        )
                        continue
                    result = execute_swap_stop_update(
                        broker=self.config.broker,
                        mandate=mandate,
                        sdk_module=self.sdk,
                        config=self.sdk_config,
                        symbol=symbol,
                        position_side=position.direction,
                        algo_order=algo_order,
                        current_stop_loss=position.stop_loss_price,
                        new_stop_loss=stop_loss,
                        reference_price=execution_reference,
                        request_id=request_id,
                        session_id=run_id,
                        store=self.store,
                    )
                    executions.append({"plan_id": plan.plan_id, **result})
                    continue
                if plan.action is TradeAction.CLOSE:
                    if position is None:
                        continue
                    if self.config.mode is ExecutionMode.SHADOW:
                        executions.append(
                            {
                                "status": "shadow",
                                "plan_id": plan.plan_id,
                                "action": plan.action.value,
                                "would_execute": True,
                            }
                        )
                        continue
                    result = execute_swap_close(
                        broker=self.config.broker,
                        mandate=mandate,
                        sdk_module=self.sdk,
                        config=self.sdk_config,
                        symbol=symbol,
                        margin_mode=position.margin_mode,
                        position_side=("net" if position_mode == "net_mode" else position.direction),
                        leverage=position.leverage,
                        client_order_id=_client_order_id(plan.plan_id, "close"),
                        session_id=run_id,
                        store=self.store,
                    )
                    executions.append({"plan_id": plan.plan_id, **result})
                    continue
                if position is not None:
                    raise ValueError(f"{symbol} already has an open position")

                instrument = self._read_instrument(symbol)
                direction = "long" if plan.action is TradeAction.OPEN_LONG else "short"
                side = "buy" if direction == "long" else "sell"
                execution_reference = self._read_execution_reference(symbol, side=side, now=now)
                risk = self.store.derivative_risk_snapshot(
                    position_side=direction
                )
                total_exposure = sum(_position_notional(row) for row in positions)
                same_symbol = sum(
                    _position_notional(row)
                    for row in positions
                    if str(row.get("symbol") or "").upper() == symbol
                )
                sized = size_trade_plan(
                    plan,
                    instrument,
                    mandate,
                    equity_usdt=risk.equity_usd,
                    current_total_exposure_usdt=total_exposure,
                    current_position_exposure_usdt=same_symbol,
                    current_correlated_exposure_usdt=risk.correlated_exposure_usd,
                    margin_used_usdt=risk.margin_used_usd,
                    cost_model=self.cost_model,
                    execution_reference_price=execution_reference,
                )
                stop_loss = _align_trigger_price(
                    plan.stop_loss_price,
                    tick_size=instrument.tick_size,
                    reference_price=sized.reference_price,
                )
                take_profit = _align_trigger_price(
                    plan.take_profit_price,
                    tick_size=instrument.tick_size,
                    reference_price=sized.reference_price,
                )
                position_side = "net" if position_mode == "net_mode" else direction
                swap_intent = SwapOrderIntent.from_values(
                    instrument_id=symbol,
                    side=side,
                    size=str(sized.contracts),
                    size_unit="contracts",
                    order_type="market",
                    margin_mode=self.config.margin_mode,
                    position_side=position_side,
                    leverage=plan.leverage,
                    client_order_id=_client_order_id(plan.plan_id, "open"),
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    trigger_price_type="mark",
                    allowed_instruments=self.config.instruments,
                    max_leverage=Decimal(str(mandate.hard_caps.max_leverage)),
                )
                if self.config.mode is ExecutionMode.SHADOW:
                    executions.append(
                        {
                            "status": "shadow",
                            "plan_id": plan.plan_id,
                            "action": plan.action.value,
                            "would_execute": True,
                            "sizing": sized.to_dict(),
                        }
                    )
                    continue
                result = execute_swap_order(
                    broker=self.config.broker,
                    mandate=mandate,
                    sdk_module=self.sdk,
                    config=self.sdk_config,
                    swap_intent=swap_intent,
                    instrument=instrument,
                    reference_price=str(sized.reference_price),
                    positions=positions,
                    balance=state["balance"],
                    derivative_risk=risk,
                    planned_liquidation_price=str(sized.planned_liquidation_price),
                    session_id=run_id,
                    store=self.store,
                )
                executions.append({"plan_id": plan.plan_id, "sizing": sized.to_dict(), **result})
            except Exception as exc:
                return self._finish(
                    run_id,
                    now,
                    "instrument_error",
                    reason=f"{symbol}: {exc}",
                    plans=plans,
                    executions=executions,
                )

        return self._finish(run_id, now, "completed", plans=plans, executions=executions)

    def _safety_precheck(self, now: datetime) -> str | None:
        if halt_flag_set(self.config.broker) or halt_flag_set(None):
            return "trading halt is active"
        try:
            if swap_breaker_status(self.config.broker).get("tripped") is True:
                return "swap circuit breaker is tripped; manual clear required"
        except SwapStateError as exc:
            return str(exc)
        mandate = self._load_mandate(self.config.broker)
        if mandate is None or mandate.schema_version != MANDATE_SCHEMA_VERSION:
            return "an active mandate schema v2 is required"
        if InstrumentType.PERPETUAL_SWAP not in mandate.hard_caps.allowed_instruments:
            return "mandate does not permit perpetual swaps"
        if AssetClass.CRYPTO not in mandate.universe.asset_classes:
            return "mandate does not permit crypto assets"
        if set(self.config.instruments) - set(mandate.universe.include_symbols):
            return "runner instruments are outside the mandate symbol allowlist"
        try:
            expiry = _parse_time(mandate.consent.expires_at)
        except ValueError:
            return "mandate expiry is invalid"
        if now >= expiry:
            return "mandate has expired"
        if self.config.mode is ExecutionMode.DEMO and not bool(getattr(self.sdk_config, "is_demo", False)):
            return "Demo runner is not bound to an OKX demo profile"
        return None

    def _read_bars(
        self,
        symbol: str,
        *,
        period: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        result = self.sdk.get_historical_bars(
            symbol,
            config=self.sdk_config,
            period=period,
            limit=limit,
        )
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            raise ValueError(str(result.get("error") if isinstance(result, Mapping) else "bar read failed"))
        bars = result.get("bars")
        if not isinstance(bars, list):
            raise ValueError("bar read returned malformed data")
        return bars

    def _read_instrument(self, symbol: str) -> SwapInstrument:
        result = self.sdk.get_swap_instrument(symbol, config=self.sdk_config)
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            raise ValueError(str(result.get("error") if isinstance(result, Mapping) else "instrument read failed"))
        row = result.get("instrument")
        if not isinstance(row, Mapping):
            raise ValueError("instrument read returned malformed metadata")
        return _instrument_from_normalized(row)

    def _read_execution_reference(self, symbol: str, *, side: str, now: datetime) -> float:
        result = self.sdk.get_quote(symbol, config=self.sdk_config)
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            raise ValueError(
                str(result.get("error") if isinstance(result, Mapping) else "quote read failed")
            )
        quote = result.get("quote")
        if not isinstance(quote, Mapping):
            raise ValueError("quote read returned malformed data")
        preferred = "ask" if side == "buy" else "bid"
        raw_price = quote.get(preferred) or quote.get("last")
        try:
            price = float(raw_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("quote has no usable execution price") from exc
        if not (price > 0 and price < float("inf")):
            raise ValueError("quote execution price is invalid")
        raw_time = quote.get("time")
        if raw_time in (None, ""):
            raise ValueError("quote timestamp is missing")
        quote_time = _parse_time_or_epoch(raw_time)
        age = (now - quote_time).total_seconds()
        if age < -5 or age > self.config.max_quote_age_seconds:
            raise ValueError("execution quote is stale or future-dated")
        return price

    def _read_system_status(self) -> object:
        getter = getattr(self.sdk, "get_system_status", None)
        if getter is None:
            return {"status": "error", "error": "OKX system-status reader is unavailable"}
        return getter(config=self.sdk_config)

    def _finish(self, run_id: str, now: datetime, outcome: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "run_id": run_id,
            "broker": self.config.broker,
            "mode": self.config.mode.value,
            "tick_at": now.isoformat(),
            "outcome": outcome,
            **extra,
        }
        if self.config.mode is ExecutionMode.DEMO:
            try:
                record_demo_event(
                    self.config.broker,
                    "tick",
                    {"run_id": run_id, "outcome": outcome, "count": 1},
                    clock=lambda: now,
                )
                if outcome == "reconcile_unsafe":
                    record_demo_event(
                        self.config.broker,
                        "reconcile_unsafe",
                        {"run_id": run_id},
                        clock=lambda: now,
                    )
                for execution in extra.get("executions", []):
                    if not isinstance(execution, Mapping):
                        continue
                    safety_event = str(execution.get("safety_event") or "")
                    if safety_event:
                        record_demo_event(
                            self.config.broker,
                            safety_event,
                            {"run_id": run_id, "plan_id": execution.get("plan_id")},
                            clock=lambda: now,
                        )
                    if execution.get("write_state") == "unknown_needs_reconcile":
                        record_demo_event(
                            self.config.broker,
                            "unknown_order",
                            {"run_id": run_id, "plan_id": execution.get("plan_id")},
                            clock=lambda: now,
                        )
                    if execution.get("status") == "blocked" and execution.get("breach"):
                        record_demo_event(
                            self.config.broker,
                            "risk_limit_breach",
                            {"run_id": run_id, "breach": execution.get("breach")},
                            clock=lambda: now,
                        )
            except Exception as exc:  # qualification evidence fails closed later
                payload["qualification_record_error"] = str(exc)
        _write_status(self.config.broker, payload)
        return payload

    def start(self, scheduler: Scheduler | None = None) -> Scheduler:
        """Schedule the same tick 24x7; maintenance/freshness gate each wake."""
        if scheduler is None:
            async def _on_fire(_job: Job) -> None:
                await asyncio.to_thread(self.run_once)

            scheduler = Scheduler(_on_fire)
        now_ms = int(_aware(self._clock()).timestamp() * 1000)
        scheduler.add_job(
            Job(
                id=f"{self.config.broker}-crypto-24x7",
                next_run_at=now_ms,
                schedule=f"interval:{self.config.tick_interval_seconds * 1000}",
                payload={"market": "crypto", "mode": self.config.mode.value},
            )
        )
        scheduler.start()
        self._scheduler = scheduler
        return scheduler

    async def stop(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
            self._scheduler = None


def quant_status(broker: str = "okx") -> dict[str, Any]:
    path = broker_dir(broker) / _STATUS_FILENAME
    if not path.is_file():
        return {"broker": broker, "outcome": "not_started"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("quant runner status is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != STATUS_SCHEMA_VERSION:
        raise RuntimeError("quant runner status schema is invalid")
    return payload


def _write_status(broker: str, payload: Mapping[str, Any]) -> None:
    path = broker_dir(broker) / _STATUS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _maintenance_reason(payload: object, now: datetime, pause_before_seconds: int) -> str | None:
    if not isinstance(payload, Mapping) or str(payload.get("status") or "").lower() != "ok":
        return "OKX system status is unavailable (fail-closed)"
    incidents = payload.get("incidents", payload.get("data", []))
    if not isinstance(incidents, list):
        return "OKX system status payload is malformed"
    horizon = now + timedelta(seconds=pause_before_seconds)
    for item in incidents:
        if not isinstance(item, Mapping):
            return "OKX system status contains a malformed incident"
        state = str(item.get("state") or item.get("status") or "").lower()
        begin_raw = item.get("begin") or item.get("begin_at") or item.get("beginTs")
        if state in ("ongoing", "in_progress", "maintenance"):
            return str(item.get("title") or "OKX maintenance is ongoing")
        if begin_raw not in (None, ""):
            try:
                begin = _parse_time_or_epoch(begin_raw)
            except ValueError:
                return "OKX maintenance time is malformed"
            if now <= begin <= horizon and state not in ("completed", "resolved"):
                return str(item.get("title") or "OKX maintenance is imminent")
    return None


def _position_view(
    rows: Sequence[Mapping[str, Any]],
    symbol: str,
    *,
    algo_orders: Sequence[Mapping[str, Any]] = (),
) -> PositionView | None:
    stop_loss = _unique_protection_price(algo_orders, symbol, "stop_loss_trigger")
    take_profit = _unique_protection_price(algo_orders, symbol, "take_profit_trigger")
    matches: list[PositionView] = []
    for row in rows:
        if str(row.get("symbol") or row.get("instId") or "").upper() != symbol:
            continue
        qty = float(row.get("quantity", row.get("pos", row.get("qty", 0))) or 0)
        if qty == 0:
            continue
        side = str(row.get("position_side") or row.get("side") or row.get("posSide") or "net").lower()
        matches.append(
            PositionView(
                instrument_id=symbol,
                position_side=side,
                contracts=qty,
                entry_price=float(row.get("average_cost", row.get("avgPx", 0)) or 0),
                margin_mode=str(row.get("margin_mode") or row.get("mgnMode") or "cross").lower(),
                leverage=float(row.get("leverage", row.get("lever", 1)) or 1),
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
        )
    if len(matches) > 1:
        raise ValueError(f"{symbol} has multiple non-zero hedge positions; strategy requires one side")
    return matches[0] if matches else None


def _unique_protection_price(
    rows: Sequence[Mapping[str, Any]],
    symbol: str,
    field: str,
) -> float | None:
    values = {
        float(row[field])
        for row in rows
        if str(row.get("symbol") or row.get("instId") or "").upper() == symbol
        and row.get(field) not in (None, "")
    }
    if len(values) > 1:
        raise ValueError(f"{symbol} has ambiguous {field} protection")
    return next(iter(values), None)


def _stop_algo_order(
    rows: Sequence[Mapping[str, Any]],
    symbol: str,
) -> Mapping[str, Any]:
    matches = [
        row
        for row in rows
        if str(row.get("symbol") or row.get("instId") or "").upper() == symbol
        and row.get("stop_loss_trigger", row.get("slTriggerPx")) not in (None, "")
    ]
    if len(matches) != 1:
        raise ValueError(f"{symbol} requires exactly one pending stop-loss algo order")
    if not str(matches[0].get("algo_id") or matches[0].get("algoId") or "").strip():
        raise ValueError(f"{symbol} stop-loss algo order id is unavailable")
    return matches[0]


def _validate_tighter_stop(
    *,
    direction: str,
    current: Decimal,
    proposed: Decimal,
    reference: Decimal,
) -> None:
    if direction == "long":
        if proposed <= current:
            raise ValueError("long stop update does not tighten the existing stop")
        if proposed >= reference:
            raise ValueError("long stop update is crossed by the execution quote")
        return
    if direction == "short":
        if proposed >= current:
            raise ValueError("short stop update does not tighten the existing stop")
        if proposed <= reference:
            raise ValueError("short stop update is crossed by the execution quote")
        return
    raise ValueError("position direction must resolve to long or short")


def _align_trigger_price(
    value: float | None,
    *,
    tick_size: Decimal,
    reference_price: Decimal,
) -> Decimal | None:
    """Align a trigger toward entry without crossing the execution reference."""
    if value is None:
        return None
    raw = Decimal(str(value))
    lower = floor_to_step(raw, tick_size)
    upper = lower if lower == raw else lower + tick_size
    if raw < reference_price:
        return upper if upper < reference_price else lower
    if raw > reference_price:
        return lower if lower > reference_price else upper
    return raw


def _position_notional(row: Mapping[str, Any]) -> float:
    value = row.get("notional_usd", row.get("notionalUsd", row.get("notional")))
    if value in (None, ""):
        raise ValueError("position notional is unavailable (fail-closed)")
    parsed = abs(float(value))
    if parsed != parsed:
        raise ValueError("position notional is invalid")
    return parsed


def _instrument_from_normalized(row: Mapping[str, Any]) -> SwapInstrument:
    return SwapInstrument(
        instrument_id=str(row["instrument_id"]),
        instrument_type=str(row["instrument_type"]),
        state=str(row["state"]),
        base_currency=str(row["base_currency"]),
        quote_currency=str(row["quote_currency"]),
        settle_currency=str(row["settle_currency"]),
        contract_type=str(row["contract_type"]),
        contract_value=Decimal(str(row["contract_value"])),
        contract_value_currency=str(row["contract_value_currency"]),
        lot_size=Decimal(str(row["lot_size"])),
        minimum_size=Decimal(str(row["minimum_size"])),
        tick_size=Decimal(str(row["tick_size"])),
        maximum_market_size=(
            None if row.get("maximum_market_size") in (None, "") else Decimal(str(row["maximum_market_size"]))
        ),
        maximum_limit_size=(
            None if row.get("maximum_limit_size") in (None, "") else Decimal(str(row["maximum_limit_size"]))
        ),
    )


def _client_order_id(plan_id: str, action: str) -> str:
    clean = "".join(char for char in plan_id if char.isalnum() or char in "_-")
    return f"{clean[:24]}-{action[:5]}"[:32]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware(parsed)


def _parse_time_or_epoch(value: object) -> datetime:
    raw = str(value).strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
    return _parse_time(raw)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _report_dict(report: Any) -> dict[str, Any]:
    issues = getattr(report, "issues", getattr(report, "deltas", ()))
    return {
        "broker": report.broker,
        "ts": report.ts,
        "issues": [
            {
                "kind": item.kind,
                "subject": item.subject,
                "identity": item.identity,
                "detail": item.detail,
            }
            for item in issues
        ],
        "state_persisted": report.state_persisted,
    }
