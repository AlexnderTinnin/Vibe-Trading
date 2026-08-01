"""Regime-switching BTC/ETH perpetual strategy regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    ConsentMeta,
    HardCaps,
    InstrumentType,
    Mandate,
    UniverseConstraint,
)
from src.trading.connectors.okx.swap import SwapInstrument
from src.trading.quant.backtest import PerpetualBacktester
from src.trading.quant.models import PositionView, TradeAction, TradePlan
from src.trading.quant.sizing import size_trade_plan
from src.trading.quant.strategy import RegimeStrategyConfig, RegimeSwitchingStrategy

pytestmark = pytest.mark.unit


def _bars_4h(*, direction: int, count: int = 120) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=count, freq="4h", tz="UTC")
    trend = np.linspace(100.0, 132.0 if direction > 0 else 68.0, count)
    closes = trend + np.sin(np.arange(count) / 3.0) * 0.25
    closes[-1] = (max(closes[-21:-1]) + 3.0) if direction > 0 else (min(closes[-21:-1]) - 3.0)
    return pd.DataFrame(
        {
            "open": closes - direction * 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [100.0] * (count - 1) + [250.0],
            "confirm": ["1"] * count,
        },
        index=index,
    )


def _daily(*, direction: int, count: int = 130, start: str = "2025-09-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=count, freq="1D", tz="UTC")
    closes = np.linspace(100.0, 180.0 if direction > 0 else 55.0, count)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 1000.0,
            "confirm": "1",
        },
        index=index,
    )


def _volatile_daily(*, count: int = 130, start: str = "2025-09-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=count, freq="1D", tz="UTC")
    returns = np.where(np.arange(count) % 2 == 0, 0.04, -0.03)
    returns[0] = 0.0
    closes = 100.0 * np.cumprod(1.0 + returns)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1000.0,
            "confirm": "1",
        },
        index=index,
    )


def _strategy() -> RegimeSwitchingStrategy:
    return RegimeSwitchingStrategy(
        RegimeStrategyConfig(
            daily_momentum_lookback=30,
            realized_volatility_lookback=20,
            adx_period=10,
            donchian_lookback=20,
            volume_lookback=20,
            volatility_expansion_lookback=10,
            minimum_volume_ratio=1.0,
        )
    )


def _instrument() -> SwapInstrument:
    return SwapInstrument(
        instrument_id="BTC-USDT-SWAP",
        instrument_type="SWAP",
        state="live",
        base_currency="BTC",
        quote_currency="USDT",
        settle_currency="USDT",
        contract_type="linear",
        contract_value=Decimal("0.01"),
        contract_value_currency="BTC",
        lot_size=Decimal("0.01"),
        minimum_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        maximum_market_size=Decimal("10000"),
        maximum_limit_size=Decimal("20000"),
    )


def _mandate() -> Mandate:
    now = datetime.now(timezone.utc)
    return Mandate(
        schema_version=MANDATE_SCHEMA_VERSION,
        hard_caps=HardCaps(
            account_funding_usd=10_000,
            max_order_notional_usd=20_000,
            max_total_exposure_usd=20_000,
            max_leverage=3,
            allowed_instruments=(InstrumentType.PERPETUAL_SWAP,),
            max_trades_per_day=20,
            max_position_notional_usd=20_000,
            max_correlated_exposure_usd=20_000,
            max_daily_loss_usd=1_000,
            max_drawdown_pct=0.30,
            min_liquidation_buffer_pct=0.10,
            max_margin_utilization_pct=0.90,
            max_consecutive_losses=5,
            require_stop_loss=True,
        ),
        universe=UniverseConstraint(
            asset_classes=(AssetClass.CRYPTO,),
            min_market_cap_usd=None,
            min_avg_daily_volume_usd=None,
            exclude_symbols=(),
            include_symbols=("BTC-USDT-SWAP",),
        ),
        consent=ConsentMeta(
            created_at=now.isoformat(),
            consent_token_sha256="e" * 64,
            broker="okx",
            account_ref="demo",
            expires_at=(now + timedelta(days=30)).isoformat(),
        ),
    )


def test_trend_breakout_uses_daily_direction_and_has_no_fixed_take_profit() -> None:
    bars = _bars_4h(direction=1)
    plan = _strategy().evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=bars,
        daily_bars=_daily(direction=1),
        run_id="trend-long",
        now=bars.index[-1].to_pydatetime() + timedelta(hours=4),
    )

    assert plan.action is TradeAction.OPEN_LONG
    assert plan.stop_loss_price is not None and plan.stop_loss_price < plan.reference_price
    assert plan.take_profit_price is None
    assert plan.target_notional_fraction is not None
    assert 0 < plan.target_notional_fraction <= 1
    assert "regime_trend" in plan.reason_codes


def test_three_x_leverage_scales_target_exposure_but_not_risk_fraction() -> None:
    bars = _bars_4h(direction=1)
    common = {
        "daily_momentum_lookback": 30,
        "realized_volatility_lookback": 20,
        "adx_period": 10,
        "donchian_lookback": 20,
        "volume_lookback": 20,
        "volatility_expansion_lookback": 10,
        "max_notional_fraction": 3.0,
        "max_portfolio_notional_fraction": 4.0,
    }
    plans = [
        RegimeSwitchingStrategy(
            RegimeStrategyConfig(leverage=leverage, **common)
        ).evaluate(
            instrument_id="BTC-USDT-SWAP",
            bars=bars,
            daily_bars=_volatile_daily(),
            run_id=f"leverage-{leverage}",
            now=bars.index[-1].to_pydatetime() + timedelta(hours=4),
        )
        for leverage in (1.0, 3.0)
    ]

    assert all(plan.action is TradeAction.OPEN_LONG for plan in plans)
    assert plans[0].target_notional_fraction is not None
    assert plans[1].target_notional_fraction == pytest.approx(
        plans[0].target_notional_fraction * 3.0
    )
    assert plans[0].risk_budget_fraction == plans[1].risk_budget_fraction == 0.02


def test_daily_context_is_cut_off_at_the_primary_bar_close() -> None:
    bars = _bars_4h(direction=1)
    decision_time = bars.index[-1] + pd.Timedelta(hours=4)
    visible = _daily(direction=1)
    visible = visible.loc[visible.index + pd.Timedelta(days=1) <= decision_time]
    future = _daily(direction=-1, count=20, start=(decision_time + pd.Timedelta(days=1)).date().isoformat())

    one = _strategy().evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=bars,
        daily_bars=visible,
        run_id="no-lookahead",
        now=decision_time.to_pydatetime(),
    )
    two = _strategy().evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=bars,
        daily_bars=pd.concat([visible, future]),
        run_id="no-lookahead",
        now=decision_time.to_pydatetime(),
    )

    assert one == two


def test_range_mode_uses_bollinger_rsi_entry_and_midline_target() -> None:
    bars = _bars_4h(direction=1)
    bars.loc[:, "close"] = 100.0 + np.sin(np.arange(len(bars)) / 2.0) * 0.25
    bars.loc[:, "open"] = bars["close"]
    bars.loc[:, "high"] = bars["close"] + 0.3
    bars.loc[:, "low"] = bars["close"] - 0.3
    bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [96.2, 96.4, 95.8, 96.0]

    strategy = RegimeSwitchingStrategy(
        RegimeStrategyConfig(
            daily_momentum_lookback=30,
            realized_volatility_lookback=20,
            adx_period=10,
            trend_adx_threshold=100.0,
            range_adx_threshold=99.0,
            donchian_lookback=20,
            range_long_rsi=45.0,
            range_short_rsi=55.0,
            volume_lookback=20,
            volatility_expansion_lookback=10,
        )
    )
    plan = strategy.evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=bars,
        daily_bars=_daily(direction=1),
        run_id="range-long",
        now=bars.index[-1].to_pydatetime() + timedelta(hours=4),
    )

    assert plan.action is TradeAction.OPEN_LONG
    assert plan.stop_loss_price is not None and plan.stop_loss_price < plan.reference_price
    assert plan.take_profit_price is not None and plan.take_profit_price > plan.reference_price
    assert plan.target_notional_fraction is not None
    assert plan.target_notional_fraction <= 0.5
    assert "regime_range" in plan.reason_codes


def test_trend_position_waits_for_ema_or_daily_direction_reversal() -> None:
    bars = _bars_4h(direction=1)
    closes = 100.0 + np.sin(np.arange(len(bars)) / 2.0) * 0.2
    closes[-1] = 101.0
    bars.loc[:, "open"] = closes
    bars.loc[:, "high"] = closes + 0.3
    bars.loc[:, "low"] = closes - 0.3
    bars.loc[:, "close"] = closes
    strategy = RegimeSwitchingStrategy(
        RegimeStrategyConfig(
            daily_momentum_lookback=30,
            realized_volatility_lookback=20,
            adx_period=10,
            trend_adx_threshold=100.0,
            range_adx_threshold=99.0,
            donchian_lookback=20,
            volume_lookback=20,
            volatility_expansion_lookback=10,
        )
    )
    position = PositionView(
        instrument_id="BTC-USDT-SWAP",
        position_side="long",
        contracts=1.0,
        entry_price=98.0,
        leverage=3.0,
        stop_loss_price=90.0,
        take_profit_price=None,
    )

    held = strategy.evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=bars,
        daily_bars=_daily(direction=1),
        run_id="trend-held",
        now=bars.index[-1].to_pydatetime() + timedelta(hours=4),
        position=position,
    )
    assert held.action is TradeAction.HOLD

    reversed_bars = bars.copy()
    reversed_bars.loc[reversed_bars.index[-1], ["open", "high", "low", "close"]] = [
        95.2,
        95.5,
        94.8,
        95.0,
    ]
    closed = strategy.evaluate(
        instrument_id="BTC-USDT-SWAP",
        bars=reversed_bars,
        daily_bars=_daily(direction=1),
        run_id="trend-reversed",
        now=bars.index[-1].to_pydatetime() + timedelta(hours=4),
        position=position,
    )
    assert closed.action is TradeAction.CLOSE
    assert "ema_exit" in closed.reason_codes


def test_trade_plan_allows_protective_stop_without_fixed_take_profit() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    plan = TradePlan.create(
        strategy_id="trend-no-tp",
        run_id="r1",
        instrument_id="BTC-USDT-SWAP",
        signal_at=now,
        data_as_of=now,
        action=TradeAction.OPEN_SHORT,
        confidence=0.8,
        reference_price=100.0,
        stop_loss_price=108.0,
        take_profit_price=None,
        leverage=3.0,
        risk_budget_usdt=0.0,
        risk_budget_fraction=0.05,
        target_notional_fraction=0.25,
        reason_codes=("test",),
        source_bar_count=100,
    )

    assert plan.is_opening
    assert plan.take_profit_price is None


def test_volatility_target_fraction_caps_risk_sized_notional() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    plan = TradePlan.create(
        strategy_id="vol-target",
        run_id="r1",
        instrument_id="BTC-USDT-SWAP",
        signal_at=now,
        data_as_of=now,
        action=TradeAction.OPEN_LONG,
        confidence=0.8,
        reference_price=100.0,
        stop_loss_price=99.0,
        take_profit_price=None,
        leverage=3.0,
        risk_budget_usdt=0.0,
        risk_budget_fraction=0.05,
        target_notional_fraction=0.25,
        reason_codes=("test",),
        source_bar_count=100,
    )

    sized = size_trade_plan(
        plan,
        _instrument(),
        _mandate(),
        equity_usdt=10_000,
        current_total_exposure_usdt=0,
        current_position_exposure_usdt=0,
        current_correlated_exposure_usdt=0,
        margin_used_usdt=0,
    )

    assert Decimal("2400") <= sized.notional_usdt <= Decimal("2500")


def test_dynamic_portfolio_fraction_caps_incremental_notional() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    plan = TradePlan.create(
        strategy_id="portfolio-cap",
        run_id="r1",
        instrument_id="BTC-USDT-SWAP",
        signal_at=now,
        data_as_of=now,
        action=TradeAction.OPEN_LONG,
        confidence=0.8,
        reference_price=100.0,
        stop_loss_price=99.0,
        take_profit_price=None,
        leverage=3.0,
        risk_budget_usdt=0.0,
        risk_budget_fraction=0.05,
        target_notional_fraction=1.0,
        max_portfolio_notional_fraction=0.15,
        reason_codes=("test",),
        source_bar_count=100,
    )

    sized = size_trade_plan(
        plan,
        _instrument(),
        _mandate(),
        equity_usdt=10_000,
        current_total_exposure_usdt=1_000,
        current_position_exposure_usdt=0,
        current_correlated_exposure_usdt=1_000,
        margin_used_usdt=0,
    )

    assert Decimal("499") <= sized.notional_usdt <= Decimal("500")


def test_backtester_keeps_no_take_profit_position_open_until_strategy_exit() -> None:
    index = pd.date_range("2026-07-01", periods=4, freq="4h", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0, 108.0, 110.0],
            "high": [101.0, 150.0, 112.0, 111.0],
            "low": [99.0, 99.0, 107.0, 109.0],
            "close": [100.0, 108.0, 110.0, 110.0],
            "volume": 100.0,
            "confirm": "1",
        },
        index=index,
    )

    class _NoTakeProfitStrategy:
        def evaluate(self, *, instrument_id, bars, run_id, now, position=None):  # noqa: ANN001
            frame = pd.DataFrame(bars)
            action = TradeAction.HOLD
            if position is None and len(frame) == 1:
                action = TradeAction.OPEN_LONG
            elif position is not None and len(frame) == 3:
                action = TradeAction.CLOSE
            return TradePlan.create(
                strategy_id="no-take-profit",
                run_id=run_id,
                instrument_id=instrument_id,
                signal_at=now,
                data_as_of=frame.index[-1].to_pydatetime(),
                action=action,
                confidence=0.8,
                reference_price=float(frame["close"].iloc[-1]),
                stop_loss_price=90.0 if action is TradeAction.OPEN_LONG else None,
                take_profit_price=None,
                leverage=3.0,
                risk_budget_usdt=0.0,
                risk_budget_fraction=0.01 if action is TradeAction.OPEN_LONG else None,
                target_notional_fraction=0.25 if action is TradeAction.OPEN_LONG else None,
                reason_codes=(
                    "strategy_exit" if action is TradeAction.CLOSE else "test",
                ),
                source_bar_count=len(frame),
            )

    result = PerpetualBacktester(
        strategy=_NoTakeProfitStrategy(),
        mandate=_mandate(),
        instruments={"BTC-USDT-SWAP": _instrument()},
    ).run({"BTC-USDT-SWAP": bars})

    assert result.trade_count == 1
    assert result.trades[0].exit_reason == "strategy_exit"
    assert result.trades[0].closed_at == index[-1].isoformat()
    assert result.trades[0].exit_price > result.trades[0].entry_price


def test_backtest_window_uses_earlier_bars_only_as_indicator_warmup() -> None:
    index = pd.date_range("2026-07-01", periods=5, freq="4h", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [98.0, 99.0, 100.0, 101.0, 102.0],
            "high": [99.0, 100.0, 101.0, 102.0, 103.0],
            "low": [97.0, 98.0, 99.0, 100.0, 101.0],
            "close": [98.5, 99.5, 100.5, 101.5, 102.5],
            "volume": 100.0,
            "confirm": "1",
        },
        index=index,
    )

    signal_times: list[datetime] = []

    class _WarmupAwareStrategy:
        def evaluate(self, *, instrument_id, bars, run_id, now, position=None):  # noqa: ANN001
            frame = pd.DataFrame(bars)
            signal_times.append(now)
            opening = position is None and frame.index[-1] == index[2]
            return TradePlan.create(
                strategy_id="warmup-aware",
                run_id=run_id,
                instrument_id=instrument_id,
                signal_at=now,
                data_as_of=frame.index[-1].to_pydatetime(),
                action=TradeAction.OPEN_LONG if opening else TradeAction.HOLD,
                confidence=0.8,
                reference_price=float(frame["close"].iloc[-1]),
                stop_loss_price=90.0 if opening else None,
                take_profit_price=None,
                leverage=3.0,
                risk_budget_usdt=0.0,
                risk_budget_fraction=0.01 if opening else None,
                target_notional_fraction=0.25 if opening else None,
                reason_codes=("test",),
                source_bar_count=len(frame),
            )

    result = PerpetualBacktester(
        strategy=_WarmupAwareStrategy(),
        mandate=_mandate(),
        instruments={"BTC-USDT-SWAP": _instrument()},
    ).run(
        {"BTC-USDT-SWAP": bars},
        start_at=index[2],
        end_at=index[4],
    )

    assert result.equity_curve[0][0] == index[2].isoformat()
    assert len(result.equity_curve) == 3
    assert signal_times[0] == (index[2] + pd.Timedelta(hours=4)).to_pydatetime()
    assert result.trade_count == 1
    assert result.trades[0].opened_at == index[3].isoformat()
