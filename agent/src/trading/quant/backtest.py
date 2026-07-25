"""Bar-by-bar perpetual simulator using the production strategy and sizer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from src.live.mandate.model import Mandate
from src.trading.connectors.okx.swap import SwapInstrument
from src.trading.quant.costs import PerpetualCostModel
from src.trading.quant.models import PositionView, TradeAction, TradePlan
from src.trading.quant.sizing import size_trade_plan
from src.trading.quant.strategy import BtcEthTrendStrategy, _normalize_bars


@dataclass(frozen=True)
class BacktestTrade:
    instrument_id: str
    direction: str
    opened_at: str
    closed_at: str
    entry_price: float
    exit_price: float
    contracts: float
    gross_pnl_usdt: float
    costs_usdt: float
    net_pnl_usdt: float
    exit_reason: str


@dataclass(frozen=True)
class BacktestResult:
    initial_equity_usdt: float
    final_equity_usdt: float
    net_pnl_usdt: float
    return_pct: float
    max_drawdown_pct: float
    trade_count: int
    rejected_plan_count: int
    win_rate: float
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict:
        return {
            "initial_equity_usdt": self.initial_equity_usdt,
            "final_equity_usdt": self.final_equity_usdt,
            "net_pnl_usdt": self.net_pnl_usdt,
            "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trade_count": self.trade_count,
            "rejected_plan_count": self.rejected_plan_count,
            "win_rate": self.win_rate,
            "trades": [trade.__dict__ for trade in self.trades],
            "equity_curve": [list(item) for item in self.equity_curve],
        }


@dataclass
class _OpenPosition:
    instrument_id: str
    direction: str
    opened_at: pd.Timestamp
    entry_price: float
    contracts: float
    stop_loss: float
    take_profit: float
    liquidation_price: float
    leverage: float
    entry_fee_usdt: float


class PerpetualBacktester:
    """Conservative simulator: next-bar entry and worst-case intrabar ordering."""

    def __init__(
        self,
        *,
        strategy: BtcEthTrendStrategy,
        mandate: Mandate,
        instruments: Mapping[str, SwapInstrument],
        initial_equity_usdt: float = 10_000.0,
        cost_model: PerpetualCostModel | None = None,
    ) -> None:
        if initial_equity_usdt <= 0:
            raise ValueError("initial equity must be positive")
        self.strategy = strategy
        self.mandate = mandate
        self.instruments = dict(instruments)
        self.initial_equity = float(initial_equity_usdt)
        self.cost_model = cost_model or PerpetualCostModel()

    def run(self, bars_by_instrument: Mapping[str, pd.DataFrame]) -> BacktestResult:
        frames = {symbol: _normalize_bars(frame) for symbol, frame in bars_by_instrument.items()}
        unknown = set(frames) - set(self.instruments)
        if unknown:
            raise ValueError(f"missing instrument metadata: {', '.join(sorted(unknown))}")
        timeline = sorted(set().union(*(set(frame.index) for frame in frames.values())))
        equity = self.initial_equity
        peak = equity
        max_drawdown = 0.0
        positions: dict[str, _OpenPosition] = {}
        pending: dict[str, TradePlan] = {}
        trades: list[BacktestTrade] = []
        rejected_plans = 0
        curve: list[tuple[str, float]] = []

        for timestamp in timeline:
            for symbol, frame in frames.items():
                if timestamp not in frame.index:
                    continue
                bar = frame.loc[timestamp]
                position = positions.get(symbol)
                if position is not None:
                    exit_reason, exit_reference = self._gap_exit(position, bar)
                    if exit_reason is not None:
                        trade = self._close(position, timestamp, exit_reference, exit_reason)
                        equity += trade.net_pnl_usdt
                        trades.append(trade)
                        del positions[symbol]

                plan = pending.pop(symbol, None)
                if plan is not None:
                    if plan.is_opening and symbol not in positions:
                        try:
                            position, _entry_cost = self._open(
                                plan,
                                timestamp,
                                float(bar["open"]),
                                equity,
                                positions,
                            )
                        except ValueError:
                            rejected_plans += 1
                        else:
                            positions[symbol] = position
                    elif plan.action is TradeAction.CLOSE and symbol in positions:
                        trade = self._close(positions[symbol], timestamp, float(bar["open"]), "strategy_exit")
                        equity += trade.net_pnl_usdt
                        trades.append(trade)
                        del positions[symbol]

                position = positions.get(symbol)
                if position is not None:
                    exit_reason, exit_reference = self._intrabar_exit(position, bar)
                    if exit_reason is not None:
                        trade = self._close(position, timestamp, exit_reference, exit_reason)
                        equity += trade.net_pnl_usdt
                        trades.append(trade)
                        del positions[symbol]

                history = frame.loc[:timestamp]
                try:
                    signal = self.strategy.evaluate(
                        instrument_id=symbol,
                        bars=history,
                        run_id="backtest",
                        now=timestamp.to_pydatetime(),
                        position=_position_view(positions.get(symbol)),
                    )
                except ValueError:
                    continue
                if signal.action is not TradeAction.HOLD:
                    pending[symbol] = signal

            mark_equity = equity + sum(
                self._unrealized(position, frames[symbol].loc[timestamp])
                for symbol, position in positions.items()
                if timestamp in frames[symbol].index
            )
            peak = max(peak, mark_equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - mark_equity) / peak)
            curve.append((timestamp.isoformat(), mark_equity))

        if timeline:
            last_ts = timeline[-1]
            for symbol, position in list(positions.items()):
                frame = frames[symbol]
                row = frame.loc[:last_ts].iloc[-1]
                close_ts = row.name if isinstance(row.name, pd.Timestamp) else last_ts
                trade = self._close(position, close_ts, float(row["close"]), "end_of_test")
                equity += trade.net_pnl_usdt
                trades.append(trade)
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)
            if curve:
                curve[-1] = (last_ts.isoformat(), equity)

        wins = sum(1 for trade in trades if trade.net_pnl_usdt > 0)
        net = equity - self.initial_equity
        return BacktestResult(
            initial_equity_usdt=self.initial_equity,
            final_equity_usdt=equity,
            net_pnl_usdt=net,
            return_pct=net / self.initial_equity,
            max_drawdown_pct=max_drawdown,
            trade_count=len(trades),
            rejected_plan_count=rejected_plans,
            win_rate=wins / len(trades) if trades else 0.0,
            trades=tuple(trades),
            equity_curve=tuple(curve),
        )

    def _open(
        self,
        plan: TradePlan,
        timestamp: pd.Timestamp,
        open_price: float,
        equity: float,
        positions: Mapping[str, _OpenPosition],
    ) -> tuple[_OpenPosition, float]:
        instrument = self.instruments[plan.instrument_id]
        total = sum(
            value.contracts * float(self.instruments[symbol].contract_value) * value.entry_price
            for symbol, value in positions.items()
        )
        side = "buy" if plan.action is TradeAction.OPEN_LONG else "sell"
        entry = self.cost_model.adverse_execution_price(reference_price=open_price, side=side)
        sized = size_trade_plan(
            plan,
            instrument,
            self.mandate,
            equity_usdt=equity,
            current_total_exposure_usdt=total,
            current_position_exposure_usdt=0.0,
            current_correlated_exposure_usdt=total,
            margin_used_usdt=sum(
                value.contracts
                * float(self.instruments[symbol].contract_value)
                * value.entry_price
                / max(value.leverage, 1.0)
                for symbol, value in positions.items()
            ),
            cost_model=self.cost_model,
            execution_reference_price=entry,
        )
        notional = float(sized.contracts * instrument.contract_value) * entry
        entry_cost = self.cost_model.estimate(notional_usdt=notional).fee_usdt
        return (
            _OpenPosition(
                instrument_id=plan.instrument_id,
                direction="long" if plan.action is TradeAction.OPEN_LONG else "short",
                opened_at=timestamp,
                entry_price=entry,
                contracts=float(sized.contracts),
                stop_loss=float(plan.stop_loss_price),
                take_profit=float(plan.take_profit_price),
                liquidation_price=float(sized.planned_liquidation_price),
                leverage=plan.leverage,
                entry_fee_usdt=entry_cost,
            ),
            entry_cost,
        )

    def _gap_exit(self, position: _OpenPosition, bar: pd.Series) -> tuple[str | None, float]:
        open_price = float(bar["open"])
        if position.direction == "long":
            if open_price <= position.liquidation_price:
                return "liquidation", open_price
            if open_price <= position.stop_loss:
                return "stop_loss", open_price
            if open_price >= position.take_profit:
                return "take_profit", position.take_profit
        else:
            if open_price >= position.liquidation_price:
                return "liquidation", open_price
            if open_price >= position.stop_loss:
                return "stop_loss", open_price
            if open_price <= position.take_profit:
                return "take_profit", position.take_profit
        return None, open_price

    def _intrabar_exit(self, position: _OpenPosition, bar: pd.Series) -> tuple[str | None, float]:
        gap_reason, gap_reference = self._gap_exit(position, bar)
        if gap_reason is not None:
            return gap_reason, gap_reference
        high, low = float(bar["high"]), float(bar["low"])
        if position.direction == "long":
            if low <= position.liquidation_price:
                return "liquidation", position.liquidation_price
            if low <= position.stop_loss:
                return "stop_loss", position.stop_loss
            if high >= position.take_profit:
                return "take_profit", position.take_profit
        else:
            if high >= position.liquidation_price:
                return "liquidation", position.liquidation_price
            if high >= position.stop_loss:
                return "stop_loss", position.stop_loss
            if low <= position.take_profit:
                return "take_profit", position.take_profit
        return None, float(bar["close"])

    def _close(
        self,
        position: _OpenPosition,
        timestamp: pd.Timestamp,
        reference_price: float,
        reason: str,
    ) -> BacktestTrade:
        instrument = self.instruments[position.instrument_id]
        side = "sell" if position.direction == "long" else "buy"
        exit_price = self.cost_model.adverse_execution_price(reference_price=reference_price, side=side)
        base_qty = position.contracts * float(instrument.contract_value)
        direction = 1.0 if position.direction == "long" else -1.0
        gross = (exit_price - position.entry_price) * base_qty * direction
        hours = max(0.0, (timestamp - position.opened_at).total_seconds() / 3600.0)
        notional = position.contracts * float(instrument.contract_value) * exit_price
        estimated = self.cost_model.estimate(notional_usdt=notional, holding_hours=hours)
        # Slippage is already embedded in the adverse entry/exit prices.
        costs = position.entry_fee_usdt + estimated.fee_usdt + estimated.funding_usdt
        return BacktestTrade(
            instrument_id=position.instrument_id,
            direction=position.direction,
            opened_at=position.opened_at.isoformat(),
            closed_at=timestamp.isoformat(),
            entry_price=position.entry_price,
            exit_price=exit_price,
            contracts=position.contracts,
            gross_pnl_usdt=gross,
            costs_usdt=costs,
            net_pnl_usdt=gross - costs,
            exit_reason=reason,
        )

    def _unrealized(self, position: _OpenPosition, bar: pd.Series) -> float:
        instrument = self.instruments[position.instrument_id]
        direction = 1.0 if position.direction == "long" else -1.0
        gross = (
            (float(bar["close"]) - position.entry_price)
            * position.contracts
            * float(instrument.contract_value)
            * direction
        )
        timestamp = bar.name if isinstance(bar.name, pd.Timestamp) else position.opened_at
        hours = max(0.0, (timestamp - position.opened_at).total_seconds() / 3600.0)
        notional = position.contracts * float(instrument.contract_value) * float(bar["close"])
        funding = self.cost_model.estimate(
            notional_usdt=notional,
            holding_hours=hours,
        ).funding_usdt
        return gross - position.entry_fee_usdt - funding


def _position_view(position: _OpenPosition | None) -> PositionView | None:
    if position is None:
        return None
    contracts = position.contracts if position.direction == "long" else -position.contracts
    return PositionView(
        instrument_id=position.instrument_id,
        position_side=position.direction,
        contracts=contracts,
        entry_price=position.entry_price,
        leverage=position.leverage,
    )
