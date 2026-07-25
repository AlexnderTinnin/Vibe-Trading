"""Tamper-evident Demo qualification and manual Live promotion control plane.

This module can establish eligibility and record approval. It intentionally
cannot enable a real OKX perpetual mutation: ``LIVE_SWAP_WRITES_COMPILED`` is a
constant false in this release and no environment variable overrides it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.live.mandate.model import MANDATE_SCHEMA_VERSION, InstrumentType, Mandate
from src.live.paths import broker_dir

QUALIFICATION_SCHEMA_VERSION = 1
_LEDGER_FILENAME = "demo_qualification.jsonl"
_APPROVAL_FILENAME = "live_promotion_approval.json"

# Deliberately not configurable. A later audited release must change code and
# tests to open live perpetual writes after the promotion control plane proves
# itself. Eligibility in this release never equals execution authority.
LIVE_SWAP_WRITES_COMPILED = False
_LEDGER_LOCK = threading.RLock()

REQUIRED_RESILIENCE_DRILLS = frozenset(
    {
        "network_timeout",
        "process_restart",
        "api_rate_limit",
        "partial_fill",
        "websocket_disconnect",
    }
)
DISQUALIFYING_EVENTS = frozenset(
    {
        "unknown_order",
        "duplicate_order",
        "unexplained_position",
        "reconcile_unsafe",
        "missing_protection",
        "risk_limit_breach",
    }
)


class QualificationError(RuntimeError):
    """Raised when qualification/approval state is invalid or untrusted."""


@dataclass(frozen=True)
class DemoQualificationPolicy:
    minimum_elapsed_days: int = 30
    minimum_active_days: int = 30
    minimum_ticks: int = 1_000
    maximum_last_tick_age_hours: int = 24
    required_drills: frozenset[str] = REQUIRED_RESILIENCE_DRILLS


@dataclass(frozen=True)
class DemoQualification:
    eligible: bool
    broker: str
    window_started_at: str | None
    elapsed_days: float
    active_days: int
    tick_count: int
    last_tick_at: str | None
    passed_drills: tuple[str, ...]
    missing_drills: tuple[str, ...]
    disqualifying_events: tuple[str, ...]
    reasons: tuple[str, ...]
    ledger_head: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("passed_drills", "missing_drills", "disqualifying_events", "reasons"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class PromotionDecision:
    broker: str
    qualified: bool
    manually_approved: bool
    execution_enabled: bool
    reasons: tuple[str, ...]
    qualification: DemoQualification
    approval: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "qualified": self.qualified,
            "manually_approved": self.manually_approved,
            "execution_enabled": self.execution_enabled,
            "reasons": list(self.reasons),
            "qualification": self.qualification.to_dict(),
            "approval": dict(self.approval) if self.approval else None,
        }


def qualification_ledger_path(broker: str = "okx") -> Path:
    return broker_dir(broker) / _LEDGER_FILENAME


def _approval_path(broker: str) -> Path:
    return broker_dir(broker) / _APPROVAL_FILENAME


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _event_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _read_events_unlocked(broker: str) -> list[dict[str, Any]]:
    path = qualification_ledger_path(broker)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    previous: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QualificationError("qualification ledger is unreadable") from exc
    for index, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise QualificationError(f"qualification ledger line {index} is invalid JSON") from exc
        if not isinstance(event, dict):
            raise QualificationError(f"qualification ledger line {index} is not an object")
        digest = str(event.get("hash") or "")
        body = {key: value for key, value in event.items() if key != "hash"}
        if event.get("previous_hash") != previous or digest != _event_hash(body):
            raise QualificationError(f"qualification ledger hash chain failed at line {index}")
        if event.get("schema_version") != QUALIFICATION_SCHEMA_VERSION:
            raise QualificationError("qualification ledger schema is unsupported")
        previous = digest
        events.append(event)
    return events


def _read_events(broker: str) -> list[dict[str, Any]]:
    with _LEDGER_LOCK:
        return _read_events_unlocked(broker)


def record_demo_event(
    broker: str,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> dict[str, Any]:
    """Append one internal Demo observation to the hash-chained ledger."""
    key = broker.strip().lower()
    clean_kind = kind.strip().lower()
    if not clean_kind:
        raise ValueError("qualification event kind is required")
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    with _LEDGER_LOCK:
        events = _read_events_unlocked(key)
        previous = str(events[-1]["hash"]) if events else None
        body: dict[str, Any] = {
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "event_id": f"dq_{uuid.uuid4().hex}",
            "broker": key,
            "environment": "demo",
            "kind": clean_kind,
            "ts": now.isoformat(),
            "payload": dict(payload or {}),
            "previous_hash": previous,
        }
        event = {**body, "hash": _event_hash(body)}
        path = qualification_ledger_path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return event


def record_resilience_drill(
    broker: str,
    drill: str,
    *,
    passed: bool,
    evidence_ref: str,
    operator: str,
    clock: Callable[[], datetime] = _utcnow,
) -> dict[str, Any]:
    """Privileged surface record for a completed fault-injection drill."""
    clean = drill.strip().lower()
    if clean not in REQUIRED_RESILIENCE_DRILLS:
        raise ValueError(f"unknown resilience drill: {clean}")
    if not evidence_ref.strip() or not operator.strip():
        raise ValueError("drill evidence_ref and operator are required")
    return record_demo_event(
        broker,
        "resilience_drill",
        {
            "drill": clean,
            "passed": bool(passed),
            "evidence_ref": evidence_ref,
            "operator": operator,
        },
        clock=clock,
    )


def evaluate_demo_qualification(
    broker: str = "okx",
    *,
    policy: DemoQualificationPolicy | None = None,
    now: datetime | None = None,
) -> DemoQualification:
    """Evaluate the latest uninterrupted Demo window without bypasses."""
    baseline = DemoQualificationPolicy()
    rules = policy or baseline
    if (
        rules.minimum_elapsed_days < baseline.minimum_elapsed_days
        or rules.minimum_active_days < baseline.minimum_active_days
        or rules.minimum_ticks < baseline.minimum_ticks
        or not baseline.required_drills.issubset(rules.required_drills)
    ):
        raise QualificationError("qualification policy may tighten but never bypass the 30-day baseline")
    current = now or _utcnow()
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    events = _read_events(broker)
    reset_index = max(
        (index for index, event in enumerate(events) if event.get("kind") == "qualification_reset"),
        default=-1,
    )
    window = events[reset_index + 1 :]
    demo_events = [event for event in window if event.get("environment") == "demo"]
    started = _parse_time(demo_events[0]["ts"]) if demo_events else None
    elapsed_days = max(0.0, (current - started).total_seconds() / 86_400.0) if started else 0.0
    tick_events = [
        event
        for event in demo_events
        if (
            event.get("kind") == "tick"
            and (event.get("payload") or {}).get("outcome") == "completed"
        )
        or (
            event.get("kind") == "tick_checkpoint"
            and (event.get("payload") or {}).get("healthy") is True
        )
    ]
    tick_count = sum(
        max(0, int((event.get("payload") or {}).get("count", 1)))
        for event in tick_events
    )
    last_tick = _parse_time(tick_events[-1]["ts"]) if tick_events else None
    active_days = len({_parse_time(event["ts"]).date() for event in tick_events})
    passed_drills = {
        str((event.get("payload") or {}).get("drill") or "")
        for event in demo_events
        if event.get("kind") == "resilience_drill"
        and (event.get("payload") or {}).get("passed") is True
    } & set(rules.required_drills)
    disqualifying = tuple(
        str(event.get("kind"))
        for event in demo_events
        if event.get("kind") in DISQUALIFYING_EVENTS
    )
    missing = tuple(sorted(set(rules.required_drills) - passed_drills))
    reasons: list[str] = []
    if elapsed_days < rules.minimum_elapsed_days:
        reasons.append(f"requires {rules.minimum_elapsed_days} elapsed Demo days")
    if active_days < rules.minimum_active_days:
        reasons.append(f"requires activity on {rules.minimum_active_days} UTC days")
    if tick_count < rules.minimum_ticks:
        reasons.append(f"requires at least {rules.minimum_ticks} Demo ticks")
    if last_tick is None or current - last_tick > timedelta(hours=rules.maximum_last_tick_age_hours):
        reasons.append("latest Demo tick is stale or missing")
    if missing:
        reasons.append("required resilience drills are incomplete")
    if disqualifying:
        reasons.append("Demo window contains disqualifying safety events")
    head = str(events[-1]["hash"]) if events else None
    return DemoQualification(
        eligible=not reasons,
        broker=broker.strip().lower(),
        window_started_at=started.isoformat() if started else None,
        elapsed_days=elapsed_days,
        active_days=active_days,
        tick_count=tick_count,
        last_tick_at=last_tick.isoformat() if last_tick else None,
        passed_drills=tuple(sorted(passed_drills)),
        missing_drills=missing,
        disqualifying_events=disqualifying,
        reasons=tuple(reasons),
        ledger_head=head,
    )


def approve_live_promotion(
    broker: str,
    *,
    mandate: Mandate,
    approver: str,
    dedicated_subaccount: bool,
    ip_whitelist_enabled: bool,
    withdraw_permission_disabled: bool,
    expected_uid: str,
    max_live_notional_usdt: float,
    clock: Callable[[], datetime] = _utcnow,
) -> dict[str, Any]:
    """Record human approval only after the non-negotiable Demo gate passes."""
    now = clock()
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    qualified = evaluate_demo_qualification(broker, now=now)
    if not qualified.eligible:
        raise QualificationError("Demo qualification is not eligible: " + "; ".join(qualified.reasons))
    mandate_reasons = _mandate_promotion_reasons(broker, mandate, now)
    if mandate_reasons:
        raise QualificationError("live approval mandate is invalid: " + "; ".join(mandate_reasons))
    if not approver.strip() or not expected_uid.strip():
        raise QualificationError("approver and expected_uid are required")
    if not (dedicated_subaccount and ip_whitelist_enabled and withdraw_permission_disabled):
        raise QualificationError("dedicated subaccount, IP whitelist, and disabled withdrawal are mandatory")
    live_notional_cap = min(
        mandate.hard_caps.max_order_notional_usd,
        mandate.hard_caps.max_position_notional_usd,
        mandate.hard_caps.max_total_exposure_usd,
        mandate.hard_caps.max_correlated_exposure_usd,
    )
    if max_live_notional_usdt <= 0 or max_live_notional_usdt > live_notional_cap:
        raise QualificationError("live notional must be positive and within every mandate notional cap")
    approval = {
        "schema_version": 1,
        "approval_id": f"lpa_{uuid.uuid4().hex}",
        "broker": broker.strip().lower(),
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "approver": approver,
        "mandate_consent_ref": mandate.consent.consent_token_sha256,
        "qualification_ledger_head": qualified.ledger_head,
        "expected_uid": expected_uid,
        "dedicated_subaccount": True,
        "ip_whitelist_enabled": True,
        "withdraw_permission_disabled": True,
        "max_live_notional_usdt": float(max_live_notional_usdt),
        "execution_enabled": False,
    }
    approval["approval_sha256"] = _event_hash(approval)
    _atomic_write(_approval_path(broker), approval)
    return approval


def load_live_approval(broker: str = "okx") -> dict[str, Any] | None:
    path = _approval_path(broker)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QualificationError("live promotion approval is unreadable") from exc
    if not isinstance(raw, dict):
        raise QualificationError("live promotion approval is malformed")
    digest = str(raw.get("approval_sha256") or "")
    body = {key: value for key, value in raw.items() if key != "approval_sha256"}
    if digest != _event_hash(body):
        raise QualificationError("live promotion approval integrity check failed")
    return raw


class LivePromotionGate:
    """Evaluate eligibility and approval while keeping execution compiled off."""

    def evaluate(
        self,
        *,
        broker: str,
        mandate: Mandate,
        now: datetime | None = None,
    ) -> PromotionDecision:
        current = now or _utcnow()
        current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
        qualification = evaluate_demo_qualification(broker, now=current)
        approval = load_live_approval(broker)
        reasons = list(qualification.reasons)
        mandate_reasons = _mandate_promotion_reasons(broker, mandate, current)
        reasons.extend(mandate_reasons)
        approved = approval is not None
        if mandate_reasons:
            approved = False
        if approval is None:
            reasons.append("manual live promotion approval is missing")
        else:
            if _parse_time(str(approval["expires_at"])) <= current:
                approved = False
                reasons.append("manual live promotion approval has expired")
            if approval.get("mandate_consent_ref") != mandate.consent.consent_token_sha256:
                approved = False
                reasons.append("approval does not bind to the active mandate")
            if approval.get("qualification_ledger_head") != qualification.ledger_head:
                approved = False
                reasons.append("qualification ledger advanced after approval; re-approval required")
        if not LIVE_SWAP_WRITES_COMPILED:
            reasons.append("this build keeps real perpetual SDK writes disabled")
        enabled = bool(qualification.eligible and approved and LIVE_SWAP_WRITES_COMPILED)
        return PromotionDecision(
            broker=broker.strip().lower(),
            qualified=qualification.eligible,
            manually_approved=approved,
            execution_enabled=enabled,
            reasons=tuple(dict.fromkeys(reasons)),
            qualification=qualification,
            approval=approval,
        )

    def require_execution_enabled(self, *, broker: str, mandate: Mandate) -> None:
        decision = self.evaluate(broker=broker, mandate=mandate)
        if not decision.execution_enabled:
            raise QualificationError("live perpetual execution denied: " + "; ".join(decision.reasons))


def _mandate_promotion_reasons(
    broker: str,
    mandate: Mandate,
    now: datetime,
) -> list[str]:
    reasons: list[str] = []
    if mandate.schema_version != MANDATE_SCHEMA_VERSION:
        reasons.append("live approval requires mandate schema v2")
    if mandate.consent.broker.strip().lower() != broker.strip().lower():
        reasons.append("mandate broker does not match the promotion channel")
    if not mandate.consent.account_ref.strip():
        reasons.append("mandate does not bind a dedicated account reference")
    if InstrumentType.PERPETUAL_SWAP not in mandate.hard_caps.allowed_instruments:
        reasons.append("mandate does not permit perpetual swaps")
    symbols = set(mandate.universe.include_symbols)
    if not symbols or not symbols.issubset({"BTC-USDT-SWAP", "ETH-USDT-SWAP"}):
        reasons.append("mandate must explicitly allow only supported BTC/ETH perpetual symbols")
    try:
        expires_at = _parse_time(mandate.consent.expires_at)
    except (TypeError, ValueError):
        reasons.append("mandate expiry is invalid")
    else:
        if expires_at <= now:
            reasons.append("mandate has expired")
    return reasons


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
