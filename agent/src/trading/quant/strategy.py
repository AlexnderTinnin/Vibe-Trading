"""Deterministic BTC/ETH EMA-breakout strategy shared by all run modes."""

from __future__ import annotations

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
