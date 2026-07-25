"""Phase 4 qualification/approval tests; real swap execution stays disabled."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from src.live.promotion import (
    DemoQualificationPolicy,
    LivePromotionGate,
    QualificationError,
    approve_live_promotion,
    evaluate_demo_qualification,
    qualification_ledger_path,
    record_demo_event,
    record_resilience_drill,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(live_paths, "get_runtime_root", lambda: tmp_path)
    return tmp_path


def _mandate(now: datetime) -> Mandate:
    return Mandate(
        schema_version=MANDATE_SCHEMA_VERSION,
        hard_caps=HardCaps(
            account_funding_usd=1_000,
            max_order_notional_usd=100,
            max_total_exposure_usd=300,
            max_leverage=3,
            allowed_instruments=(InstrumentType.PERPETUAL_SWAP,),
            max_trades_per_day=10,
            max_position_notional_usd=150,
            max_correlated_exposure_usd=250,
            max_daily_loss_usd=20,
            max_drawdown_pct=0.05,
            min_liquidation_buffer_pct=0.15,
            max_margin_utilization_pct=0.30,
            max_consecutive_losses=2,
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
            consent_token_sha256="c" * 64,
            broker="okx",
            account_ref="dedicated-subaccount",
            expires_at=(now + timedelta(days=30)).isoformat(),
        ),
    )


def _record_qualified_window(now: datetime) -> None:
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
            evidence_ref=f"evidence:{drill}",
            operator="human-operator",
            clock=lambda: now,
        )


def test_qualification_requires_real_30_day_active_window(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    for day in range(30):
        ts = now - timedelta(days=29 - day)
        record_demo_event(
            "okx",
            "tick_checkpoint",
            {"count": 40, "healthy": True},
            clock=lambda ts=ts: ts,
        )
    result = evaluate_demo_qualification("okx", now=now)
    assert result.eligible is False
    assert result.elapsed_days < 30
    assert any("30 elapsed" in reason for reason in result.reasons)


def test_policy_cannot_lower_nonnegotiable_baseline(runtime: Path) -> None:
    with pytest.raises(QualificationError, match="never bypass"):
        evaluate_demo_qualification(
            "okx",
            policy=DemoQualificationPolicy(minimum_elapsed_days=1),
        )


def test_blocked_ticks_do_not_count_toward_demo_qualification(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(days=30)
    for day in range(31):
        ts = start + timedelta(days=day)
        record_demo_event(
            "okx",
            "tick",
            {"count": 100, "outcome": "blocked"},
            clock=lambda ts=ts: ts,
        )
    result = evaluate_demo_qualification("okx", now=now)
    assert result.tick_count == 0
    assert result.active_days == 0
    assert result.eligible is False


def test_full_window_and_drills_become_eligible(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    _record_qualified_window(now)
    result = evaluate_demo_qualification("okx", now=now)
    assert result.eligible is True
    assert result.elapsed_days == pytest.approx(30.0)
    assert result.active_days == 31
    assert result.tick_count == 1085
    assert result.missing_drills == ()


def test_any_unknown_order_disqualifies_window(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    _record_qualified_window(now)
    record_demo_event("okx", "unknown_order", {"client_order_id": "x"}, clock=lambda: now)
    result = evaluate_demo_qualification("okx", now=now)
    assert not result.eligible
    assert "unknown_order" in result.disqualifying_events


def test_manual_approval_still_cannot_enable_live_execution(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    _record_qualified_window(now)
    mandate = _mandate(now)
    approval = approve_live_promotion(
        "okx",
        mandate=mandate,
        approver="human-operator",
        dedicated_subaccount=True,
        ip_whitelist_enabled=True,
        withdraw_permission_disabled=True,
        expected_uid="uid-dedicated",
        max_live_notional_usdt=50,
        clock=lambda: now,
    )
    assert approval["execution_enabled"] is False
    decision = LivePromotionGate().evaluate(broker="okx", mandate=mandate, now=now)
    assert decision.qualified and decision.manually_approved
    assert decision.execution_enabled is False
    assert any("writes disabled" in reason for reason in decision.reasons)
    with pytest.raises(QualificationError, match="execution denied"):
        LivePromotionGate().require_execution_enabled(broker="okx", mandate=mandate)


def test_manual_approval_rejects_expired_mandate(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    _record_qualified_window(now)
    mandate = _mandate(now)
    mandate = replace(
        mandate,
        consent=replace(mandate.consent, expires_at=(now - timedelta(seconds=1)).isoformat()),
    )
    with pytest.raises(QualificationError, match="expired"):
        approve_live_promotion(
            "okx",
            mandate=mandate,
            approver="human-operator",
            dedicated_subaccount=True,
            ip_whitelist_enabled=True,
            withdraw_permission_disabled=True,
            expected_uid="uid-dedicated",
            max_live_notional_usdt=50,
            clock=lambda: now,
        )


def test_manual_approval_uses_the_tightest_notional_cap(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    _record_qualified_window(now)
    mandate = _mandate(now)
    mandate = replace(
        mandate,
        hard_caps=replace(
            mandate.hard_caps,
            max_order_notional_usd=200,
            max_position_notional_usd=40,
        ),
    )
    with pytest.raises(QualificationError, match="every mandate notional cap"):
        approve_live_promotion(
            "okx",
            mandate=mandate,
            approver="human-operator",
            dedicated_subaccount=True,
            ip_whitelist_enabled=True,
            withdraw_permission_disabled=True,
            expected_uid="uid-dedicated",
            max_live_notional_usdt=50,
            clock=lambda: now,
        )


def test_ledger_tampering_fails_closed(runtime: Path) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    record_demo_event("okx", "tick", {"count": 1}, clock=lambda: now)
    path = qualification_ledger_path("okx")
    path.write_text(path.read_text(encoding="utf-8").replace('"count": 1', '"count": 9'), encoding="utf-8")
    with pytest.raises(QualificationError, match="hash chain"):
        evaluate_demo_qualification("okx", now=now)
