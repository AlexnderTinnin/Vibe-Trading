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
class RegimeStrategyConfig:
    """Parameters for the 1D-filtered, 4H regime-switching strategy."""

    strategy_id: str = "btc_eth_regime_switch_v1"
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
    max_notional_fraction: float = 1.0
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
            self.range_notional_scale,
            self.max_risk_fraction,
        ) <= 0:
            raise ValueError("regime strategy risk and signal parameters must be positive")
        if self.range_notional_scale > 1 or self.max_risk_fraction > 1:
            raise ValueError("regime strategy fraction parameters exceed one")
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
            target_fraction = min(
                self.config.target_annual_volatility / realized_volatility,
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
    upward = high.diff()
    downward = -low.diff()
    plus_dm = upward.where((upward > downward) & (upward > 0), 0.0)
    minus_dm = downward.where((downward > upward) & (downward > 0), 0.0)
    atr = _wilder_atr(high, low, close, period)
    plus_di = 100.0 * _wilder_average(plus_dm, period) / atr.where(atr != 0)
    minus_di = 100.0 * _wilder_average(minus_dm, period) / atr.where(atr != 0)
    denominator = (plus_di + minus_di).where((plus_di + minus_di) != 0)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return _wilder_average(dx, period)


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    change = close.diff()
    gains = _wilder_average(change.clip(lower=0), period)
    losses = _wilder_average((-change.clip(upper=0)), period)
    relative_strength = gains / losses.where(losses != 0)
    result = 100.0 - 100.0 / (1.0 + relative_strength)
    return result.mask((losses == 0) & (gains > 0), 100.0).mask((losses == 0) & (gains == 0), 50.0)


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
