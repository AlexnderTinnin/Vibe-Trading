"""Shared deterministic position sizing for backtest, shadow, and Demo."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.live.mandate.model import Mandate
from src.trading.connectors.okx.swap import SwapInstrument, decimal_string, floor_to_step
from src.trading.quant.costs import PerpetualCostModel
from src.trading.quant.models import TradePlan


@dataclass(frozen=True)
class SizedTrade:
    plan_id: str
    contracts: Decimal
    notional_usdt: Decimal
    margin_usdt: Decimal
    reference_price: Decimal
    planned_liquidation_price: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "plan_id": self.plan_id,
            "contracts": decimal_string(self.contracts),
            "notional_usdt": decimal_string(self.notional_usdt),
            "margin_usdt": decimal_string(self.margin_usdt),
            "reference_price": decimal_string(self.reference_price),
            "planned_liquidation_price": decimal_string(self.planned_liquidation_price),
        }


def size_trade_plan(
    plan: TradePlan,
    instrument: SwapInstrument,
    mandate: Mandate,
    *,
    equity_usdt: float,
    current_total_exposure_usdt: float,
    current_position_exposure_usdt: float,
    current_correlated_exposure_usdt: float,
    margin_used_usdt: float,
    current_portfolio_open_risk_usdt: float = 0.0,
    cost_model: PerpetualCostModel | None = None,
    execution_reference_price: float | None = None,
) -> SizedTrade:
    """Risk-budget sizing followed by every mandate and product cap."""
    plan.validate()
    if not plan.is_risk_increasing:
        raise ValueError("only risk-increasing TradePlans are sized by risk budget")
    if equity_usdt <= 0:
        raise ValueError("positive equity is required for sizing")
    if current_portfolio_open_risk_usdt < 0:
        raise ValueError("current portfolio open risk must not be negative")
    if plan.stop_loss_price is None:
        raise ValueError("opening TradePlan has no stop loss")
    model = cost_model or PerpetualCostModel()
    resolved_price = (
        plan.reference_price
        if execution_reference_price is None
        else float(execution_reference_price)
    )
    if resolved_price <= 0:
        raise ValueError("execution reference price must be positive")
    if plan.position_side == "long":
        if plan.stop_loss_price >= resolved_price:
            raise ValueError("current execution price is outside the long stop/target range")
        if plan.take_profit_price is not None and resolved_price >= plan.take_profit_price:
            raise ValueError("current execution price is outside the long stop/target range")
    if plan.position_side == "short":
        if plan.stop_loss_price <= resolved_price:
            raise ValueError("current execution price is outside the short target/stop range")
        if plan.take_profit_price is not None and resolved_price <= plan.take_profit_price:
            raise ValueError("current execution price is outside the short target/stop range")
    price = Decimal(str(resolved_price))
    leverage = Decimal(str(min(plan.leverage, mandate.hard_caps.max_leverage)))
    direction = plan.position_side
    if direction is None:
        raise ValueError("risk-increasing plan has no position direction")
    liquidation = Decimal(
        str(
            model.estimated_liquidation_price(
                entry_price=resolved_price,
                leverage=float(leverage),
                direction=direction,
            )
        )
    )
    stop = Decimal(str(plan.stop_loss_price))
    if direction == "long" and stop <= liquidation:
        raise ValueError("long stop loss is not above the estimated liquidation price")
    if direction == "short" and stop >= liquidation:
        raise ValueError("short stop loss is not below the estimated liquidation price")
    stop_ratio = abs(Decimal(str(resolved_price - plan.stop_loss_price))) / price
    cost_buffer = Decimal(
        str(2.0 * (model.taker_fee_bps + model.slippage_bps) / 10_000.0)
    )
    risk_ratio = stop_ratio + cost_buffer
    if risk_ratio <= 0:
        raise ValueError("risk distance is not positive")
    risk_budgets = []
    if plan.risk_budget_usdt > 0:
        risk_budgets.append(Decimal(str(plan.risk_budget_usdt)))
    if plan.risk_budget_fraction is not None:
        risk_budgets.append(Decimal(str(equity_usdt * plan.risk_budget_fraction)))
    if not risk_budgets:
        raise ValueError("opening TradePlan has no usable risk budget")
    risk_target = min(risk_budgets) / risk_ratio
    caps = mandate.hard_caps
    capacity_limits = [
        risk_target,
        Decimal(str(caps.max_order_notional_usd)),
        Decimal(str(max(0.0, caps.max_position_notional_usd - current_position_exposure_usdt))),
        Decimal(str(max(0.0, caps.max_total_exposure_usd - current_total_exposure_usdt))),
        Decimal(str(max(0.0, caps.max_correlated_exposure_usd - current_correlated_exposure_usdt))),
        Decimal(
            str(
                max(
                    0.0,
                    (equity_usdt * caps.max_margin_utilization_pct - margin_used_usdt)
                    * float(leverage),
                )
            )
        ),
    ]
    if plan.target_notional_fraction is not None:
        target_notional = equity_usdt * plan.target_notional_fraction
        if plan.is_scale_in:
            target_notional = max(0.0, target_notional - current_position_exposure_usdt)
        capacity_limits.append(
            Decimal(str(target_notional))
        )
    if plan.max_portfolio_notional_fraction is not None:
        dynamic_portfolio_cap = equity_usdt * plan.max_portfolio_notional_fraction
        capacity_limits.extend(
            (
                Decimal(
                    str(max(0.0, dynamic_portfolio_cap - current_total_exposure_usdt))
                ),
                Decimal(
                    str(
                        max(
                            0.0,
                            dynamic_portfolio_cap - current_correlated_exposure_usdt,
                        )
                    )
                ),
            )
        )
    if plan.max_portfolio_risk_fraction is not None:
        remaining_open_risk = max(
            0.0,
            equity_usdt * plan.max_portfolio_risk_fraction
            - current_portfolio_open_risk_usdt,
        )
        capacity_limits.append(Decimal(str(remaining_open_risk)) / risk_ratio)
    capacity = min(capacity_limits)
    one_contract = instrument.contract_value * price
    contracts = floor_to_step(capacity / one_contract, instrument.lot_size)
    if contracts < instrument.minimum_size:
        raise ValueError("risk-sized order is below the instrument minimum")
    maximum = instrument.maximum_market_size
    if maximum is not None:
        contracts = floor_to_step(min(contracts, maximum), instrument.lot_size)
    if contracts < instrument.minimum_size:
        raise ValueError("risk-sized order is below the instrument minimum")
    notional = contracts * one_contract
    margin = notional / leverage
    return SizedTrade(plan.plan_id, contracts, notional, margin, price, liquidation)
