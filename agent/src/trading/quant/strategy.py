"""Deterministic BTC/ETH EMA-breakout strategy shared by all run modes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pandas as pd

from src.trading.quant.models import PositionView, TradeAction, TradePlan


@dataclass(frozen=True)
class TrendStrategyConfig:
    strategy_id: str = "btc_eth_ema_breakout_v1"
    fast_ema: int = 20
    slow_ema: int = 50
    breakout_lookback: int = 20
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    reward_risk_ratio: float = 2.0
    volume_lookback: int = 20
    leverage: float = 3.0
    risk_budget_usdt: float = 25.0
    minimum_confidence: float = 0.55

    def validate(self) -> None:
        if self.fast_ema <= 1 or self.slow_ema <= self.fast_ema:
            raise ValueError("strategy EMA windows are invalid")
        if min(self.breakout_lookback, self.atr_period, self.volume_lookback) <= 1:
            raise ValueError("strategy lookbacks must be greater than one")
        if not 1 <= self.leverage <= 10 or self.risk_budget_usdt <= 0:
            raise ValueError("strategy leverage/risk budget is invalid")


class BtcEthTrendStrategy:
    """Confirmed-bar trend filter plus prior-window breakout entry/exit."""

    def __init__(self, config: TrendStrategyConfig | None = None) -> None:
        self.config = config or TrendStrategyConfig()
        self.config.validate()

    def evaluate(
        self,
        *,
        instrument_id: str,
        bars: pd.DataFrame | Sequence[Mapping[str, Any]],
        run_id: str,
        now: datetime,
        position: PositionView | None = None,
    ) -> TradePlan:
        frame = _normalize_bars(bars)
        needed = max(
            self.config.slow_ema + 2,
            self.config.breakout_lookback + 2,
            self.config.atr_period + 2,
            self.config.volume_lookback + 2,
        )
        if len(frame) < needed:
            raise ValueError(f"strategy requires at least {needed} confirmed bars")

        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        volume = frame["volume"].astype(float)
        fast = close.ewm(span=self.config.fast_ema, adjust=False).mean()
        slow = close.ewm(span=self.config.slow_ema, adjust=False).mean()
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(self.config.atr_period).mean()
        prior_high = high.shift(1).rolling(self.config.breakout_lookback).max()
        prior_low = low.shift(1).rolling(self.config.breakout_lookback).min()
        median_volume = volume.shift(1).rolling(self.config.volume_lookback).median()

        price = float(close.iloc[-1])
        atr_value = float(atr.iloc[-1])
        if not pd.notna(atr_value) or atr_value <= 0:
            raise ValueError("ATR is unavailable on the latest confirmed bar")
        data_as_of = frame.index[-1].to_pydatetime()
        trend_strength = abs(float(fast.iloc[-1] - slow.iloc[-1])) / price
        volume_ratio = float(volume.iloc[-1] / max(float(median_volume.iloc[-1]), 1e-12))
        confidence = min(1.0, 0.50 + trend_strength * 20.0 + min(volume_ratio, 2.0) * 0.05)

        action = TradeAction.HOLD
        reasons: list[str] = ["no_entry"]
        if position and position.is_open:
            if position.direction == "long" and fast.iloc[-1] < slow.iloc[-1]:
                action, reasons = TradeAction.CLOSE, ["trend_reversal", "close_long"]
            elif position.direction == "short" and fast.iloc[-1] > slow.iloc[-1]:
                action, reasons = TradeAction.CLOSE, ["trend_reversal", "close_short"]
            else:
                reasons = ["position_held", f"trend_{position.direction}"]
        elif confidence >= self.config.minimum_confidence:
            if fast.iloc[-1] > slow.iloc[-1] and price > float(prior_high.iloc[-1]) and volume_ratio >= 1.0:
                action, reasons = TradeAction.OPEN_LONG, ["ema_bull", "breakout_high", "volume_confirmed"]
            elif fast.iloc[-1] < slow.iloc[-1] and price < float(prior_low.iloc[-1]) and volume_ratio >= 1.0:
                action, reasons = TradeAction.OPEN_SHORT, ["ema_bear", "breakout_low", "volume_confirmed"]

        stop: float | None = None
        target: float | None = None
        if action is TradeAction.OPEN_LONG:
            stop = price - atr_value * self.config.atr_stop_multiple
            target = price + (price - stop) * self.config.reward_risk_ratio
        elif action is TradeAction.OPEN_SHORT:
            stop = price + atr_value * self.config.atr_stop_multiple
            target = price - (stop - price) * self.config.reward_risk_ratio

        return TradePlan.create(
            strategy_id=self.config.strategy_id,
            run_id=run_id,
            instrument_id=instrument_id,
            signal_at=_aware(now),
            data_as_of=_aware(data_as_of),
            action=action,
            confidence=confidence,
            reference_price=price,
            stop_loss_price=stop,
            take_profit_price=target,
            leverage=self.config.leverage,
            risk_budget_usdt=self.config.risk_budget_usdt if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT) else 0.0,
            reason_codes=reasons,
            source_bar_count=len(frame),
        )


@dataclass(frozen=True)
class MediumTermTrendConfig:
    """Parameters for the daily momentum and Donchian trend strategy."""

    strategy_id: str = "btc_eth_medium_term_trend_v2"
    momentum_lookback: int = 75
    realized_volatility_lookback: int = 30
    entry_lookback: int = 50
    exit_lookback: int = 20
    adx_period: int = 14
    minimum_adx: float = 15.0
    atr_period: int = 14
    initial_stop_atr_multiple: float = 2.5
    trailing_stop_atr_multiple: float = 2.5
    maximum_initial_stop_fraction: float = 0.20
    minimum_stop_update_atr_fraction: float = 0.10
    target_annual_volatility: float = 0.45
    max_notional_fraction: float = 1.0
    max_portfolio_notional_fraction: float = 1.5
    max_risk_fraction: float = 0.02
    leverage: float = 3.0
    allow_short: bool = True
    maximum_history_bars: int = 300

    def validate(self) -> None:
        lookbacks = (
            self.momentum_lookback,
            self.realized_volatility_lookback,
            self.entry_lookback,
            self.exit_lookback,
            self.adx_period,
            self.atr_period,
        )
        if min(lookbacks) <= 1:
            raise ValueError("medium-term trend lookbacks must be greater than one")
        if self.exit_lookback >= self.entry_lookback:
            raise ValueError("medium-term trend exit lookback must be shorter than entry lookback")
        if not 0 < self.minimum_adx <= 100:
            raise ValueError("medium-term trend ADX threshold is invalid")
        if min(
            self.initial_stop_atr_multiple,
            self.trailing_stop_atr_multiple,
            self.minimum_stop_update_atr_fraction,
            self.target_annual_volatility,
            self.max_notional_fraction,
            self.max_portfolio_notional_fraction,
            self.max_risk_fraction,
        ) <= 0:
            raise ValueError("medium-term trend risk parameters must be positive")
        if self.max_risk_fraction > 1:
            raise ValueError("medium-term trend risk fraction exceeds one")
        if not 0 < self.maximum_initial_stop_fraction < 1:
            raise ValueError("medium-term trend maximum initial stop fraction is invalid")
        if not isinstance(self.allow_short, bool):
            raise ValueError("allow_short must be boolean")
        if not 1 <= self.leverage <= 10:
            raise ValueError("medium-term trend leverage is invalid")
        if (
            not isinstance(self.maximum_history_bars, int)
            or self.maximum_history_bars < self.minimum_history_bars
        ):
            raise ValueError("medium-term trend history window is shorter than its lookbacks")

    @property
    def minimum_history_bars(self) -> int:
        return max(
            self.momentum_lookback + 1,
            self.realized_volatility_lookback + 1,
            self.entry_lookback + 2,
            self.exit_lookback + 2,
            self.adx_period * 3,
            self.atr_period + 2,
        )


class MediumTermTrendStrategy:
    """Daily time-series momentum with Donchian entry and ATR trailing stops."""

    def __init__(self, config: MediumTermTrendConfig | None = None) -> None:
        self.config = config or MediumTermTrendConfig()
        self.config.validate()
        self._prepared_indicators: dict[tuple[str, object], pd.DataFrame] = {}

    @property
    def history_limit(self) -> int:
        """Maximum bars needed by live evaluation and backtest parity."""
        return self.config.maximum_history_bars

    def prepare_backtest(self, frames: Mapping[str, pd.DataFrame]) -> None:
        """Precompute immutable indicators for bar-by-bar research runs."""
        for instrument_id, frame in frames.items():
            token = frame.attrs.get("_quant_backtest_token")
            if token is None:
                continue
            self._prepared_indicators[(instrument_id, token)] = _medium_term_indicators(
                frame,
                self.config,
            )

    def evaluate(
        self,
        *,
        instrument_id: str,
        bars: pd.DataFrame | Sequence[Mapping[str, Any]],
        run_id: str,
        now: datetime,
        position: PositionView | None = None,
    ) -> TradePlan:
        backtest_token = (
            bars.attrs.get("_quant_backtest_token")
            if isinstance(bars, pd.DataFrame)
            else None
        )
        frame = bars if backtest_token is not None else _normalize_bars(bars)
        interval = _bar_interval(frame)
        if not pd.Timedelta(hours=20) <= interval <= pd.Timedelta(hours=28):
            raise ValueError("medium-term trend strategy requires confirmed daily bars")

        needed = self.config.minimum_history_bars
        if len(frame) < needed:
            raise ValueError(
                f"medium-term trend strategy requires at least {needed} confirmed daily bars"
            )
        frame = frame.iloc[-self.config.maximum_history_bars :]

        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        prepared = self._prepared_indicators.get((instrument_id, backtest_token))
        if prepared is not None and frame.index[-1] in prepared.index:
            latest_row = prepared.loc[frame.index[-1]]
        else:
            latest_row = _medium_term_indicators(frame, self.config).iloc[-1]
        latest = tuple(
            latest_row[column]
            for column in (
                "atr",
                "adx",
                "entry_high",
                "entry_low",
                "exit_high",
                "exit_low",
                "momentum",
                "realized_volatility",
            )
        )
        if not all(pd.notna(value) for value in latest):
            raise ValueError("medium-term trend indicators are unavailable")

        price = float(close.iloc[-1])
        atr_value = float(latest_row["atr"])
        adx_value = float(latest_row["adx"])
        momentum = float(latest_row["momentum"])
        realized_volatility = float(latest_row["realized_volatility"])
        if not math.isfinite(realized_volatility) or realized_volatility <= 0:
            raise ValueError("daily realized volatility is unavailable")

        action = TradeAction.HOLD
        stop: float | None = None
        reasons = ["no_entry"]
        confidence = min(
            1.0,
            0.55
            + max(0.0, adx_value - self.config.minimum_adx) / 100.0
            + min(abs(momentum), 0.50) * 0.20,
        )
        target_fraction: float | None = None
        risk_fraction: float | None = None

        if position and position.is_open:
            direction = 1 if position.direction == "long" else -1
            channel_reversed = (
                direction > 0 and price < float(latest_row["exit_low"])
            ) or (
                direction < 0 and price > float(latest_row["exit_high"])
            )
            momentum_reversed = direction * momentum <= 0
            if channel_reversed:
                action = TradeAction.CLOSE
                reasons = ["donchian_exit", f"close_{position.direction}"]
            elif momentum_reversed:
                action = TradeAction.CLOSE
                reasons = ["momentum_reversal", f"close_{position.direction}"]
            else:
                candidate = (
                    float(high.iloc[-1])
                    - self.config.trailing_stop_atr_multiple * atr_value
                    if direction > 0
                    else float(low.iloc[-1])
                    + self.config.trailing_stop_atr_multiple * atr_value
                )
                current = position.stop_loss_price
                improvement = (
                    candidate - current
                    if direction > 0 and current is not None
                    else current - candidate
                    if direction < 0 and current is not None
                    else math.inf
                )
                if improvement >= self.config.minimum_stop_update_atr_fraction * atr_value:
                    action = TradeAction.UPDATE_STOP
                    stop = candidate
                    reasons = ["atr_trailing_stop", f"protect_{position.direction}"]
                else:
                    reasons = ["position_held", f"trend_{position.direction}"]
        elif adx_value >= self.config.minimum_adx:
            if momentum > 0 and price > float(latest_row["entry_high"]):
                action = TradeAction.OPEN_LONG
                stop = max(
                    price - self.config.initial_stop_atr_multiple * atr_value,
                    price * (1.0 - self.config.maximum_initial_stop_fraction),
                )
                reasons = ["momentum_long", "donchian_breakout_high", "adx_confirmed"]
            elif (
                self.config.allow_short
                and momentum < 0
                and price < float(latest_row["entry_low"])
            ):
                action = TradeAction.OPEN_SHORT
                stop = min(
                    price + self.config.initial_stop_atr_multiple * atr_value,
                    price * (1.0 + self.config.maximum_initial_stop_fraction),
                )
                reasons = ["momentum_short", "donchian_breakout_low", "adx_confirmed"]

        if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT):
            target_fraction = min(
                self.config.target_annual_volatility / realized_volatility,
                self.config.max_notional_fraction,
            )
            risk_fraction = self.config.max_risk_fraction

        return TradePlan.create(
            strategy_id=self.config.strategy_id,
            run_id=run_id,
            instrument_id=instrument_id,
            signal_at=_aware(now),
            data_as_of=_aware(frame.index[-1].to_pydatetime()),
            action=action,
            confidence=confidence,
            reference_price=price,
            stop_loss_price=stop,
            take_profit_price=None,
            leverage=self.config.leverage,
            risk_budget_usdt=0.0,
            risk_budget_fraction=risk_fraction,
            target_notional_fraction=target_fraction,
            max_portfolio_notional_fraction=(
                self.config.max_portfolio_notional_fraction
                if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT)
                else None
            ),
            reason_codes=reasons,
            source_bar_count=len(frame),
        )


@dataclass(frozen=True)
class EnhancedMediumTermTrendConfig:
    """Robust, staged daily trend parameters for research and walk-forward tests."""

    strategy_id: str = "btc_eth_medium_term_trend_v3"
    momentum_lookback: int = 75
    realized_volatility_lookback: int = 30
    early_entry_lookback: int = 20
    confirmation_entry_lookback: int = 50
    exit_lookback: int = 20
    trend_ema_period: int = 100
    trend_ema_slope_lookback: int = 20
    adx_period: int = 14
    minimum_adx: float = 20.0
    atr_period: int = 14
    initial_stop_atr_multiple: float = 3.0
    trailing_stop_atr_multiple: float = 4.0
    trailing_activation_r_multiple: float = 1.0
    maximum_initial_stop_fraction: float = 0.20
    minimum_stop_update_atr_fraction: float = 0.10
    momentum_reversal_confirmation_bars: int = 3
    pyramid_atr_step: float = 1.0
    stage_target_fractions: tuple[float, float, float] = (0.40, 0.70, 1.0)
    target_annual_volatility: float = 0.45
    max_notional_fraction: float = 1.0
    max_portfolio_notional_fraction: float = 1.5
    initial_risk_fraction: float = 0.02
    scale_in_risk_fraction: float = 0.01
    max_portfolio_open_risk_fraction: float = 0.045
    adverse_funding_rate_8h: float = 0.0003
    extreme_funding_rate_8h: float = 0.00075
    adverse_funding_notional_scale: float = 0.50
    funding_rate_lookback_events: int = 9
    leverage: float = 3.0
    allow_short: bool = True
    enable_pyramiding: bool = False
    maximum_history_bars: int = 400

    def validate(self) -> None:
        lookbacks = (
            self.momentum_lookback,
            self.realized_volatility_lookback,
            self.early_entry_lookback,
            self.confirmation_entry_lookback,
            self.exit_lookback,
            self.trend_ema_period,
            self.trend_ema_slope_lookback,
            self.adx_period,
            self.atr_period,
        )
        if min(lookbacks) <= 1:
            raise ValueError("enhanced trend lookbacks must be greater than one")
        if self.exit_lookback > self.confirmation_entry_lookback:
            raise ValueError("enhanced trend exit lookback exceeds confirmation entry")
        if self.early_entry_lookback > self.confirmation_entry_lookback:
            raise ValueError("enhanced trend early entry exceeds confirmation entry")
        if not 0 < self.minimum_adx <= 100:
            raise ValueError("enhanced trend ADX threshold is invalid")
        positive = (
            self.initial_stop_atr_multiple,
            self.trailing_stop_atr_multiple,
            self.trailing_activation_r_multiple,
            self.minimum_stop_update_atr_fraction,
            self.pyramid_atr_step,
            self.target_annual_volatility,
            self.max_notional_fraction,
            self.max_portfolio_notional_fraction,
            self.initial_risk_fraction,
            self.scale_in_risk_fraction,
            self.max_portfolio_open_risk_fraction,
            self.adverse_funding_rate_8h,
            self.extreme_funding_rate_8h,
            self.adverse_funding_notional_scale,
        )
        if min(positive) <= 0:
            raise ValueError("enhanced trend risk parameters must be positive")
        if not 0 < self.maximum_initial_stop_fraction < 1:
            raise ValueError("enhanced trend initial stop cap is invalid")
        if self.extreme_funding_rate_8h <= self.adverse_funding_rate_8h:
            raise ValueError("extreme funding threshold must exceed the adverse threshold")
        if self.adverse_funding_notional_scale > 1:
            raise ValueError("adverse funding scale must not exceed one")
        if not 1 <= self.momentum_reversal_confirmation_bars <= 10:
            raise ValueError("momentum reversal confirmation bars are invalid")
        if not 1 <= self.funding_rate_lookback_events <= 90:
            raise ValueError("funding rate lookback events are invalid")
        stages = self.stage_target_fractions
        if (
            len(stages) != 3
            or not 0 < stages[0] < stages[1] < stages[2] <= 1
        ):
            raise ValueError("enhanced trend stage target fractions are invalid")
        if max(
            self.initial_risk_fraction,
            self.scale_in_risk_fraction,
            self.max_portfolio_open_risk_fraction,
        ) > 1:
            raise ValueError("enhanced trend risk fraction exceeds one")
        if not isinstance(self.allow_short, bool) or not isinstance(
            self.enable_pyramiding, bool
        ):
            raise ValueError("enhanced trend boolean settings are invalid")
        if not 1 <= self.leverage <= 10:
            raise ValueError("enhanced trend leverage is invalid")
        if (
            not isinstance(self.maximum_history_bars, int)
            or self.maximum_history_bars < self.minimum_history_bars
        ):
            raise ValueError("enhanced trend history window is shorter than its lookbacks")

    @property
    def minimum_history_bars(self) -> int:
        return max(
            self.momentum_lookback + self.momentum_reversal_confirmation_bars,
            self.realized_volatility_lookback + 1,
            self.confirmation_entry_lookback + 2,
            self.exit_lookback + 2,
            self.trend_ema_period + self.trend_ema_slope_lookback,
            self.adx_period * 3,
            self.atr_period + 2,
        )


class EnhancedMediumTermTrendStrategy:
    """Daily staged trend following with delayed trailing and funding-aware size."""

    requires_funding_rate = True
    research_only = True

    def __init__(self, config: EnhancedMediumTermTrendConfig | None = None) -> None:
        self.config = config or EnhancedMediumTermTrendConfig()
        self.config.validate()
        self._prepared_indicators: dict[tuple[str, object], pd.DataFrame] = {}

    @property
    def history_limit(self) -> int:
        return self.config.maximum_history_bars

    @property
    def funding_rate_lookback_events(self) -> int:
        return self.config.funding_rate_lookback_events

    def prepare_backtest(self, frames: Mapping[str, pd.DataFrame]) -> None:
        for instrument_id, frame in frames.items():
            token = frame.attrs.get("_quant_backtest_token")
            if token is not None:
                self._prepared_indicators[(instrument_id, token)] = (
                    _enhanced_medium_term_indicators(frame, self.config)
                )

    def evaluate(
        self,
        *,
        instrument_id: str,
        bars: pd.DataFrame | Sequence[Mapping[str, Any]],
        run_id: str,
        now: datetime,
        position: PositionView | None = None,
        funding_rate_8h: float | None = None,
    ) -> TradePlan:
        backtest_token = (
            bars.attrs.get("_quant_backtest_token")
            if isinstance(bars, pd.DataFrame)
            else None
        )
        frame = bars if backtest_token is not None else _normalize_bars(bars)
        interval = _bar_interval(frame)
        if not pd.Timedelta(hours=20) <= interval <= pd.Timedelta(hours=28):
            raise ValueError("enhanced trend strategy requires confirmed daily bars")
        if len(frame) < self.config.minimum_history_bars:
            raise ValueError(
                "enhanced trend strategy requires at least "
                f"{self.config.minimum_history_bars} confirmed daily bars"
            )
        frame = frame.iloc[-self.config.maximum_history_bars :]
        prepared = self._prepared_indicators.get((instrument_id, backtest_token))
        indicators = (
            prepared.loc[: frame.index[-1]]
            if prepared is not None and frame.index[-1] in prepared.index
            else _enhanced_medium_term_indicators(frame, self.config)
        )
        latest = indicators.iloc[-1]
        required = (
            "atr",
            "adx",
            "plus_di",
            "minus_di",
            "early_high",
            "early_low",
            "confirmation_high",
            "confirmation_low",
            "exit_high",
            "exit_low",
            "momentum",
            "realized_volatility",
            "trend_ema",
            "trend_ema_slope",
        )
        if not all(pd.notna(latest[column]) for column in required):
            raise ValueError("enhanced trend indicators are unavailable")

        close = frame["close"].astype(float)
        price = float(close.iloc[-1])
        atr_value = float(latest["atr"])
        adx_value = float(latest["adx"])
        momentum = float(latest["momentum"])
        realized_volatility = float(latest["realized_volatility"])
        if not math.isfinite(realized_volatility) or realized_volatility <= 0:
            raise ValueError("enhanced trend realized volatility is unavailable")
        if funding_rate_8h is not None and not math.isfinite(float(funding_rate_8h)):
            raise ValueError("enhanced trend funding rate is invalid")

        long_regime = (
            price > float(latest["trend_ema"])
            and float(latest["trend_ema_slope"]) > 0
            and momentum > 0
            and float(latest["plus_di"]) > float(latest["minus_di"])
            and adx_value >= self.config.minimum_adx
        )
        short_regime = (
            price < float(latest["trend_ema"])
            and float(latest["trend_ema_slope"]) < 0
            and momentum < 0
            and float(latest["minus_di"]) > float(latest["plus_di"])
            and adx_value >= self.config.minimum_adx
        )
        confidence = min(
            1.0,
            0.60
            + max(0.0, adx_value - self.config.minimum_adx) / 100.0
            + min(abs(momentum), 0.50) * 0.20,
        )
        action = TradeAction.HOLD
        stop: float | None = None
        reasons = ["no_entry"]
        risk_fraction: float | None = None
        target_fraction: float | None = None

        if position is not None and position.is_open:
            action, stop, reasons, target_fraction, risk_fraction = self._manage_position(
                position=position,
                frame=frame,
                indicators=indicators,
                latest=latest,
                price=price,
                atr_value=atr_value,
                realized_volatility=realized_volatility,
                long_regime=long_regime,
                short_regime=short_regime,
                funding_rate_8h=funding_rate_8h,
            )
        elif long_regime and price > float(latest["early_high"]):
            action = TradeAction.OPEN_LONG
            stop = max(
                price - self.config.initial_stop_atr_multiple * atr_value,
                price * (1.0 - self.config.maximum_initial_stop_fraction),
            )
            reasons = ["early_donchian_long", "ema_rising", "adx_di_confirmed"]
        elif (
            self.config.allow_short
            and short_regime
            and price < float(latest["early_low"])
        ):
            action = TradeAction.OPEN_SHORT
            stop = min(
                price + self.config.initial_stop_atr_multiple * atr_value,
                price * (1.0 + self.config.maximum_initial_stop_fraction),
            )
            reasons = ["early_donchian_short", "ema_falling", "adx_di_confirmed"]

        if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT):
            direction = 1 if action is TradeAction.OPEN_LONG else -1
            funding_scale = self._funding_scale(direction, funding_rate_8h)
            if funding_scale == 0:
                action = TradeAction.HOLD
                stop = None
                reasons = ["extreme_adverse_funding", "entry_skipped"]
            else:
                base_target = self._base_target_fraction(realized_volatility)
                stage_scale = (
                    self.config.stage_target_fractions[0]
                    if self.config.enable_pyramiding
                    else 1.0
                )
                target_fraction = base_target * stage_scale * funding_scale
                risk_fraction = self.config.initial_risk_fraction * funding_scale
                if funding_scale < 1:
                    reasons.append("adverse_funding_scaled")

        return TradePlan.create(
            strategy_id=self.config.strategy_id,
            run_id=run_id,
            instrument_id=instrument_id,
            signal_at=_aware(now),
            data_as_of=_aware(frame.index[-1].to_pydatetime()),
            action=action,
            confidence=confidence,
            reference_price=price,
            stop_loss_price=stop,
            take_profit_price=None,
            leverage=self.config.leverage,
            risk_budget_usdt=0.0,
            risk_budget_fraction=risk_fraction,
            target_notional_fraction=target_fraction,
            max_portfolio_notional_fraction=(
                self.config.max_portfolio_notional_fraction
                if action in (
                    TradeAction.OPEN_LONG,
                    TradeAction.OPEN_SHORT,
                    TradeAction.ADD_LONG,
                    TradeAction.ADD_SHORT,
                )
                else None
            ),
            max_portfolio_risk_fraction=(
                self.config.max_portfolio_open_risk_fraction
                if action in (
                    TradeAction.OPEN_LONG,
                    TradeAction.OPEN_SHORT,
                    TradeAction.ADD_LONG,
                    TradeAction.ADD_SHORT,
                )
                else None
            ),
            reason_codes=reasons,
            source_bar_count=len(frame),
        )

    def _manage_position(
        self,
        *,
        position: PositionView,
        frame: pd.DataFrame,
        indicators: pd.DataFrame,
        latest: pd.Series,
        price: float,
        atr_value: float,
        realized_volatility: float,
        long_regime: bool,
        short_regime: bool,
        funding_rate_8h: float | None,
    ) -> tuple[TradeAction, float | None, list[str], float | None, float | None]:
        direction = 1 if position.direction == "long" else -1
        channel_reversed = (
            direction > 0 and price < float(latest["exit_low"])
        ) or (
            direction < 0 and price > float(latest["exit_high"])
        )
        if channel_reversed:
            return (
                TradeAction.CLOSE,
                None,
                ["donchian_exit", f"close_{position.direction}"],
                None,
                None,
            )

        recent_momentum = indicators["momentum"].dropna().iloc[
            -self.config.momentum_reversal_confirmation_bars :
        ]
        confirmed_reversal = (
            len(recent_momentum) == self.config.momentum_reversal_confirmation_bars
            and bool((direction * recent_momentum <= 0).all())
        )
        if confirmed_reversal:
            return (
                TradeAction.CLOSE,
                None,
                ["confirmed_momentum_reversal", f"close_{position.direction}"],
                None,
                None,
            )

        current_stop = position.stop_loss_price
        if current_stop is None:
            raise ValueError("enhanced trend position requires a durable stop")
        regime_held = long_regime if direction > 0 else short_regime
        scale_count = max(1, int(position.scale_count))
        if self.config.enable_pyramiding and regime_held and scale_count < 3:
            confirmation_crossed = (
                direction > 0 and price > float(latest["confirmation_high"])
            ) or (
                direction < 0 and price < float(latest["confirmation_low"])
            )
            favorable_move = direction * (price - position.entry_price)
            pyramid_crossed = favorable_move >= self.config.pyramid_atr_step * atr_value
            should_add = confirmation_crossed if scale_count == 1 else pyramid_crossed
            if should_add:
                funding_scale = self._funding_scale(direction, funding_rate_8h)
                if funding_scale > 0:
                    action = TradeAction.ADD_LONG if direction > 0 else TradeAction.ADD_SHORT
                    protected_stop = (
                        max(current_stop, position.entry_price)
                        if direction > 0
                        else min(current_stop, position.entry_price)
                    )
                    target = (
                        self._base_target_fraction(realized_volatility)
                        * self.config.stage_target_fractions[scale_count]
                        * funding_scale
                    )
                    reasons = [
                        "confirmation_scale_in" if scale_count == 1 else "profit_scale_in",
                        f"scale_level_{scale_count + 1}",
                        "existing_risk_protected",
                    ]
                    if funding_scale < 1:
                        reasons.append("adverse_funding_scaled")
                    return (
                        action,
                        protected_stop,
                        reasons,
                        target,
                        self.config.scale_in_risk_fraction * funding_scale,
                    )

        initial_stop = (
            position.initial_stop_loss_price
            if position.initial_stop_loss_price is not None
            else current_stop
        )
        initial_risk = abs(position.entry_price - initial_stop)
        favorable_move = direction * (price - position.entry_price)
        if initial_risk <= 0 or favorable_move < (
            self.config.trailing_activation_r_multiple * initial_risk
        ):
            reasons = ["position_held", "trailing_not_armed"]
            if not regime_held:
                reasons.append("trend_confirmation_softened")
            return TradeAction.HOLD, None, reasons, None, None

        opened_at = pd.Timestamp(position.opened_at) if position.opened_at else frame.index[0]
        if opened_at.tzinfo is None:
            opened_at = opened_at.tz_localize("UTC")
        else:
            opened_at = opened_at.tz_convert("UTC")
        since_entry = frame.loc[frame.index >= opened_at]
        if since_entry.empty:
            since_entry = frame.iloc[[-1]]
        candidate = (
            float(since_entry["high"].max())
            - self.config.trailing_stop_atr_multiple * atr_value
            if direction > 0
            else float(since_entry["low"].min())
            + self.config.trailing_stop_atr_multiple * atr_value
        )
        improvement = candidate - current_stop if direction > 0 else current_stop - candidate
        crossed = candidate >= price if direction > 0 else candidate <= price
        if (
            improvement >= self.config.minimum_stop_update_atr_fraction * atr_value
            and not crossed
        ):
            return (
                TradeAction.UPDATE_STOP,
                candidate,
                ["delayed_atr_trailing_stop", f"protect_{position.direction}"],
                None,
                None,
            )
        return TradeAction.HOLD, None, ["position_held", "trend_active"], None, None

    def _base_target_fraction(self, realized_volatility: float) -> float:
        return min(
            self.config.target_annual_volatility / realized_volatility,
            self.config.max_notional_fraction,
        )

    def _funding_scale(self, direction: int, funding_rate_8h: float | None) -> float:
        if funding_rate_8h is None:
            return 1.0
        adverse_rate = direction * float(funding_rate_8h)
        if adverse_rate >= self.config.extreme_funding_rate_8h:
            return 0.0
        if adverse_rate >= self.config.adverse_funding_rate_8h:
            return self.config.adverse_funding_notional_scale
        return 1.0


@dataclass(frozen=True)
class RegimeStrategyConfig:
    """Parameters for the 1D-filtered, 4H regime-switching strategy."""

    strategy_id: str = "btc_eth_regime_switch_v2"
    daily_momentum_lookback: int = 90
    realized_volatility_lookback: int = 20
    adx_period: int = 14
    trend_adx_threshold: float = 25.0
    range_adx_threshold: float = 18.0
    donchian_lookback: int = 20
    bollinger_period: int = 20
    bollinger_stddev: float = 2.0
    rsi_period: int = 14
    range_long_rsi: float = 35.0
    range_short_rsi: float = 65.0
    atr_period: int = 14
    trend_stop_atr_multiple: float = 4.0
    range_stop_atr_multiple: float = 2.0
    trend_exit_ema: int = 20
    volume_lookback: int = 20
    minimum_volume_ratio: float = 1.0
    volatility_expansion_lookback: int = 20
    target_annual_volatility: float = 0.15
    scale_target_with_leverage: bool = True
    max_notional_fraction: float = 1.0
    max_portfolio_notional_fraction: float = 1.5
    range_notional_scale: float = 0.5
    max_risk_fraction: float = 0.02
    leverage: float = 3.0
    minimum_confidence: float = 0.55

    def validate(self) -> None:
        lookbacks = (
            self.daily_momentum_lookback,
            self.realized_volatility_lookback,
            self.adx_period,
            self.donchian_lookback,
            self.bollinger_period,
            self.rsi_period,
            self.atr_period,
            self.trend_exit_ema,
            self.volume_lookback,
            self.volatility_expansion_lookback,
        )
        if min(lookbacks) <= 1:
            raise ValueError("regime strategy lookbacks must be greater than one")
        if not 0 <= self.range_adx_threshold < self.trend_adx_threshold <= 100:
            raise ValueError("regime ADX thresholds are invalid")
        if not 0 < self.range_long_rsi < self.range_short_rsi < 100:
            raise ValueError("regime RSI thresholds are invalid")
        if min(
            self.bollinger_stddev,
            self.trend_stop_atr_multiple,
            self.range_stop_atr_multiple,
            self.minimum_volume_ratio,
            self.target_annual_volatility,
            self.max_notional_fraction,
            self.max_portfolio_notional_fraction,
            self.range_notional_scale,
            self.max_risk_fraction,
        ) <= 0:
            raise ValueError("regime strategy risk and signal parameters must be positive")
        if self.range_notional_scale > 1 or self.max_risk_fraction > 1:
            raise ValueError("regime strategy fraction parameters exceed one")
        if not isinstance(self.scale_target_with_leverage, bool):
            raise ValueError("scale_target_with_leverage must be boolean")
        if not 1 <= self.leverage <= 10 or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("regime strategy leverage/confidence is invalid")


class RegimeSwitchingStrategy:
    """Daily momentum filter with 4H trend-breakout/range-reversion modes."""

    requires_daily_bars = True

    def __init__(self, config: RegimeStrategyConfig | None = None) -> None:
        self.config = config or RegimeStrategyConfig()
        self.config.validate()

    def evaluate(
        self,
        *,
        instrument_id: str,
        bars: pd.DataFrame | Sequence[Mapping[str, Any]],
        run_id: str,
        now: datetime,
        position: PositionView | None = None,
        daily_bars: pd.DataFrame | Sequence[Mapping[str, Any]] | None = None,
    ) -> TradePlan:
        frame = _normalize_bars(bars)
        primary_interval = _bar_interval(frame)
        if not pd.Timedelta(hours=3) <= primary_interval <= pd.Timedelta(hours=5):
            raise ValueError("regime strategy requires confirmed 4H primary bars")
        decision_time = frame.index[-1] + primary_interval
        daily = (
            _normalize_bars(daily_bars)
            if daily_bars is not None
            else _daily_from_primary(frame, primary_interval)
        )
        daily = daily.loc[daily.index + pd.Timedelta(days=1) <= decision_time]

        needed_primary = max(
            self.config.adx_period * 3,
            self.config.donchian_lookback + 2,
            self.config.bollinger_period + self.config.volatility_expansion_lookback + 2,
            self.config.rsi_period + 2,
            self.config.atr_period + 2,
            self.config.trend_exit_ema + 2,
            self.config.volume_lookback + 2,
        )
        needed_daily = max(
            self.config.daily_momentum_lookback + 1,
            self.config.realized_volatility_lookback + 1,
        )
        if len(frame) < needed_primary:
            raise ValueError(f"regime strategy requires at least {needed_primary} confirmed 4H bars")
        if len(daily) < needed_daily:
            raise ValueError(f"regime strategy requires at least {needed_daily} confirmed daily bars")

        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        volume = frame["volume"].astype(float)
        atr = _wilder_atr(high, low, close, self.config.atr_period)
        adx = _wilder_adx(high, low, close, self.config.adx_period)
        rsi = _wilder_rsi(close, self.config.rsi_period)
        ema = close.ewm(span=self.config.trend_exit_ema, adjust=False).mean()
        middle = close.rolling(self.config.bollinger_period).mean()
        deviation = close.rolling(self.config.bollinger_period).std(ddof=0)
        upper = middle + deviation * self.config.bollinger_stddev
        lower = middle - deviation * self.config.bollinger_stddev
        width = (upper - lower) / middle.where(middle != 0)
        prior_width = width.shift(1).rolling(self.config.volatility_expansion_lookback).median()
        prior_high = high.shift(1).rolling(self.config.donchian_lookback).max()
        prior_low = low.shift(1).rolling(self.config.donchian_lookback).min()
        prior_volume = volume.shift(1).rolling(self.config.volume_lookback).median()

        required_latest = (
            atr.iloc[-1],
            adx.iloc[-1],
            rsi.iloc[-1],
            middle.iloc[-1],
            upper.iloc[-1],
            lower.iloc[-1],
            prior_width.iloc[-1],
            prior_high.iloc[-1],
            prior_low.iloc[-1],
            prior_volume.iloc[-1],
        )
        if not all(pd.notna(value) for value in required_latest):
            raise ValueError("regime indicators are unavailable on the latest confirmed bar")

        daily_close = daily["close"].astype(float)
        momentum = float(
            daily_close.iloc[-1]
            / daily_close.iloc[-1 - self.config.daily_momentum_lookback]
            - 1.0
        )
        daily_returns = daily_close.pct_change()
        realized_volatility = float(
            daily_returns.iloc[-self.config.realized_volatility_lookback :].std(ddof=1)
            * math.sqrt(365.0)
        )
        if not math.isfinite(realized_volatility) or realized_volatility <= 0:
            raise ValueError("daily realized volatility is unavailable")

        price = float(close.iloc[-1])
        atr_value = float(atr.iloc[-1])
        adx_value = float(adx.iloc[-1])
        rsi_value = float(rsi.iloc[-1])
        middle_value = float(middle.iloc[-1])
        volume_ratio = float(volume.iloc[-1] / max(float(prior_volume.iloc[-1]), 1e-12))
        volatility_expanding = float(width.iloc[-1]) >= float(prior_width.iloc[-1])
        daily_direction = 1 if momentum > 0 else -1 if momentum < 0 else 0
        regime = (
            "trend"
            if adx_value >= self.config.trend_adx_threshold and volatility_expanding
            else "range"
            if adx_value <= self.config.range_adx_threshold
            else "neutral"
        )

        action = TradeAction.HOLD
        reasons = [f"regime_{regime}", "no_entry"]
        confidence = 0.5
        if position and position.is_open:
            action, reasons, confidence = self._position_action(
                position=position,
                regime=regime,
                daily_direction=daily_direction,
                price=price,
                ema=float(ema.iloc[-1]),
                middle=middle_value,
            )
        elif regime == "trend":
            confidence = min(
                1.0,
                0.55
                + max(0.0, adx_value - self.config.trend_adx_threshold) / 100.0
                + min(max(volume_ratio - 1.0, 0.0) * 0.10, 0.10),
            )
            if (
                daily_direction > 0
                and price > float(prior_high.iloc[-1])
                and volume_ratio >= self.config.minimum_volume_ratio
                and confidence >= self.config.minimum_confidence
            ):
                action = TradeAction.OPEN_LONG
                reasons = ["regime_trend", "daily_momentum_long", "donchian_breakout_high", "volume_confirmed"]
            elif (
                daily_direction < 0
                and price < float(prior_low.iloc[-1])
                and volume_ratio >= self.config.minimum_volume_ratio
                and confidence >= self.config.minimum_confidence
            ):
                action = TradeAction.OPEN_SHORT
                reasons = ["regime_trend", "daily_momentum_short", "donchian_breakout_low", "volume_confirmed"]
        elif regime == "range":
            confidence = min(1.0, 0.55 + abs(rsi_value - 50.0) / 200.0)
            if (
                daily_direction > 0
                and price <= float(lower.iloc[-1])
                and rsi_value <= self.config.range_long_rsi
                and confidence >= self.config.minimum_confidence
            ):
                action = TradeAction.OPEN_LONG
                reasons = ["regime_range", "daily_momentum_long", "bollinger_lower", "rsi_oversold"]
            elif (
                daily_direction < 0
                and price >= float(upper.iloc[-1])
                and rsi_value >= self.config.range_short_rsi
                and confidence >= self.config.minimum_confidence
            ):
                action = TradeAction.OPEN_SHORT
                reasons = ["regime_range", "daily_momentum_short", "bollinger_upper", "rsi_overbought"]

        stop: float | None = None
        target: float | None = None
        target_fraction: float | None = None
        risk_fraction: float | None = None
        if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT):
            direction = 1.0 if action is TradeAction.OPEN_LONG else -1.0
            stop_multiple = (
                self.config.trend_stop_atr_multiple
                if regime == "trend"
                else self.config.range_stop_atr_multiple
            )
            stop = price - direction * atr_value * stop_multiple
            target = middle_value if regime == "range" else None
            exposure_multiplier = (
                self.config.leverage if self.config.scale_target_with_leverage else 1.0
            )
            target_fraction = min(
                self.config.target_annual_volatility
                * exposure_multiplier
                / realized_volatility,
                self.config.max_notional_fraction,
            )
            if regime == "range":
                target_fraction *= self.config.range_notional_scale
            risk_fraction = self.config.max_risk_fraction

        return TradePlan.create(
            strategy_id=self.config.strategy_id,
            run_id=run_id,
            instrument_id=instrument_id,
            signal_at=_aware(now),
            data_as_of=_aware(frame.index[-1].to_pydatetime()),
            action=action,
            confidence=confidence,
            reference_price=price,
            stop_loss_price=stop,
            take_profit_price=target,
            leverage=self.config.leverage,
            risk_budget_usdt=0.0,
            risk_budget_fraction=risk_fraction,
            target_notional_fraction=target_fraction,
            max_portfolio_notional_fraction=(
                self.config.max_portfolio_notional_fraction
                if action in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT)
                else None
            ),
            reason_codes=reasons,
            source_bar_count=len(frame),
        )

    def _position_action(
        self,
        *,
        position: PositionView,
        regime: str,
        daily_direction: int,
        price: float,
        ema: float,
        middle: float,
    ) -> tuple[TradeAction, list[str], float]:
        position_direction = 1 if position.direction == "long" else -1
        if daily_direction != position_direction:
            return TradeAction.CLOSE, ["daily_momentum_reversal", f"close_{position.direction}"], 0.8
        trend_position = position.take_profit_price is None
        if trend_position:
            reversed_ema = (position_direction > 0 and price < ema) or (
                position_direction < 0 and price > ema
            )
            if reversed_ema:
                return TradeAction.CLOSE, ["ema_exit", f"close_{position.direction}"], 0.75
        else:
            reverted = (position_direction > 0 and price >= middle) or (
                position_direction < 0 and price <= middle
            )
            if reverted:
                return TradeAction.CLOSE, ["bollinger_mid_exit", f"close_{position.direction}"], 0.75
        return TradeAction.HOLD, ["position_held", f"regime_{regime}", f"direction_{position.direction}"], 0.6


def _bar_interval(frame: pd.DataFrame) -> pd.Timedelta:
    if len(frame.index) < 2:
        raise ValueError("at least two bars are required to infer the bar interval")
    intervals = frame.index.to_series().diff().dropna()
    interval = intervals.median()
    if not isinstance(interval, pd.Timedelta) or interval <= pd.Timedelta(0):
        raise ValueError("bar interval is invalid")
    return interval


def _daily_from_primary(frame: pd.DataFrame, interval: pd.Timedelta) -> pd.DataFrame:
    expected = round(pd.Timedelta(days=1) / interval)
    if expected <= 0:
        raise ValueError("primary bar interval cannot be converted to daily bars")
    daily = frame.resample("1D").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    counts = frame["close"].resample("1D").count()
    return daily.loc[counts == expected].dropna()


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    return values.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous = close.shift(1)
    return pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()],
        axis=1,
    ).max(axis=1)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return _wilder_average(_true_range(high, low, close), period)


def _wilder_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    adx, _, _ = _wilder_directional_index(high, low, close, period)
    return adx


def _wilder_directional_index(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    upward = high.diff()
    downward = -low.diff()
    plus_dm = upward.where((upward > downward) & (upward > 0), 0.0)
    minus_dm = downward.where((downward > upward) & (downward > 0), 0.0)
    atr = _wilder_atr(high, low, close, period)
    plus_di = 100.0 * _wilder_average(plus_dm, period) / atr.where(atr != 0)
    minus_di = 100.0 * _wilder_average(minus_dm, period) / atr.where(atr != 0)
    denominator = (plus_di + minus_di).where((plus_di + minus_di) != 0)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return _wilder_average(dx, period), plus_di, minus_di


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    change = close.diff()
    gains = _wilder_average(change.clip(lower=0), period)
    losses = _wilder_average((-change.clip(upper=0)), period)
    relative_strength = gains / losses.where(losses != 0)
    result = 100.0 - 100.0 / (1.0 + relative_strength)
    return result.mask((losses == 0) & (gains > 0), 100.0).mask((losses == 0) & (gains == 0), 50.0)


def _medium_term_indicators(
    frame: pd.DataFrame,
    config: MediumTermTrendConfig,
) -> pd.DataFrame:
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    return pd.DataFrame(
        {
            "atr": _wilder_atr(high, low, close, config.atr_period),
            "adx": _wilder_adx(high, low, close, config.adx_period),
            "entry_high": high.shift(1).rolling(config.entry_lookback).max(),
            "entry_low": low.shift(1).rolling(config.entry_lookback).min(),
            "exit_high": high.shift(1).rolling(config.exit_lookback).max(),
            "exit_low": low.shift(1).rolling(config.exit_lookback).min(),
            "momentum": close / close.shift(config.momentum_lookback) - 1.0,
            "realized_volatility": (
                close.pct_change()
                .rolling(config.realized_volatility_lookback)
                .std(ddof=1)
                * math.sqrt(365.0)
            ),
        },
        index=frame.index,
    )


def _enhanced_medium_term_indicators(
    frame: pd.DataFrame,
    config: EnhancedMediumTermTrendConfig,
) -> pd.DataFrame:
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    adx, plus_di, minus_di = _wilder_directional_index(
        high,
        low,
        close,
        config.adx_period,
    )
    trend_ema = close.ewm(
        span=config.trend_ema_period,
        adjust=False,
        min_periods=config.trend_ema_period,
    ).mean()
    return pd.DataFrame(
        {
            "atr": _wilder_atr(high, low, close, config.atr_period),
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "early_high": high.shift(1).rolling(config.early_entry_lookback).max(),
            "early_low": low.shift(1).rolling(config.early_entry_lookback).min(),
            "confirmation_high": high.shift(1)
            .rolling(config.confirmation_entry_lookback)
            .max(),
            "confirmation_low": low.shift(1)
            .rolling(config.confirmation_entry_lookback)
            .min(),
            "exit_high": high.shift(1).rolling(config.exit_lookback).max(),
            "exit_low": low.shift(1).rolling(config.exit_lookback).min(),
            "momentum": close / close.shift(config.momentum_lookback) - 1.0,
            "realized_volatility": (
                close.pct_change()
                .rolling(config.realized_volatility_lookback)
                .std(ddof=1)
                * math.sqrt(365.0)
            ),
            "trend_ema": trend_ema,
            "trend_ema_slope": (
                trend_ema / trend_ema.shift(config.trend_ema_slope_lookback) - 1.0
            ),
        },
        index=frame.index,
    )


def _normalize_bars(bars: pd.DataFrame | Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if isinstance(bars, pd.DataFrame):
        frame = bars.copy()
    else:
        frame = pd.DataFrame([dict(row) for row in bars])
    aliases = {"ts": "timestamp", "time": "timestamp", "vol": "volume"}
    frame = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns})
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {', '.join(sorted(missing))}")
    if "confirm" in frame.columns:
        confirmed = frame["confirm"].astype(str).str.lower().isin({"1", "true"})
        frame = frame.loc[confirmed]
    if not isinstance(frame.index, pd.DatetimeIndex):
        if "timestamp" not in frame.columns:
            raise ValueError("bars require a DatetimeIndex or timestamp column")
        numeric = pd.to_numeric(frame["timestamp"], errors="coerce")
        if numeric.notna().all():
            frame.index = pd.to_datetime(numeric, unit="ms", utc=True)
        else:
            frame.index = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    elif frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=sorted(required))
    if frame.empty:
        raise ValueError("no usable confirmed bars")
    return frame


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
