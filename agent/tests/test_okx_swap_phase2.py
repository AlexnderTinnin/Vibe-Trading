"""Phase 2 derivative mandate, durable state, reconcile, and flatten tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.live.paths as live_paths
from src.live.enforcement import DerivativeRiskSnapshot, OrderIntent, check_mandate
from src.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    ConsentMeta,
    HardCaps,
    InstrumentType,
    Mandate,
    UniverseConstraint,
)
from src.live.runtime.flatten import flatten_okx_swaps
from src.live.runtime.swap_state import (
    SwapOrderStatus,
    SwapRuntimeStore,
    reconcile_swap,
    swap_breaker_status,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(live_paths, "get_runtime_root", lambda: tmp_path)
    return tmp_path


def _mandate(**caps_overrides) -> Mandate:
    caps = {
        "account_funding_usd": 10_000.0,
        "max_order_notional_usd": 2_000.0,
        "max_total_exposure_usd": 5_000.0,
        "max_leverage": 10.0,
        "allowed_instruments": (InstrumentType.PERPETUAL_SWAP,),
        "max_trades_per_day": 20,
        "max_position_notional_usd": 3_000.0,
        "max_correlated_exposure_usd": 4_000.0,
        "max_daily_loss_usd": 200.0,
        "max_drawdown_pct": 0.10,
        "min_liquidation_buffer_pct": 0.08,
        "max_margin_utilization_pct": 0.50,
        "max_consecutive_losses": 3,
        "require_stop_loss": True,
    }
    caps.update(caps_overrides)
    now = datetime.now(timezone.utc)
    return Mandate(
        schema_version=MANDATE_SCHEMA_VERSION,
        hard_caps=HardCaps(**caps),
        universe=UniverseConstraint(
            asset_classes=(AssetClass.CRYPTO,),
            min_market_cap_usd=None,
            min_avg_daily_volume_usd=None,
            exclude_symbols=(),
            include_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        ),
        consent=ConsentMeta(
            created_at=now.isoformat(),
            consent_token_sha256="a" * 64,
            broker="okx",
            account_ref="demo",
            expires_at=(now + timedelta(days=30)).isoformat(),
        ),
    )


def _intent(**overrides) -> OrderIntent:
    values = {
        "symbol": "BTC-USDT-SWAP",
        "side": "buy",
        "notional_usd": None,
        "quantity": 1.0,
        "instrument_type": InstrumentType.PERPETUAL_SWAP,
        "asset_class": AssetClass.CRYPTO,
        "contract_value": 0.01,
        "reference_price": 100_000.0,
        "leverage": 10.0,
        "reduce_only": False,
        "position_side": "long",
        "margin_mode": "cross",
        "stop_loss_price": 98_000.0,
        "liquidation_price": 90_000.0,
        "client_order_id": "run-btc-1",
    }
    values.update(overrides)
    return OrderIntent(**values)


def _risk(**overrides) -> DerivativeRiskSnapshot:
    values = {
        "equity_usd": 10_000.0,
        "peak_equity_usd": 10_000.0,
        "daily_realized_pnl_usd": 0.0,
        "correlated_exposure_usd": 0.0,
        "margin_used_usd": 0.0,
        "consecutive_losses": 0,
    }
    values.update(overrides)
    return DerivativeRiskSnapshot(**values)


def _check(intent: OrderIntent, *, mandate: Mandate | None = None, positions=None, risk=None):
    return check_mandate(
        mandate or _mandate(),
        intent,
        [] if positions is None else positions,
        {"equity": 10_000},
        broker="okx",
        remote_tool="place_swap_order",
        daily_count=0,
        derivative_risk=risk or _risk(),
    )


def test_contract_notional_uses_contract_value_not_coin_quantity() -> None:
    assert _check(_intent()) is None  # 1 contract * 0.01 BTC * 100k = 1,000 USDT
    breach = _check(_intent(quantity=3.0))
    assert breach is not None and breach.limit == "max_order_notional_usd"
    assert breach.attempted_value == 3_000.0


def test_reduce_only_bypasses_open_caps_but_cannot_exceed_exact_side() -> None:
    position = {
        "symbol": "BTC-USDT-SWAP",
        "side": "long",
        "margin_mode": "cross",
        "quantity": "2",
        "notional_usd": "2000",
    }
    reducing = _intent(side="sell", reduce_only=True, quantity=1.0, stop_loss_price=None)
    assert _check(reducing, positions=[position], risk=_risk(daily_realized_pnl_usd=-999)) is None
    breach = _check(_intent(side="sell", reduce_only=True, quantity=3.0), positions=[position])
    assert breach is not None and breach.limit == "reduce_only"


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (_risk(daily_realized_pnl_usd=-200.0), "max_daily_loss_usd"),
        (_risk(equity_usd=8_900.0), "max_drawdown_pct"),
        (_risk(consecutive_losses=3), "max_consecutive_losses"),
    ],
)
def test_derivative_breaker_limits(risk: DerivativeRiskSnapshot, expected: str) -> None:
    breach = _check(_intent(), risk=risk)
    assert breach is not None and breach.limit == expected


def test_liquidation_buffer_and_stop_are_required() -> None:
    breach = _check(_intent(stop_loss_price=None))
    assert breach is not None and breach.limit == "require_stop_loss"
    breach = _check(_intent(liquidation_price=94_000.0))
    assert breach is not None and breach.limit == "min_liquidation_buffer_pct"


def test_unknown_write_is_durable_and_trips_breaker(runtime: Path) -> None:
    store = SwapRuntimeStore("okx")
    store.stage_order({"client_order_id": "cid-1", "symbol": "BTC-USDT-SWAP"})
    tracked = store.record_order_result(
        "cid-1",
        {
            "status": "error",
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
        },
    )
    assert tracked["status"] == SwapOrderStatus.UNKNOWN_NEEDS_RECONCILE.value
    assert swap_breaker_status("okx")["tripped"] is True
    with pytest.raises(Exception, match="already exists"):
        store.stage_order({"client_order_id": "cid-1"})


def test_verified_stop_amendment_is_durable(runtime: Path) -> None:
    store = SwapRuntimeStore("okx")
    store.stage_stop_amendment(
        {
            "request_id": "stop-1",
            "algo_id": "A1",
            "symbol": "BTC-USDT-SWAP",
            "old_stop_loss": "95000",
            "new_stop_loss": "96000",
        }
    )

    tracked = store.record_stop_amendment_result(
        "stop-1",
        {"status": "ok", "verification": {"status": "verified"}},
    )

    assert tracked["status"] == SwapOrderStatus.FILLED.value
    assert store.load(required=True)["stop_amendments"][0]["request_id"] == "stop-1"


def test_reconcile_resolves_unknown_stop_amendment_from_broker_state(runtime: Path) -> None:
    store = SwapRuntimeStore("okx")
    position = {
        "symbol": "BTC-USDT-SWAP",
        "position_side": "net",
        "margin_mode": "cross",
        "quantity": "1",
    }
    algo = {
        "symbol": "BTC-USDT-SWAP",
        "algo_id": "A1",
        "stop_loss_trigger": "95000",
    }
    store.save(
        {
            "account_configuration": {"uid": "demo", "position_mode": "net_mode"},
            "balance": {"total_equity": "10000"},
            "orders": [],
            "stop_amendments": [],
            "open_orders": [],
            "fills": [],
            "positions": [position],
            "algo_orders": [algo],
            "risk": {},
        }
    )
    store.stage_stop_amendment(
        {
            "request_id": "stop-unknown",
            "algo_id": "A1",
            "symbol": "BTC-USDT-SWAP",
            "old_stop_loss": "95000",
            "new_stop_loss": "96000",
        }
    )
    store.record_stop_amendment_result(
        "stop-unknown",
        {
            "status": "error",
            "write_state": "unknown_needs_reconcile",
            "retry_safe": False,
        },
    )
    reads = {
        "read_account_configuration": lambda: {
            "account_configuration": {"uid": "demo", "position_mode": "net_mode"}
        },
        "read_balance": lambda: {"account": {"total_equity": "10000"}},
        "read_positions": lambda: {"positions": [position]},
        "read_open_orders": lambda: {"open_orders": []},
        "read_fills": lambda: {"fills": []},
        "read_algo_orders": lambda: {
            "algo_orders": [{**algo, "stop_loss_trigger": "96000"}]
        },
    }

    report = reconcile_swap("okx", **reads)

    assert report.is_safe
    assert store.load(required=True)["stop_amendments"][0]["status"] == "filled"


def _empty_reads():
    return {
        "read_account_configuration": lambda: {"account_configuration": {"uid": "demo", "position_mode": "net_mode"}},
        "read_balance": lambda: {"account": {"total_equity": "10000"}},
        "read_positions": lambda: {"positions": []},
        "read_open_orders": lambda: {"open_orders": []},
        "read_fills": lambda: {"fills": []},
        "read_algo_orders": lambda: {"algo_orders": []},
    }


def test_swap_reconcile_persists_clean_composite_baseline(runtime: Path) -> None:
    report = reconcile_swap("okx", **_empty_reads())
    assert report.is_safe and report.state_persisted and not report.had_prior_state
    second = reconcile_swap("okx", **_empty_reads())
    assert second.is_safe and second.had_prior_state


def test_swap_reconcile_halts_on_unknown_broker_order(runtime: Path) -> None:
    reconcile_swap("okx", **_empty_reads())
    reads = _empty_reads()
    reads["read_open_orders"] = lambda: {
        "open_orders": [{"symbol": "BTC-USDT-SWAP", "order_id": "foreign", "client_order_id": "x"}]
    }
    report = reconcile_swap("okx", **reads)
    assert report.requires_halt and report.issues[0].kind == "unknown_order"
    assert swap_breaker_status("okx")["tripped"] is True


def test_swap_reconcile_records_the_failed_read_surface(runtime: Path) -> None:
    reads = _empty_reads()
    reads["read_positions"] = lambda: {"status": "error", "error": "transient EOF"}

    with pytest.raises(Exception, match="transient EOF"):
        reconcile_swap("okx", **reads)

    breaker = swap_breaker_status("okx")
    assert breaker["reason"] == "swap reconciliation read/state failure"
    assert breaker["context"]["surface"] == "positions"
    assert breaker["context"]["error"] == "transient EOF"


def test_contract_flatten_uses_position_side_and_margin_mode(runtime: Path) -> None:
    cancellations: list[dict] = []
    closes: list[dict] = []

    report = flatten_okx_swaps(
        "okx",
        lambda request: cancellations.append(request) or {"status": "ok"},
        lambda request: closes.append(request) or {"status": "ok"},
        lambda: [
            {
                "symbol": "BTC-USDT-SWAP",
                "side": "short",
                "margin_mode": "isolated",
                "quantity": "2",
                "leverage": "5",
            }
        ],
        lambda: [{"symbol": "BTC-USDT-SWAP", "order_id": "o1"}],
        allow_flatten=True,
    )
    assert report["errors"] == []
    assert cancellations == [{"action": "cancel", "symbol": "BTC-USDT-SWAP", "order_id": "o1"}]
    assert closes[0]["position_side"] == "short"
    assert closes[0]["margin_mode"] == "isolated"
    assert closes[0]["action"] == "close_swap_position"
