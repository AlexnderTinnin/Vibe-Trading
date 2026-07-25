"""Perpetual execution, funding, margin, and liquidation cost model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionCosts:
    notional_usdt: float
    fee_usdt: float
    slippage_usdt: float
    funding_usdt: float
    total_usdt: float


@dataclass(frozen=True)
class PerpetualCostModel:
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    slippage_bps: float = 3.0
    funding_rate_per_8h: float = 0.0001
    maintenance_margin_ratio: float = 0.005

    def estimate(
        self,
        *,
        notional_usdt: float,
        maker: bool = False,
        holding_hours: float = 0.0,
        funding_rate_per_8h: float | None = None,
    ) -> ExecutionCosts:
        if notional_usdt <= 0 or holding_hours < 0:
            raise ValueError("notional must be positive and holding_hours non-negative")
        fee_bps = self.maker_fee_bps if maker else self.taker_fee_bps
        fee = notional_usdt * fee_bps / 10_000.0
        slippage = notional_usdt * self.slippage_bps / 10_000.0
        rate = self.funding_rate_per_8h if funding_rate_per_8h is None else funding_rate_per_8h
        funding = notional_usdt * rate * holding_hours / 8.0
        return ExecutionCosts(notional_usdt, fee, slippage, funding, fee + slippage + funding)

    def adverse_execution_price(self, *, reference_price: float, side: str) -> float:
        if reference_price <= 0 or side not in ("buy", "sell"):
            raise ValueError("reference price/side is invalid")
        ratio = self.slippage_bps / 10_000.0
        return reference_price * (1.0 + ratio if side == "buy" else 1.0 - ratio)

    def estimated_liquidation_price(
        self,
        *,
        entry_price: float,
        leverage: float,
        direction: str,
    ) -> float:
        if entry_price <= 0 or leverage < 1 or direction not in ("long", "short"):
            raise ValueError("liquidation inputs are invalid")
        initial_margin = 1.0 / leverage
        if direction == "long":
            return max(0.0, entry_price * (1.0 - initial_margin + self.maintenance_margin_ratio))
        return entry_price * (1.0 + initial_margin - self.maintenance_margin_ratio)
