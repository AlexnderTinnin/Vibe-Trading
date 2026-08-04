"""Bar-by-bar perpetual simulator using the production strategy and sizer."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from src.live.mandate.model import Mandate
from src.trading.connectors.okx.swap import SwapInstrument
from src.trading.quant.costs import PerpetualCostModel
from src.trading.quant.models import PositionView, TradeAction, TradePlan
from src.trading.quant.sizing import size_trade_plan
from src.trading.quant.strategy import _bar_interval, _daily_from_primary, _normalize_bars


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
    fees_usdt: float
    funding_usdt: float
    estimated_slippage_usdt: float
    costs_usdt: float
    net_pnl_usdt: float
    exit_reason: str
    entry_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BacktestResult:
    initial_equity_usdt: float
    final_equity_usdt: float
    net_pnl_usdt: float
    return_pct: float
    max_drawdown_pct: float
    cagr_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    profit_factor: float
    total_costs_usdt: float
    total_fees_usdt: float
    total_funding_usdt: float
    estimated_slippage_usdt: float
    total_economic_costs_usdt: float
    average_holding_hours: float
    trade_count: int
    scale_in_count: int
    rejected_plan_count: int
    win_rate: float
    max_gross_exposure_fraction: float
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[tuple[str, float], ...]
    exit_reasons: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict:
        return {
            "initial_equity_usdt": self.initial_equity_usdt,
            "final_equity_usdt": self.final_equity_usdt,
            "net_pnl_usdt": self.net_pnl_usdt,
            "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "cagr_pct": self.cagr_pct,
            "annualized_volatility_pct": self.annualized_volatility_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "profit_factor": self.profit_factor,
            "total_costs_usdt": self.total_costs_usdt,
            "total_fees_usdt": self.total_fees_usdt,
            "total_funding_usdt": self.total_funding_usdt,
            "estimated_slippage_usdt": self.estimated_slippage_usdt,
            "total_economic_costs_usdt": self.total_economic_costs_usdt,
            "average_holding_hours": self.average_holding_hours,
            "trade_count": self.trade_count,
            "scale_in_count": self.scale_in_count,
            "rejected_plan_count": self.rejected_plan_count,
            "win_rate": self.win_rate,
            "max_gross_exposure_fraction": self.max_gross_exposure_fraction,
            "trades": [trade.__dict__ for trade in self.trades],
            "equity_curve": [list(item) for item in self.equity_curve],
            "exit_reasons": dict(self.exit_reasons),
        }


@dataclass
class _PositionLot:
    opened_at: pd.Timestamp
    entry_price: float
    contracts: float
    entry_fee_usdt: float
    entry_reason_codes: tuple[str, ...]


@dataclass
class _OpenPosition:
    instrument_id: str
    direction: str
    lots: list[_PositionLot]
    stop_loss: float
    take_profit: float | None
    liquidation_price: float
    leverage: float
    initial_stop_loss: float
    scale_count: int = 1

    @property
    def opened_at(self) -> pd.Timestamp:
        return min(lot.opened_at for lot in self.lots)

    @property
    def contracts(self) -> float:
        return sum(lot.contracts for lot in self.lots)

    @property
    def entry_price(self) -> float:
        contracts = self.contracts
        if contracts <= 0:
            return 0.0
        return sum(lot.entry_price * lot.contracts for lot in self.lots) / contracts

    @property
    def entry_fee_usdt(self) -> float:
        return sum(lot.entry_fee_usdt for lot in self.lots)

    @property
    def entry_reason_codes(self) -> tuple[str, ...]:
        return tuple(
            reason
            for lot in self.lots
            for reason in lot.entry_reason_codes
        )


class PerpetualBacktester:
    """Conservative simulator: next-bar entry and worst-case intrabar ordering."""

    def __init__(
        self,
        *,
        strategy: Any,
        mandate: Mandate,
        instruments: Mapping[str, SwapInstrument],
        initial_equity_usdt: float = 10_000.0,
        cost_model: PerpetualCostModel | None = None,
        funding_rates_by_instrument: Mapping[
            str, pd.Series | pd.DataFrame
        ]
        | None = None,
    ) -> None:
        if initial_equity_usdt <= 0:
            raise ValueError("initial equity must be positive")
        self.strategy = strategy
        self.mandate = mandate
        self.instruments = dict(instruments)
        self.initial_equity = float(initial_equity_usdt)
        self.cost_model = cost_model or PerpetualCostModel()
        self._use_actual_funding = funding_rates_by_instrument is not None
        raw_funding = dict(funding_rates_by_instrument or {})
        unknown_funding = set(raw_funding) - set(self.instruments)
        if unknown_funding:
            raise ValueError(
                f"funding rates have no instrument metadata: {', '.join(sorted(unknown_funding))}"
            )
        self.funding_rates = {
            symbol: _normalize_funding_rates(values)
            for symbol, values in raw_funding.items()
        }

    def run(
        self,
        bars_by_instrument: Mapping[str, pd.DataFrame],
        *,
        start_at: str | pd.Timestamp | None = None,
        end_at: str | pd.Timestamp | None = None,
    ) -> BacktestResult:
        frames = {symbol: _normalize_bars(frame) for symbol, frame in bars_by_instrument.items()}
        for frame in frames.values():
            frame.attrs["_quant_backtest_token"] = object()
        prepare = getattr(self.strategy, "prepare_backtest", None)
        if callable(prepare):
            prepare(frames)
        unknown = set(frames) - set(self.instruments)
        if unknown:
            raise ValueError(f"missing instrument metadata: {', '.join(sorted(unknown))}")
        start = _utc_timestamp(start_at) if start_at is not None else None
        end = _utc_timestamp(end_at) if end_at is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("backtest start_at must not be after end_at")
        timeline = sorted(
            timestamp
            for timestamp in set().union(*(set(frame.index) for frame in frames.values()))
            if (start is None or timestamp >= start) and (end is None or timestamp <= end)
        )
        bar_intervals = {symbol: _bar_interval(frame) for symbol, frame in frames.items()}
        daily_frames = (
            {
                symbol: _daily_from_primary(frame, bar_intervals[symbol])
                for symbol, frame in frames.items()
            }
            if bool(getattr(self.strategy, "requires_daily_bars", False))
            else {}
        )
        equity = self.initial_equity
        peak = equity
        max_drawdown = 0.0
        positions: dict[str, _OpenPosition] = {}
        pending: dict[str, TradePlan] = {}
        trades: list[BacktestTrade] = []
        scale_ins = 0
        rejected_plans = 0
        max_gross_exposure_fraction = 0.0
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
                    elif plan.is_scale_in and symbol in positions:
                        try:
                            self._increase(
                                positions[symbol],
                                plan,
                                timestamp,
                                float(bar["open"]),
                                equity,
                                positions,
                            )
                        except ValueError:
                            rejected_plans += 1
                        else:
                            scale_ins += 1
                    elif plan.is_scale_in:
                        rejected_plans += 1
                    elif plan.action is TradeAction.CLOSE and symbol in positions:
                        exit_reason = (
                            plan.reason_codes[0] if plan.reason_codes else "strategy_exit"
                        )
                        trade = self._close(
                            positions[symbol], timestamp, float(bar["open"]), exit_reason
                        )
                        equity += trade.net_pnl_usdt
                        trades.append(trade)
                        del positions[symbol]
                    elif plan.action is TradeAction.UPDATE_STOP and symbol in positions:
                        position = positions[symbol]
                        proposed = plan.stop_loss_price
                        if proposed is None or not self._is_tighter_stop(position, proposed):
                            rejected_plans += 1
                        elif self._stop_crossed_at_open(position, proposed, float(bar["open"])):
                            trade = self._close(
                                position,
                                timestamp,
                                float(bar["open"]),
                                "trailing_stop_gap",
                            )
                            equity += trade.net_pnl_usdt
                            trades.append(trade)
                            del positions[symbol]
                        else:
                            position.stop_loss = float(proposed)

                position = positions.get(symbol)
                if position is not None:
                    exit_reason, exit_reference = self._intrabar_exit(position, bar)
                    if exit_reason is not None:
                        trade = self._close(position, timestamp, exit_reference, exit_reason)
                        equity += trade.net_pnl_usdt
                        trades.append(trade)
                        del positions[symbol]

                history = frame.loc[:timestamp]
                history_limit = getattr(self.strategy, "history_limit", None)
                if history_limit is not None:
                    history = history.iloc[-int(history_limit) :]
                strategy_inputs: dict[str, Any] = {}
                if symbol in daily_frames:
                    strategy_inputs["daily_bars"] = daily_frames[symbol].loc[:timestamp]
                if bool(getattr(self.strategy, "requires_funding_rate", False)):
                    strategy_inputs["funding_rate_8h"] = self._recent_funding_rate(
                        symbol,
                        timestamp,
                    )
                try:
                    signal = self.strategy.evaluate(
                        instrument_id=symbol,
                        bars=history,
                        run_id="backtest",
                        now=(timestamp + bar_intervals[symbol]).to_pydatetime(),
                        position=_position_view(positions.get(symbol)),
                        **strategy_inputs,
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
            gross_exposure = sum(
                position.contracts
                * float(self.instruments[symbol].contract_value)
                * float(frames[symbol].loc[timestamp]["close"])
                for symbol, position in positions.items()
                if timestamp in frames[symbol].index
            )
            if mark_equity > 0:
                max_gross_exposure_fraction = max(
                    max_gross_exposure_fraction,
                    gross_exposure / mark_equity,
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
        curve_series = pd.Series(
            [value for _, value in curve],
            index=pd.DatetimeIndex([timestamp for timestamp, _ in curve]),
            dtype=float,
        )
        daily_returns = curve_series.resample("1D").last().dropna().pct_change().dropna()
        annualized_volatility = (
            float(daily_returns.std(ddof=1) * math.sqrt(365.0))
            if len(daily_returns) > 1
            else 0.0
        )
        sharpe = (
            float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(365.0))
            if len(daily_returns) > 1 and float(daily_returns.std(ddof=1)) > 0
            else 0.0
        )
        elapsed_years = (
            (timeline[-1] - timeline[0]).total_seconds() / (365.0 * 86_400.0)
            if len(timeline) > 1
            else 0.0
        )
        cagr = (
            (equity / self.initial_equity) ** (1.0 / elapsed_years) - 1.0
            if elapsed_years > 0 and equity > 0
            else 0.0
        )
        positive_pnl = sum(trade.net_pnl_usdt for trade in trades if trade.net_pnl_usdt > 0)
        negative_pnl = -sum(trade.net_pnl_usdt for trade in trades if trade.net_pnl_usdt < 0)
        profit_factor = positive_pnl / negative_pnl if negative_pnl > 0 else (math.inf if positive_pnl > 0 else 0.0)
        holding_hours = [
            max(
                0.0,
                (
                    pd.Timestamp(trade.closed_at) - pd.Timestamp(trade.opened_at)
                ).total_seconds()
                / 3600.0,
            )
            for trade in trades
        ]
        total_fees = sum(trade.fees_usdt for trade in trades)
        total_funding = sum(trade.funding_usdt for trade in trades)
        estimated_slippage = sum(trade.estimated_slippage_usdt for trade in trades)
        return BacktestResult(
            initial_equity_usdt=self.initial_equity,
            final_equity_usdt=equity,
            net_pnl_usdt=net,
            return_pct=net / self.initial_equity,
            max_drawdown_pct=max_drawdown,
            cagr_pct=cagr,
            annualized_volatility_pct=annualized_volatility,
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
            total_costs_usdt=total_fees + total_funding,
            total_fees_usdt=total_fees,
            total_funding_usdt=total_funding,
            estimated_slippage_usdt=estimated_slippage,
            total_economic_costs_usdt=total_fees + total_funding + estimated_slippage,
            average_holding_hours=(sum(holding_hours) / len(holding_hours) if holding_hours else 0.0),
            trade_count=len(trades),
            scale_in_count=scale_ins,
            rejected_plan_count=rejected_plans,
            win_rate=wins / len(trades) if trades else 0.0,
            max_gross_exposure_fraction=max_gross_exposure_fraction,
            trades=tuple(trades),
            equity_curve=tuple(curve),
            exit_reasons=tuple(sorted(Counter(trade.exit_reason for trade in trades).items())),
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
        direction = plan.position_side
        if direction is None:
            raise ValueError("opening plan has no direction")
        total = self._total_exposure(positions)
        side = "buy" if direction == "long" else "sell"
        entry = self.cost_model.adverse_execution_price(reference_price=open_price, side=side)
        sized = size_trade_plan(
            plan,
            instrument,
            self.mandate,
            equity_usdt=equity,
            current_total_exposure_usdt=total,
            current_position_exposure_usdt=0.0,
            current_correlated_exposure_usdt=total,
            margin_used_usdt=self._margin_used(positions),
            current_portfolio_open_risk_usdt=self._portfolio_open_risk(positions),
            cost_model=self.cost_model,
            execution_reference_price=entry,
        )
        notional = float(sized.contracts * instrument.contract_value) * entry
        entry_cost = self.cost_model.estimate(notional_usdt=notional).fee_usdt
        return (
            _OpenPosition(
                instrument_id=plan.instrument_id,
                direction=direction,
                lots=[
                    _PositionLot(
                        opened_at=timestamp,
                        entry_price=entry,
                        contracts=float(sized.contracts),
                        entry_fee_usdt=entry_cost,
                        entry_reason_codes=plan.reason_codes,
                    )
                ],
                stop_loss=float(plan.stop_loss_price),
                take_profit=(
                    None if plan.take_profit_price is None else float(plan.take_profit_price)
                ),
                liquidation_price=float(sized.planned_liquidation_price),
                leverage=plan.leverage,
                initial_stop_loss=float(plan.stop_loss_price),
            ),
            entry_cost,
        )

    def _increase(
        self,
        position: _OpenPosition,
        plan: TradePlan,
        timestamp: pd.Timestamp,
        open_price: float,
        equity: float,
        positions: Mapping[str, _OpenPosition],
    ) -> float:
        direction = plan.position_side
        if direction is None or direction != position.direction:
            raise ValueError("scale-in direction does not match the open position")
        if plan.stop_loss_price is None:
            raise ValueError("scale-in plan has no aggregate stop loss")
        proposed_stop = float(plan.stop_loss_price)
        if direction == "long" and proposed_stop < position.stop_loss:
            raise ValueError("long scale-in must not loosen the aggregate stop")
        if direction == "short" and proposed_stop > position.stop_loss:
            raise ValueError("short scale-in must not loosen the aggregate stop")

        instrument = self.instruments[plan.instrument_id]
        total = self._total_exposure(positions)
        current_position_exposure = (
            position.contracts * float(instrument.contract_value) * position.entry_price
        )
        side = "buy" if direction == "long" else "sell"
        entry = self.cost_model.adverse_execution_price(reference_price=open_price, side=side)
        protected_portfolio_risk = sum(
            self._position_open_risk(
                item,
                stop_override=(proposed_stop if symbol == plan.instrument_id else None),
            )
            for symbol, item in positions.items()
        )
        sized = size_trade_plan(
            plan,
            instrument,
            self.mandate,
            equity_usdt=equity,
            current_total_exposure_usdt=total,
            current_position_exposure_usdt=current_position_exposure,
            current_correlated_exposure_usdt=total,
            margin_used_usdt=self._margin_used(positions),
            current_portfolio_open_risk_usdt=protected_portfolio_risk,
            cost_model=self.cost_model,
            execution_reference_price=entry,
        )
        added_contracts = float(sized.contracts)
        combined_contracts = position.contracts + added_contracts
        combined_entry = (
            position.entry_price * position.contracts + entry * added_contracts
        ) / combined_contracts
        combined_liquidation = self.cost_model.estimated_liquidation_price(
            entry_price=combined_entry,
            leverage=plan.leverage,
            direction=direction,
        )
        if direction == "long" and proposed_stop <= combined_liquidation:
            raise ValueError("long aggregate stop is below the post-scale liquidation price")
        if direction == "short" and proposed_stop >= combined_liquidation:
            raise ValueError("short aggregate stop is above the post-scale liquidation price")
        notional = added_contracts * float(instrument.contract_value) * entry
        entry_cost = self.cost_model.estimate(notional_usdt=notional).fee_usdt
        position.lots.append(
            _PositionLot(
                opened_at=timestamp,
                entry_price=entry,
                contracts=added_contracts,
                entry_fee_usdt=entry_cost,
                entry_reason_codes=plan.reason_codes,
            )
        )
        position.stop_loss = proposed_stop
        position.liquidation_price = combined_liquidation
        position.leverage = plan.leverage
        position.scale_count += 1
        return entry_cost

    def _total_exposure(self, positions: Mapping[str, _OpenPosition]) -> float:
        return sum(
            position.contracts
            * float(self.instruments[symbol].contract_value)
            * position.entry_price
            for symbol, position in positions.items()
        )

    def _margin_used(self, positions: Mapping[str, _OpenPosition]) -> float:
        return sum(
            position.contracts
            * float(self.instruments[symbol].contract_value)
            * position.entry_price
            / max(position.leverage, 1.0)
            for symbol, position in positions.items()
        )

    def _portfolio_open_risk(
        self,
        positions: Mapping[str, _OpenPosition],
    ) -> float:
        return sum(self._position_open_risk(position) for position in positions.values())

    def _position_open_risk(
        self,
        position: _OpenPosition,
        *,
        stop_override: float | None = None,
    ) -> float:
        stop = position.stop_loss if stop_override is None else stop_override
        distance = (
            max(0.0, position.entry_price - stop)
            if position.direction == "long"
            else max(0.0, stop - position.entry_price)
        )
        instrument = self.instruments[position.instrument_id]
        return position.contracts * float(instrument.contract_value) * distance

    def _gap_exit(self, position: _OpenPosition, bar: pd.Series) -> tuple[str | None, float]:
        open_price = float(bar["open"])
        if position.direction == "long":
            if open_price <= position.liquidation_price:
                return "liquidation", open_price
            if open_price <= position.stop_loss:
                return "stop_loss", open_price
            if position.take_profit is not None and open_price >= position.take_profit:
                return "take_profit", position.take_profit
        else:
            if open_price >= position.liquidation_price:
                return "liquidation", open_price
            if open_price >= position.stop_loss:
                return "stop_loss", open_price
            if position.take_profit is not None and open_price <= position.take_profit:
                return "take_profit", position.take_profit
        return None, open_price

    @staticmethod
    def _is_tighter_stop(position: _OpenPosition, proposed: float) -> bool:
        if position.direction == "long":
            return position.stop_loss < proposed
        return proposed < position.stop_loss

    @staticmethod
    def _stop_crossed_at_open(
        position: _OpenPosition,
        proposed: float,
        open_price: float,
    ) -> bool:
        if position.direction == "long":
            return open_price <= proposed
        return open_price >= proposed

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
            if position.take_profit is not None and high >= position.take_profit:
                return "take_profit", position.take_profit
        else:
            if high >= position.liquidation_price:
                return "liquidation", position.liquidation_price
            if high >= position.stop_loss:
                return "stop_loss", position.stop_loss
            if position.take_profit is not None and low <= position.take_profit:
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
        direction = 1.0 if position.direction == "long" else -1.0
        gross = sum(
            (exit_price - lot.entry_price)
            * lot.contracts
            * float(instrument.contract_value)
            * direction
            for lot in position.lots
        )
        notional = position.contracts * float(instrument.contract_value) * exit_price
        funding = self._funding_cost(
            position,
            timestamp,
            mark_price=reference_price,
        )
        # Slippage is already embedded in the adverse entry/exit prices.
        exit_fee = self.cost_model.estimate(notional_usdt=notional).fee_usdt
        fees = position.entry_fee_usdt + exit_fee
        costs = fees + funding
        slippage_ratio = self.cost_model.slippage_bps / 10_000.0
        entry_slippage = sum(
            lot.contracts
            * float(instrument.contract_value)
            * abs(
                lot.entry_price
                - lot.entry_price
                / (
                    1.0 + slippage_ratio
                    if position.direction == "long"
                    else 1.0 - slippage_ratio
                )
            )
            for lot in position.lots
        )
        estimated_slippage = entry_slippage + (
            position.contracts
            * float(instrument.contract_value)
            * abs(exit_price - reference_price)
        )
        return BacktestTrade(
            instrument_id=position.instrument_id,
            direction=position.direction,
            opened_at=position.opened_at.isoformat(),
            closed_at=timestamp.isoformat(),
            entry_price=position.entry_price,
            exit_price=exit_price,
            contracts=position.contracts,
            gross_pnl_usdt=gross,
            fees_usdt=fees,
            funding_usdt=funding,
            estimated_slippage_usdt=estimated_slippage,
            costs_usdt=costs,
            net_pnl_usdt=gross - costs,
            exit_reason=reason,
            entry_reason_codes=position.entry_reason_codes,
        )

    def _unrealized(self, position: _OpenPosition, bar: pd.Series) -> float:
        instrument = self.instruments[position.instrument_id]
        direction = 1.0 if position.direction == "long" else -1.0
        gross = sum(
            (float(bar["close"]) - lot.entry_price)
            * lot.contracts
            * float(instrument.contract_value)
            * direction
            for lot in position.lots
        )
        timestamp = bar.name if isinstance(bar.name, pd.Timestamp) else position.opened_at
        funding = self._funding_cost(
            position,
            timestamp,
            mark_price=float(bar["close"]),
        )
        return gross - position.entry_fee_usdt - funding

    def _funding_cost(
        self,
        position: _OpenPosition,
        timestamp: pd.Timestamp,
        *,
        mark_price: float,
    ) -> float:
        if not self._use_actual_funding:
            instrument = self.instruments[position.instrument_id]
            return sum(
                self.cost_model.estimate(
                    notional_usdt=(
                        lot.contracts
                        * float(instrument.contract_value)
                        * mark_price
                    ),
                    holding_hours=max(
                        0.0,
                        (timestamp - lot.opened_at).total_seconds() / 3600.0,
                    ),
                ).funding_usdt
                for lot in position.lots
            )
        rates = self.funding_rates.get(position.instrument_id)
        if rates is None or rates.empty:
            return 0.0
        instrument = self.instruments[position.instrument_id]
        direction = 1.0 if position.direction == "long" else -1.0
        return sum(
            lot.contracts
            * float(instrument.contract_value)
            * mark_price
            * float(
                rates.loc[
                    (rates.index > lot.opened_at) & (rates.index <= timestamp)
                ].sum()
            )
            * direction
            for lot in position.lots
        )

    def _recent_funding_rate(
        self,
        symbol: str,
        timestamp: pd.Timestamp,
    ) -> float | None:
        rates = self.funding_rates.get(symbol)
        if rates is None or rates.empty:
            return None
        count = int(getattr(self.strategy, "funding_rate_lookback_events", 9))
        recent = rates.loc[rates.index <= timestamp].tail(max(1, count))
        return float(recent.mean()) if not recent.empty else None


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
        stop_loss_price=position.stop_loss,
        take_profit_price=position.take_profit,
        opened_at=position.opened_at.isoformat(),
        initial_stop_loss_price=position.initial_stop_loss,
        scale_count=position.scale_count,
    )


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _normalize_funding_rates(values: pd.Series | pd.DataFrame) -> pd.Series:
    if isinstance(values, pd.DataFrame):
        if "funding_rate" not in values.columns:
            raise ValueError("funding-rate frame requires a funding_rate column")
        series = values["funding_rate"].copy()
    elif isinstance(values, pd.Series):
        series = values.copy()
    else:
        raise ValueError("funding rates must be a pandas Series or DataFrame")
    index = pd.DatetimeIndex(series.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    series.index = index
    series = pd.to_numeric(series, errors="coerce").dropna().sort_index()
    return series.loc[~series.index.duplicated(keep="last")]
