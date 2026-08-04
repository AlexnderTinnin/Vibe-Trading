"""Phase 3 TradePlan, strategy, sizing, costs, backtest, and runner tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import src.live.paths as live_paths
from src.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    ConsentMeta,
    HardCaps,
    InstrumentType,
    Mandate,
    UniverseConstraint,
)
from src.live.runtime.swap_state import SwapRuntimeStore, swap_breaker_status
from src.trading.connectors.okx.swap import SwapInstrument
from src.trading.quant.backtest import PerpetualBacktester, _OpenPosition, _PositionLot
from src.trading.quant.costs import PerpetualCostModel
from src.trading.quant.models import ExecutionMode, TradeAction, TradePlan
from src.trading.quant.runner import (
    CryptoRunnerConfig,
    CryptoStrategyRunner,
    _align_trigger_price,
    _position_view,
    quant_status,
)
from src.trading.quant.sizing import size_trade_plan
from src.trading.quant.strategy import BtcEthTrendStrategy, TrendStrategyConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(live_paths, "get_runtime_root", lambda: tmp_path)
    return tmp_path


def _mandate() -> Mandate:
    now = datetime.now(timezone.utc)
    return Mandate(
        schema_version=MANDATE_SCHEMA_VERSION,
        hard_caps=HardCaps(
            account_funding_usd=10_000,
            max_order_notional_usd=3_000,
            max_total_exposure_usd=6_000,
            max_leverage=5,
            allowed_instruments=(InstrumentType.PERPETUAL_SWAP,),
            max_trades_per_day=20,
            max_position_notional_usd=3_000,
            max_correlated_exposure_usd=5_000,
            max_daily_loss_usd=200,
            max_drawdown_pct=0.10,
            min_liquidation_buffer_pct=0.10,
            max_margin_utilization_pct=0.50,
            max_consecutive_losses=3,
            require_stop_loss=True,
        ),
        universe=UniverseConstraint(
            asset_classes=(AssetClass.CRYPTO,),
            min_market_cap_usd=None,
            min_avg_daily_volume_usd=None,
            exclude_symbols=(),
            include_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        ),
        consent=ConsentMeta(
            created_at=now.isoformat(),
            consent_token_sha256="b" * 64,
            broker="okx",
            account_ref="demo",
            expires_at=(now + timedelta(days=30)).isoformat(),
        ),
    )


def _instrument(symbol: str = "BTC-USDT-SWAP") -> SwapInstrument:
    base = symbol.split("-")[0]
    return SwapInstrument(
        instrument_id=symbol,
        instrument_type="SWAP",
        state="live",
        base_currency=base,
        quote_currency="USDT",
        settle_currency="USDT",
        contract_type="linear",
        contract_value=Decimal("0.01" if base == "BTC" else "0.1"),
        contract_value_currency=base,
        lot_size=Decimal("0.01"),
        minimum_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        maximum_market_size=Decimal("10000"),
        maximum_limit_size=Decimal("20000"),
    )


def _bullish_bars(now: datetime, count: int = 90) -> pd.DataFrame:
    index = pd.date_range(end=now, periods=count, freq="15min", tz="UTC")
    closes = [100.0 + i * 0.2 for i in range(count)]
    closes[-1] = closes[-2] + 5.0
    frame = pd.DataFrame(
        {
            "open": [value - 0.1 for value in closes],
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [100.0] * (count - 1) + [250.0],
            "confirm": ["1"] * count,
        },
        index=index,
    )
    return frame


def _strategy() -> BtcEthTrendStrategy:
    return BtcEthTrendStrategy(
        TrendStrategyConfig(
            fast_ema=5,
            slow_ema=12,
            breakout_lookback=8,
            atr_period=5,
            volume_lookback=8,
            leverage=3,
            risk_budget_usdt=25,
        )
    )


def test_trade_plan_is_deterministic_and_directionally_valid() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    strategy = _strategy()
    one = strategy.evaluate(
        instrument_id="BTC-USDT-SWAP", bars=_bullish_bars(now), run_id="r1", now=now
    )
    two = strategy.evaluate(
        instrument_id="BTC-USDT-SWAP", bars=_bullish_bars(now), run_id="r1", now=now
    )
    assert one == two
    assert one.action is TradeAction.OPEN_LONG
    assert one.stop_loss_price < one.reference_price < one.take_profit_price
    assert one.plan_id.startswith("tp_")


def test_sizing_applies_risk_and_contract_lot_caps() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    plan = _strategy().evaluate(
        instrument_id="BTC-USDT-SWAP", bars=_bullish_bars(now), run_id="r1", now=now
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
    assert sized.contracts >= Decimal("0.01")
    assert sized.contracts % Decimal("0.01") == 0
    assert sized.notional_usdt <= Decimal("3000")
    assert sized.margin_usdt == sized.notional_usdt / Decimal("3")


def test_sizing_revalidates_levels_at_the_execution_reference() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    plan = _strategy().evaluate(
        instrument_id="BTC-USDT-SWAP", bars=_bullish_bars(now), run_id="r1", now=now
    )
    execution_price = plan.reference_price + 0.1
    sized = size_trade_plan(
        plan,
        _instrument(),
        _mandate(),
        equity_usdt=10_000,
        current_total_exposure_usdt=0,
        current_position_exposure_usdt=0,
        current_correlated_exposure_usdt=0,
        margin_used_usdt=0,
        execution_reference_price=execution_price,
    )
    assert sized.reference_price == Decimal(str(execution_price))
    with pytest.raises(ValueError, match="outside the long stop/target range"):
        size_trade_plan(
            plan,
            _instrument(),
            _mandate(),
            equity_usdt=10_000,
            current_total_exposure_usdt=0,
            current_position_exposure_usdt=0,
            current_correlated_exposure_usdt=0,
            margin_used_usdt=0,
            execution_reference_price=float(plan.take_profit_price),
        )


def test_sizing_rejects_stop_beyond_estimated_liquidation() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    plan = TradePlan.create(
        strategy_id="liquidation-guard",
        run_id="r1",
        instrument_id="BTC-USDT-SWAP",
        signal_at=now,
        data_as_of=now,
        action=TradeAction.OPEN_LONG,
        confidence=1.0,
        reference_price=100.0,
        stop_loss_price=70.0,
        take_profit_price=None,
        leverage=3.0,
        risk_budget_usdt=100.0,
        reason_codes=("test",),
        source_bar_count=100,
    )

    with pytest.raises(ValueError, match="above the estimated liquidation"):
        size_trade_plan(
            plan,
            _instrument(),
            _mandate(),
            equity_usdt=10_000,
            current_total_exposure_usdt=0,
            current_position_exposure_usdt=0,
            current_correlated_exposure_usdt=0,
            margin_used_usdt=0,
            execution_reference_price=110.0,
        )


def test_cost_model_includes_fee_slippage_funding_and_liquidation() -> None:
    model = PerpetualCostModel()
    costs = model.estimate(notional_usdt=10_000, holding_hours=8)
    assert costs.fee_usdt == pytest.approx(5.0)
    assert costs.slippage_usdt == pytest.approx(3.0)
    assert costs.funding_usdt == pytest.approx(1.0)
    assert costs.total_usdt == pytest.approx(9.0)
    assert model.estimated_liquidation_price(entry_price=100, leverage=10, direction="long") < 100
    assert model.estimated_liquidation_price(entry_price=100, leverage=10, direction="short") > 100


def test_actual_funding_rates_charge_longs_and_credit_shorts() -> None:
    opened = pd.Timestamp("2026-01-01", tz="UTC")
    closed = pd.Timestamp("2026-01-02", tz="UTC")
    rates = pd.Series(
        [0.01, 0.02],
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-01-01 08:00", tz="UTC"), closed]
        ),
    )
    backtester = PerpetualBacktester(
        strategy=_strategy(),
        mandate=_mandate(),
        instruments={"BTC-USDT-SWAP": _instrument()},
        funding_rates_by_instrument={"BTC-USDT-SWAP": rates},
    )

    def position(direction: str) -> _OpenPosition:
        return _OpenPosition(
            instrument_id="BTC-USDT-SWAP",
            direction=direction,
            lots=[
                _PositionLot(
                    opened_at=opened,
                    entry_price=100.0,
                    contracts=1.0,
                    entry_fee_usdt=0.0,
                    entry_reason_codes=("test",),
                )
            ],
            stop_loss=90.0 if direction == "long" else 110.0,
            take_profit=None,
            liquidation_price=50.0 if direction == "long" else 150.0,
            leverage=3.0,
            initial_stop_loss=90.0 if direction == "long" else 110.0,
        )

    long_cost = backtester._funding_cost(position("long"), closed, mark_price=110.0)
    short_cost = backtester._funding_cost(position("short"), closed, mark_price=110.0)

    assert long_cost == pytest.approx(0.033)
    assert short_cost == pytest.approx(-0.033)

    scaled = position("long")
    scaled.lots.append(
        _PositionLot(
            opened_at=pd.Timestamp("2026-01-01 12:00", tz="UTC"),
            entry_price=105.0,
            contracts=1.0,
            entry_fee_usdt=0.0,
            entry_reason_codes=("add",),
        )
    )
    assert backtester._funding_cost(scaled, closed, mark_price=110.0) == pytest.approx(
        0.055
    )


def test_backtest_uses_same_strategy_and_returns_costed_metrics() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    result = PerpetualBacktester(
        strategy=_strategy(),
        mandate=_mandate(),
        instruments={"BTC-USDT-SWAP": _instrument()},
    ).run({"BTC-USDT-SWAP": _bullish_bars(now, 120)})
    assert result.initial_equity_usdt == 10_000
    assert result.trade_count >= 0
    assert result.max_drawdown_pct >= 0
    assert result.to_dict()["final_equity_usdt"] == result.final_equity_usdt


def test_backtest_checks_stop_on_the_entry_bar() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    index = pd.date_range(end=now, periods=2, freq="15min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 94.0],
            "close": [100.0, 96.0],
            "volume": [100.0, 100.0],
            "confirm": ["1", "1"],
        },
        index=index,
    )

    class _OneShotStrategy:
        def evaluate(self, *, instrument_id, bars, run_id, now, position=None):  # noqa: ANN001
            frame = pd.DataFrame(bars)
            opening = len(frame) == 1
            return TradePlan.create(
                strategy_id="one_shot",
                run_id=run_id,
                instrument_id=instrument_id,
                signal_at=now,
                data_as_of=frame.index[-1].to_pydatetime(),
                action=TradeAction.OPEN_LONG if opening else TradeAction.HOLD,
                confidence=1.0,
                reference_price=100.0,
                stop_loss_price=95.0 if opening else None,
                take_profit_price=110.0 if opening else None,
                leverage=3.0,
                risk_budget_usdt=25.0 if opening else 0.0,
                reason_codes=("test",),
                source_bar_count=len(frame),
            )

    result = PerpetualBacktester(
        strategy=_OneShotStrategy(),  # type: ignore[arg-type]
        mandate=_mandate(),
        instruments={"BTC-USDT-SWAP": _instrument()},
    ).run({"BTC-USDT-SWAP": bars})

    assert result.trade_count == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].closed_at == index[-1].isoformat()
    assert result.equity_curve[-1][1] == result.final_equity_usdt


class _DemoConfig:
    is_demo = True


class _ShadowSDK:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars
        self.write_calls = 0
        self.requested_periods: list[str] = []

    def get_account_configuration(self, config):  # noqa: ANN001
        return {"status": "ok", "account_configuration": {"uid": "demo", "position_mode": "net_mode"}}

    def get_account_snapshot(self, config):  # noqa: ANN001
        return {"status": "ok", "account": {"total_equity": "10000"}}

    def get_swap_positions(self, config):  # noqa: ANN001
        return {"status": "ok", "positions": []}

    def get_swap_open_orders(self, config):  # noqa: ANN001
        return {"status": "ok", "open_orders": []}

    def get_swap_fills(self, config):  # noqa: ANN001
        return {"status": "ok", "fills": []}

    def get_swap_algo_orders(self, config):  # noqa: ANN001
        return {"status": "ok", "algo_orders": []}

    def get_quote(self, symbol, **kwargs):  # noqa: ANN001, ANN003
        timestamp = self.bars.index[-1]
        price = float(self.bars.iloc[-1]["close"])
        return {
            "status": "ok",
            "quote": {
                "last": price,
                "ask": price,
                "bid": price,
                "time": str(int(timestamp.timestamp() * 1000)),
            },
        }

    def get_swap_instrument(self, symbol, **kwargs):  # noqa: ANN001, ANN003
        return {"status": "ok", "instrument": _instrument(symbol).to_dict()}

    def get_historical_bars(self, symbol, **kwargs):  # noqa: ANN001, ANN003
        self.requested_periods.append(str(kwargs.get("period")))
        rows = []
        for timestamp, row in self.bars.iterrows():
            rows.append(
                {
                    "time": str(int(timestamp.timestamp() * 1000)),
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "confirm": "1",
                }
            )
        return {"status": "ok", "bars": rows}

    def place_swap_order(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.write_calls += 1
        return {"status": "ok"}


class _StopUpdateSDK(_ShadowSDK):
    def __init__(self, bars: pd.DataFrame) -> None:
        super().__init__(bars)
        self.stop_update_calls: list[dict] = []
        self.position = {
            "symbol": "BTC-USDT-SWAP",
            "quantity": "1",
            "position_side": "net",
            "average_cost": "100",
            "margin_mode": "cross",
            "leverage": "3",
            "notional_usd": "100",
            "margin": "33.33",
        }
        self.algo = {
            "symbol": "BTC-USDT-SWAP",
            "algo_id": "A1",
            "client_algo_order_id": "risk-A1",
            "stop_loss_trigger": "95",
            "state": "live",
        }

    def get_swap_positions(self, config):  # noqa: ANN001
        return {"status": "ok", "positions": [dict(self.position)]}

    def get_swap_algo_orders(self, config):  # noqa: ANN001
        return {"status": "ok", "algo_orders": [dict(self.algo)]}

    def amend_swap_stop_loss(self, config, **kwargs):  # noqa: ANN001, ANN003
        self.write_calls += 1
        self.stop_update_calls.append(kwargs)
        self.algo["stop_loss_trigger"] = kwargs["stop_loss"]
        return {
            "status": "ok",
            "verification": {
                "status": "verified",
                "observed_stop_loss": kwargs["stop_loss"],
            },
            "retry_safe": False,
        }


class _AlwaysTightenStopStrategy:
    def evaluate(self, *, instrument_id, bars, run_id, now, position=None):  # noqa: ANN001
        frame = pd.DataFrame(bars)
        return TradePlan.create(
            strategy_id="stop-update-test",
            run_id=run_id,
            instrument_id=instrument_id,
            signal_at=now,
            data_as_of=pd.to_datetime(
                int(frame["time"].iloc[-1]), unit="ms", utc=True
            ).to_pydatetime(),
            action=TradeAction.UPDATE_STOP,
            confidence=1.0,
            reference_price=float(frame["close"].iloc[-1]),
            stop_loss_price=96.0,
            take_profit_price=None,
            leverage=3.0,
            risk_budget_usdt=0.0,
            reason_codes=("test_trailing_stop",),
            source_bar_count=len(frame),
        )


def _seed_stop_runtime(store: SwapRuntimeStore, sdk: _StopUpdateSDK) -> None:
    store.save(
        {
            "account_configuration": {"uid": "demo", "position_mode": "net_mode"},
            "balance": {"total_equity": "10000"},
            "orders": [],
            "stop_amendments": [],
            "open_orders": [],
            "fills": [],
            "positions": [dict(sdk.position)],
            "algo_orders": [dict(sdk.algo)],
            "risk": {},
        }
    )


def test_shadow_runner_reconciles_and_emits_plan_without_writes(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    sdk = _ShadowSDK(_bullish_bars(now - timedelta(minutes=15)))
    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_strategy(),
        config=CryptoRunnerConfig(
            mode=ExecutionMode.SHADOW,
            instruments=("BTC-USDT-SWAP",),
            bar_period="15m",
            bar_limit=90,
            max_data_age_seconds=60,
            max_quote_age_seconds=20 * 60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: now,
        system_status_reader=lambda: {"status": "ok", "incidents": []},
    )
    result = runner.run_once()
    assert result["outcome"] == "completed"
    assert result["plans"][0]["action"] == "open_long"
    assert result["executions"][0]["status"] == "shadow"
    assert float(result["executions"][0]["sizing"]["notional_usdt"]) > 0
    assert sdk.write_calls == 0
    assert quant_status("okx")["run_id"] == result["run_id"]


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_writes"),
    [
        (ExecutionMode.SHADOW, "shadow", 0),
        (ExecutionMode.DEMO, "ok", 1),
    ],
)
def test_runner_handles_stop_updates_without_opening_another_position(
    runtime: Path,
    mode: ExecutionMode,
    expected_status: str,
    expected_writes: int,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    sdk = _StopUpdateSDK(_bullish_bars(now - timedelta(minutes=15)))
    store = SwapRuntimeStore("okx")
    _seed_stop_runtime(store, sdk)
    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_AlwaysTightenStopStrategy(),
        config=CryptoRunnerConfig(
            mode=mode,
            instruments=("BTC-USDT-SWAP",),
            bar_period="15m",
            bar_limit=90,
            max_data_age_seconds=60,
            max_quote_age_seconds=20 * 60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: now,
        system_status_reader=lambda: {"status": "ok", "incidents": []},
        store=store,
    )

    result = runner.run_once()

    assert result["outcome"] == "completed"
    assert result["plans"][0]["action"] == "update_stop"
    assert result["executions"][0]["status"] == expected_status
    assert sdk.write_calls == expected_writes
    if mode is ExecutionMode.DEMO:
        assert sdk.stop_update_calls[0]["algo_id"] == "A1"
        amendment = store.load(required=True)["stop_amendments"][0]
        assert amendment["status"] == "filled"
        assert amendment["new_stop_loss"] == "96"


def test_demo_runner_trips_breaker_when_stop_update_cannot_be_verified(
    runtime: Path,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    class _UnverifiedStopSDK(_StopUpdateSDK):
        def amend_swap_stop_loss(self, config, **kwargs):  # noqa: ANN001, ANN003
            self.write_calls += 1
            return {
                "status": "ok",
                "verification": {"status": "unknown"},
                "retry_safe": False,
            }

    sdk = _UnverifiedStopSDK(_bullish_bars(now - timedelta(minutes=15)))
    store = SwapRuntimeStore("okx")
    _seed_stop_runtime(store, sdk)
    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_AlwaysTightenStopStrategy(),
        config=CryptoRunnerConfig(
            mode=ExecutionMode.DEMO,
            instruments=("BTC-USDT-SWAP",),
            bar_period="15m",
            bar_limit=90,
            max_data_age_seconds=60,
            max_quote_age_seconds=20 * 60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: now,
        system_status_reader=lambda: {"status": "ok", "incidents": []},
        store=store,
    )

    result = runner.run_once()

    assert result["outcome"] == "completed"
    assert result["executions"][0]["write_state"] == "unknown_needs_reconcile"
    assert swap_breaker_status("okx")["reason"] == "stop amendment outcome unknown"
    assert store.load(required=True)["stop_amendments"][0]["status"] == (
        "unknown_needs_reconcile"
    )


def test_runner_restores_position_protection_and_aligns_trigger_prices() -> None:
    position = _position_view(
        [
            {
                "symbol": "BTC-USDT-SWAP",
                "quantity": "1",
                "position_side": "net",
                "average_cost": "100",
                "leverage": "3",
            }
        ],
        "BTC-USDT-SWAP",
        algo_orders=[
            {
                "symbol": "BTC-USDT-SWAP",
                "stop_loss_trigger": "94.73",
                "take_profit_trigger": "108.26",
            }
        ],
    )

    assert position is not None
    assert position.stop_loss_price == 94.73
    assert position.take_profit_price == 108.26
    assert _align_trigger_price(
        94.73,
        tick_size=Decimal("0.1"),
        reference_price=Decimal("100"),
    ) == Decimal("94.8")
    assert _align_trigger_price(
        108.26,
        tick_size=Decimal("0.1"),
        reference_price=Decimal("100"),
    ) == Decimal("108.2")


def test_shadow_runner_measures_freshness_from_confirmed_bar_close(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    sdk = _ShadowSDK(_bullish_bars(now - timedelta(minutes=22)))
    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_strategy(),
        config=CryptoRunnerConfig(
            mode=ExecutionMode.SHADOW,
            instruments=("BTC-USDT-SWAP",),
            bar_period="15m",
            bar_limit=90,
            max_data_age_seconds=10 * 60,
            max_quote_age_seconds=30 * 60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: now,
        system_status_reader=lambda: {"status": "ok", "incidents": []},
    )

    result = runner.run_once()

    assert result["outcome"] == "completed"
    assert result["plans"][0]["data_as_of"] == (now - timedelta(minutes=22)).isoformat()
    assert sdk.write_calls == 0


def test_shadow_runner_rejects_bar_stale_after_its_close(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    sdk = _ShadowSDK(_bullish_bars(now - timedelta(minutes=26)))
    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_strategy(),
        config=CryptoRunnerConfig(
            mode=ExecutionMode.SHADOW,
            instruments=("BTC-USDT-SWAP",),
            bar_period="15m",
            bar_limit=90,
            max_data_age_seconds=10 * 60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: now,
        system_status_reader=lambda: {"status": "ok", "incidents": []},
    )

    result = runner.run_once()

    assert result["outcome"] == "instrument_error"
    assert result["reason"] == "BTC-USDT-SWAP: BTC-USDT-SWAP confirmed market data is stale"
    assert sdk.write_calls == 0


def test_runner_reads_separate_primary_and_daily_context_bars(runtime: Path) -> None:
    last_open = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)
    bars = _bullish_bars(last_open)
    sdk = _ShadowSDK(bars)

    class _ContextStrategy:
        requires_daily_bars = True

        def evaluate(self, *, instrument_id, bars, daily_bars, run_id, now, position=None):  # noqa: ANN001
            assert len(bars) == len(daily_bars)
            frame = pd.DataFrame(bars)
            return TradePlan.create(
                strategy_id="context-test",
                run_id=run_id,
                instrument_id=instrument_id,
                signal_at=now,
                data_as_of=pd.to_datetime(
                    int(frame["time"].iloc[-1]), unit="ms", utc=True
                ).to_pydatetime(),
                action=TradeAction.HOLD,
                confidence=0.5,
                reference_price=float(frame["close"].iloc[-1]),
                stop_loss_price=None,
                take_profit_price=None,
                leverage=1.0,
                risk_budget_usdt=0.0,
                reason_codes=("test",),
                source_bar_count=len(frame),
            )

    runner = CryptoStrategyRunner(
        sdk_module=sdk,
        sdk_config=_DemoConfig(),
        strategy=_ContextStrategy(),
        config=CryptoRunnerConfig(
            mode=ExecutionMode.SHADOW,
            instruments=("BTC-USDT-SWAP",),
            bar_period="4h",
            bar_limit=90,
            daily_bar_period="1d",
            daily_bar_limit=120,
            max_data_age_seconds=60,
        ),
        mandate_loader=lambda broker: _mandate(),
        clock=lambda: last_open + timedelta(hours=4),
        system_status_reader=lambda: {"status": "ok", "incidents": []},
    )

    result = runner.run_once()

    assert result["outcome"] == "completed"
    assert sdk.requested_periods == ["4h", "1d"]
