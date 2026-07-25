"""HTTP control-plane tests for the OKX BTC/ETH quant runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
import src.live.mandate.store as mandate_store
from src.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    ConsentMeta,
    HardCaps,
    InstrumentType,
    Mandate,
    UniverseConstraint,
)
from src.live.promotion import (
    DemoQualificationPolicy,
    record_demo_event,
    record_resilience_drill,
)
from src.live.runtime.swap_state import (
    SwapReconcileIssue,
    SwapReconcileReport,
    swap_breaker_status,
    trip_swap_breaker,
)
from src.trading.quant.models import ExecutionMode


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path), raising=False)
    monkeypatch.setattr(api_server, "_quant_runners", {}, raising=False)
    monkeypatch.setattr(api_server, "_quant_runner_factory", None, raising=False)
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _mandate(now: datetime | None = None) -> Mandate:
    current = now or datetime.now(timezone.utc)
    return Mandate(
        schema_version=MANDATE_SCHEMA_VERSION,
        hard_caps=HardCaps(
            account_funding_usd=10_000,
            max_order_notional_usd=1_000,
            max_total_exposure_usd=3_000,
            max_leverage=5,
            allowed_instruments=(InstrumentType.PERPETUAL_SWAP,),
            max_trades_per_day=20,
            max_position_notional_usd=1_000,
            max_correlated_exposure_usd=2_000,
            max_daily_loss_usd=100,
            max_drawdown_pct=0.08,
            min_liquidation_buffer_pct=0.15,
            max_margin_utilization_pct=0.40,
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
            created_at=current.isoformat(),
            consent_token_sha256="d" * 64,
            broker="okx",
            account_ref="dedicated-demo",
            expires_at=(current + timedelta(days=30)).isoformat(),
        ),
    )


def _active_mandate_state() -> api_server.ActiveMandateState:
    return api_server.ActiveMandateState(
        broker="okx",
        account_ref="dedicated-demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        expires_in_seconds=30 * 86_400,
        expired=False,
        limits=api_server.MandateLimits(
            max_order_notional_usd=1_000,
            max_total_exposure_usd=3_000,
            max_leverage=5,
            max_trades_per_day=20,
            allowed_instruments=["perpetual_swap"],
            account_funding_usd=10_000,
        ),
    )


def _install_mandate(monkeypatch, mandate: Mandate | None) -> None:
    monkeypatch.setattr(mandate_store, "load_mandate", lambda broker: mandate)
    monkeypatch.setattr(
        api_server,
        "_active_mandate_state",
        lambda broker: _active_mandate_state() if mandate is not None else None,
    )


def test_quant_status_is_read_only_and_live_execution_is_compiled_off(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get("/quant/okx/status")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "okx"
    assert body["environment"] == "demo"
    assert body["runner"]["running"] is False
    assert body["breaker"]["tripped"] is False
    assert body["promotion"]["execution_enabled"] is False
    assert body["live_swap_writes_compiled"] is False


def test_quant_runner_start_requires_active_perpetual_mandate(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    _install_mandate(monkeypatch, None)

    response = client.post("/quant/okx/runner/start", json={"mode": "shadow"})

    assert response.status_code == 409
    assert "mandate" in response.json()["detail"].lower()


def test_shadow_runner_start_is_idempotent_and_stop_is_clean(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    _install_mandate(monkeypatch, _mandate())
    calls = {"start": 0, "stop": 0}

    class _Runner:
        config = SimpleNamespace(mode=ExecutionMode.SHADOW, tick_interval_seconds=30)

        def start(self) -> None:
            calls["start"] += 1

        async def stop(self) -> None:
            calls["stop"] += 1

    monkeypatch.setattr(api_server, "_quant_runner_factory", lambda mode, interval: _Runner())

    first = client.post(
        "/quant/okx/runner/start",
        json={"mode": "shadow", "tick_interval_seconds": 30},
    )
    second = client.post(
        "/quant/okx/runner/start",
        json={"mode": "demo", "tick_interval_seconds": 60},
    )
    stopped = client.post("/quant/okx/runner/stop")

    assert first.status_code == 200 and first.json()["started"] is True
    assert second.status_code == 200 and second.json()["already_running"] is True
    assert second.json()["mode"] == "shadow"
    assert stopped.json() == {"broker": "okx", "stopped": True, "was_running": True}
    assert calls == {"start": 1, "stop": 1}


def test_breaker_clear_requires_fresh_safe_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    trip_swap_breaker("okx", "test unknown order")
    runner = SimpleNamespace(config=SimpleNamespace(broker="okx"))
    monkeypatch.setattr(api_server, "_quant_runner_factory", lambda mode, interval: runner)
    reconciles: list[object] = []

    def _safe_reconcile(candidate):
        reconciles.append(candidate)
        return SwapReconcileReport(
            broker="okx",
            ts=datetime.now(timezone.utc).isoformat(),
            issues=(),
            state_persisted=True,
            had_prior_state=True,
        )

    monkeypatch.setattr(api_server, "_run_quant_reconcile", _safe_reconcile)

    response = client.post(
        "/quant/okx/breaker/clear",
        json={"authorized_by": "operator", "note": "manual reconciliation reviewed"},
    )

    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert response.json()["reconcile"]["is_safe"] is True
    assert reconciles == [runner]
    assert swap_breaker_status("okx")["tripped"] is False


def test_unsafe_reconciliation_keeps_breaker_tripped(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    trip_swap_breaker("okx", "test unknown order")
    monkeypatch.setattr(
        api_server,
        "_quant_runner_factory",
        lambda mode, interval: SimpleNamespace(config=SimpleNamespace(broker="okx")),
    )
    issue = SwapReconcileIssue(
        kind="unknown_order",
        subject="order",
        identity="cl-1",
        detail="order outcome is unknown",
    )
    monkeypatch.setattr(
        api_server,
        "_run_quant_reconcile",
        lambda runner: SwapReconcileReport(
            broker="okx",
            ts=datetime.now(timezone.utc).isoformat(),
            issues=(issue,),
            state_persisted=False,
            had_prior_state=True,
        ),
    )

    response = client.post(
        "/quant/okx/breaker/clear",
        json={"authorized_by": "operator", "note": "attempted review"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reconcile"]["issues"][0]["kind"] == "unknown_order"
    assert swap_breaker_status("okx")["tripped"] is True


def test_resilience_drill_endpoint_records_evidence_without_granting_eligibility(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/quant/okx/drills",
        json={
            "drill": "network_timeout",
            "passed": True,
            "evidence_ref": "runbook://network-timeout/2026-07-25",
            "operator": "operator",
        },
    )

    assert response.status_code == 200
    assert response.json()["recorded"] is True
    assert response.json()["qualification"]["eligible"] is False


def test_promotion_approval_cannot_bypass_thirty_day_demo_gate(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    _install_mandate(monkeypatch, _mandate())

    response = client.post(
        "/quant/okx/promotion/approve",
        json={
            "approver": "operator",
            "dedicated_subaccount": True,
            "ip_whitelist_enabled": True,
            "withdraw_permission_disabled": True,
            "expected_uid": "demo-subaccount-uid",
            "max_live_notional_usdt": 500,
        },
    )

    assert response.status_code == 409
    assert "30" in response.json()["detail"]


def test_qualified_manual_approval_still_does_not_enable_live_execution(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    mandate = _mandate(now)
    _install_mandate(monkeypatch, mandate)
    start = now - timedelta(days=30)
    for day in range(31):
        ts = start + timedelta(days=day)
        record_demo_event(
            "okx",
            "tick_checkpoint",
            {"count": 35, "healthy": True},
            clock=lambda ts=ts: ts,
        )
    for drill in DemoQualificationPolicy().required_drills:
        record_resilience_drill(
            "okx",
            drill,
            passed=True,
            evidence_ref=f"runbook://{drill}",
            operator="operator",
            clock=lambda: now,
        )

    response = client.post(
        "/quant/okx/promotion/approve",
        json={
            "approver": "operator",
            "dedicated_subaccount": True,
            "ip_whitelist_enabled": True,
            "withdraw_permission_disabled": True,
            "expected_uid": "demo-subaccount-uid",
            "max_live_notional_usdt": 500,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["execution_enabled"] is False
    assert body["promotion"]["qualified"] is True
    assert body["promotion"]["manually_approved"] is True
    assert body["promotion"]["execution_enabled"] is False
