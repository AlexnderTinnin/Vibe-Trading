"""Protected system jobs: definitions and idempotent registration (T6).

A *system job* is a built-in scheduled task the product owns end to end: its
row in the scheduled-research store is seeded and repaired by this module at
startup, it executes an in-process callable through the registry allowlist
(:mod:`src.scheduling.registry`) — never a prompt dispatch — and, once T7
lands its route/proposal/CLI guards, users cannot delete, rewrite or forge it
through any API surface.

Contracts implemented here (scheduler-unification plan todo 6, ③b):

* **(i) Timing** — :func:`ensure_system_jobs` is called from the ``api_server``
  lifespan startup BEFORE the executor's ``start()`` (T12 wires the call), so
  every enabled system job is in the store before the first tick can run.
* **(ii) Idempotence** — an existing row is never re-seeded: runtime fields
  (``next_run_at`` / ``last_run_at`` / ``consecutive_failures`` /
  ``last_error`` / ``failure_kind`` / ``created_at`` / ``config`` / ``title`` /
  ``prompt`` / every ``delivery*`` field) and a live status (PENDING /
  RUNNING / COMPLETED) are left exactly as they are; tampered spec-owned
  fields ``source_type`` / ``executor_kind`` / ``internal_callable`` ARE
  corrected back to the spec.
* **(ii-c) schedule trust boundary — plan choice (β), local trust** — a
  syntactically VALID user-edited ``schedule``/``timezone`` is LEFT ALONE:
  whoever can edit the store file could equally flip the env flags off, so
  local file tampering is not treated as a security boundary (same reasoning
  as the documented win32 no-lock limitation). An INVALID schedule/timezone,
  however, IS repaired to the spec default: repair writes go through
  ``store.upsert(job, validate=False)`` (plan (ii-b)), which would otherwise
  persist garbage the executor could only mark FAILED
  (``failure_kind="schedule"``) — repairing keeps the "protected" claim
  functional rather than merely nominal.
* **(α) status reset** — a system row found in CANCELLED or EXPIRED is reset
  to PENDING with ``end_at`` cleared. WHY this is safe: after T7, no API path
  (REST DELETE/PATCH, proposals, playbook/CLI creation) can put a system job
  into those states, so they can only originate from a manual store-file edit
  (or from this module's own flag-off halt below) — resetting makes
  "protected" true at the local-file layer too. An enabled row's ``end_at`` is
  cleared even while PENDING: specs define no end boundary, and a manually
  added future ``end_at`` would otherwise be honoured by
  ``_expire_elapsed_jobs`` mid-life — the same tamper class α addresses.
  (For CHANGELOG/T13: manual cancellation of a system job via file edit is
  undone at the next startup.)
* **(ii-b) repair writes** — every repair/halt write uses
  ``store.upsert(job, validate=False)`` so a tampered row can never crash
  startup; the fix is "correct + log", mirroring the executor's lifecycle-write
  convention (``store.py`` upsert docstring).
* **(iii) repair-replay** — a system job deleted from the store by hand is
  re-seeded at the next startup, else "protected" would be vacuous.
* **(iv) DOUBLE-gate enablement chain** — a system job actually executes only
  when BOTH gates hold: the scheduler master switch
  ``VIBE_TRADING_ENABLE_SCHEDULER`` (without it the executor never starts —
  ``scheduled_routes`` early-return) AND the job's own flag, expressed here as
  the spec's ``enabled_predicate`` (T8/T9 supply predicates reading the env
  flags they declare; THIS MODULE READS NO ENVIRONMENT ITSELF — predicates are
  injected, keeping the env surface owned by T8/T9 and the AST gate clean).
  To make the chain true in the flag-off direction too, a DISABLED spec whose
  row already exists (flag was on, then turned off) is halted: a runnable
  status (PENDING/RUNNING/COMPLETED) becomes CANCELLED so the executor's
  never-due set excludes it; terminal statuses are left untouched. Re-enabling
  the flag undoes the halt through the (α) reset above. A disabled spec with
  NO row writes nothing at all (plan QA (a)).
* **Registry independence** — seeding does NOT require ``internal_callable``
  to be registered (the registry dict is empty until T8/T9 append their
  callables). A seeded job whose callable is unregistered fails at EXECUTION
  time in the supervisor's resolve step, recorded as ``failure_kind="dispatch"``
  with a readable ``last_error`` (plan QA (c)) — never silently skipped, never
  prompt-dispatched.

ID convention for T7: every system job id starts with ``system:`` — the
colon is deliberately part of the convention because ``_SAFE_JOB_ID_RE``
(``^[A-Za-z0-9_-]{1,128}$``, enforced on POST/playbook routes and in
``_validate_path_param``) rejects ``:``, so forging or path-addressing a
system id through those surfaces is structurally impossible.
:func:`is_system_job_id` is the single prefix predicate T7's guards consume.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from src.scheduled_research.models import (
    JobStatus,
    ScheduledResearchJob,
    validate_schedule,
    validate_timezone_shape,
)
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling.triggers import is_interval_schedule, next_due

logger = logging.getLogger(__name__)

#: Mandatory id prefix. Contains ``:`` on purpose — see the ID-convention
#: note in the module docstring (T7 relies on ``_SAFE_JOB_ID_RE`` rejecting
#: every id that carries this prefix).
SYSTEM_JOB_ID_PREFIX = "system:"

#: Per-spec enablement gate (T8/T9 bind these to the env flags they declare).
EnabledPredicate = Callable[[], bool]
#: Caller-level override gate used by :func:`ensure_system_jobs`'s
#: ``enabled_predicate`` parameter; receives the spec, returns enablement.
SpecGate = Callable[["SystemJobSpec"], bool]
#: Injectable wall clock returning epoch milliseconds.
NowFn = Callable[[], int]

#: Statuses that keep a row executable by the tick loop (``is_due``'s
#: complement of the never-due set); a disabled spec halts exactly these.
_RUNNABLE_STATUSES = frozenset({JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COMPLETED})
#: Statuses only reachable (post-T7) via manual file edits or this module's
#: own flag-off halt; (α) resets them when the spec is enabled again.
_TAMPER_TERMINAL_STATUSES = frozenset({JobStatus.CANCELLED, JobStatus.EXPIRED})


def _always_enabled() -> bool:
    """Default spec gate: enabled. T8/T9 replace it with env-flag readers."""
    return True


def _now_ms() -> int:
    """Return current wall-clock time in epoch milliseconds."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class SystemJobSpec:
    """Declarative definition of one protected system job.

    Attributes:
        id: Stable, predictable job id; MUST start with
            :data:`SYSTEM_JOB_ID_PREFIX` (the ``system:`` prefix convention
            T7's guards and ``_SAFE_JOB_ID_RE`` interplay depend on).
        schedule: Default schedule (interval-ms string or 5-field cron).
            Seeded verbatim; on an existing row a VALID edited schedule is
            trusted (β) and only an INVALID one is repaired back to this.
        internal_callable: Registry KEY name (never an import path) resolved
            through :mod:`src.scheduling.registry` at execution time.
        title: Human-readable label seeded with the row.
        timezone: IANA key for cron schedules, ``None`` for UTC/interval.
        prompt: Always ``""`` — internal/system jobs carry no prompt
            (plan ③b(i); ``from_dict`` accepts ``source_type="system"`` with
            an empty prompt since T3).
        source_type: Fixed ``"system"``.
        executor_kind: Fixed ``"internal"``.
        enabled_predicate: Zero-arg callable deciding the spec's own gate —
            the second half of the double-gate chain (module docstring (iv)).
            This dataclass never reads the environment itself.
    """

    id: str
    schedule: str
    internal_callable: str
    title: str = ""
    timezone: Optional[str] = None
    prompt: str = ""
    source_type: str = "system"
    executor_kind: str = "internal"
    enabled_predicate: EnabledPredicate = field(default=_always_enabled)

    def __post_init__(self) -> None:
        """Fail fast on spec-authoring bugs (T8/T9 append specs by hand)."""
        if not isinstance(self.id, str) or not self.id.startswith(SYSTEM_JOB_ID_PREFIX):
            raise ValueError(f"system job id {self.id!r} must start with {SYSTEM_JOB_ID_PREFIX!r}")
        if len(self.id) <= len(SYSTEM_JOB_ID_PREFIX):
            raise ValueError(f"system job id {self.id!r} needs a name after the prefix")
        if self.source_type != "system":
            raise ValueError(f"system job spec {self.id!r} must keep source_type='system'")
        if self.executor_kind != "internal":
            raise ValueError(f"system job spec {self.id!r} must keep executor_kind='internal'")
        if not self.internal_callable:
            raise ValueError(f"system job spec {self.id!r} requires an internal_callable registry key")
        if not callable(self.enabled_predicate):
            raise TypeError(f"system job spec {self.id!r} requires a callable enabled_predicate")
        # Spec-owned defaults must be valid at authoring time; seeding uses
        # validate=True so a bad spec surfaces in CI, never at a user startup.
        validate_schedule(self.schedule)
        validate_timezone_shape(self.timezone)


# ---------------------------------------------------------------------------
# Production spec surface
# ---------------------------------------------------------------------------

#: The production specs. EMPTY by design in T6 — the framework ships first;
#: T8 (memory consolidation) and T9 (br_data sync) each append ONE literal
#: here plus ONE registry entry in ``registry.py``'s ``_INTERNAL_CALLABLES``
#: block, e.g.:
#:
#:     SystemJobSpec(
#:         id="system:memory-consolidation",
#:         schedule="0 3 * * *",
#:         timezone="UTC",
#:         internal_callable="memory.consolidate",
#:         title="Memory consolidation",
#:         enabled_predicate=_memory_consolidation_enabled,  # reads T8's flag
#:     ),
#:
#: Nothing else changes: ensure_system_jobs, the guards (T7) and the lifespan
#: wiring (T12) all consume this tuple generically.
_SYSTEM_JOB_SPECS: tuple[SystemJobSpec, ...] = ()


def system_job_specs() -> tuple[SystemJobSpec, ...]:
    """Return every declared production system-job spec (immutable tuple)."""
    return _SYSTEM_JOB_SPECS


def is_system_job_id(job_id: object) -> bool:
    """Whether *job_id* carries the protected ``system:`` prefix.

    The single predicate T7's route/proposal/CLI guards consume. Pure prefix
    convention — never raises, non-strings are ``False``. Note the prefix
    contains ``:``, which ``_SAFE_JOB_ID_RE`` rejects, so a user-facing
    create path can never mint an id this function returns ``True`` for.
    """
    return isinstance(job_id, str) and job_id.startswith(SYSTEM_JOB_ID_PREFIX)


@dataclass(frozen=True)
class EnsureSystemJobsReport:
    """Per-outcome id lists from one :func:`ensure_system_jobs` pass.

    ``seeded``: missing rows created (enabled specs; repair-replay (iii) lands
    here too). ``repaired``: enabled rows rewritten (tamper correction,
    invalid-schedule repair, (α) reset, ``end_at`` clearing). ``unchanged``:
    conformant enabled rows, NOT written. ``skipped_disabled``: disabled specs
    with no row — nothing written. ``halted``: disabled specs with an existing
    row (made non-runnable/corrected, or already terminal, no write needed).
    """

    seeded: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped_disabled: tuple[str, ...] = ()
    halted: tuple[str, ...] = ()


def _seed_job(spec: SystemJobSpec, now_ms: int) -> ScheduledResearchJob:
    """Build a fresh row for *spec* (mirrors ``build_job_from_draft``'s next-run
    convention: cron-with-tz waits for its next slot; interval/UTC-cron due now)."""
    next_run_at = now_ms
    if spec.timezone is not None and not is_interval_schedule(spec.schedule):
        next_run_at = next_due(spec.schedule, now_ms, spec.timezone)
    return ScheduledResearchJob(
        id=spec.id,
        prompt=spec.prompt,
        schedule=spec.schedule,
        title=spec.title,
        source_type=spec.source_type,
        executor_kind=spec.executor_kind,
        internal_callable=spec.internal_callable,
        timezone=spec.timezone,
        next_run_at=next_run_at,
        created_at=now_ms,
        status=JobStatus.PENDING,
    )


def _schedule_shape_ok(schedule: str, timezone: Optional[str]) -> bool:
    """Whether *schedule*/*timezone* pass the store's syntactic validation —
    exactly the check ``store.upsert(validate=True)`` applies, so the (β)
    boundary uses the system-wide grammar: valid-but-different is trusted
    local editing; invalid is garbage a ``validate=False`` write must not keep.
    """
    try:
        validate_schedule(schedule)
        validate_timezone_shape(timezone)
    except ValueError:
        return False
    return True


def _reconcile(job: ScheduledResearchJob, spec: SystemJobSpec, *, enabled: bool) -> list[str]:
    """Mutate *job* toward *spec* in place; return human-readable change notes.

    Touches ONLY spec-owned fields (source_type / executor_kind /
    internal_callable, invalid schedule/timezone, the (α) status reset,
    ``end_at``, and the flag-off halt). Runtime fields — next_run_at,
    last_run_at, consecutive_failures, last_error, failure_kind, created_at,
    config, title, prompt, delivery* — are never modified here.
    """
    notes: list[str] = []
    if job.source_type != spec.source_type:
        notes.append(f"source_type {job.source_type!r} -> {spec.source_type!r}")
        job.source_type = spec.source_type
    if job.executor_kind != spec.executor_kind:
        notes.append(f"executor_kind {job.executor_kind!r} -> {spec.executor_kind!r}")
        job.executor_kind = spec.executor_kind
    if job.internal_callable != spec.internal_callable:
        notes.append(f"internal_callable {job.internal_callable!r} -> {spec.internal_callable!r}")
        job.internal_callable = spec.internal_callable
    # (ii-c)/(β): a VALID edited schedule/timezone is trusted and left alone;
    # an INVALID one is repaired to the spec default because the repair write
    # below uses validate=False and would otherwise persist garbage.
    if not _schedule_shape_ok(job.schedule, job.timezone):
        notes.append(
            f"invalid schedule/timezone ({job.schedule!r}, {job.timezone!r}) "
            f"-> spec default ({spec.schedule!r}, {spec.timezone!r})"
        )
        job.schedule = spec.schedule
        job.timezone = spec.timezone
    if enabled:
        # (α): post-T7 these statuses can only come from manual file edits (or
        # our own flag-off halt), so resetting them is safe and keeps the
        # "protected" claim true at the local-file layer.
        if job.status in _TAMPER_TERMINAL_STATUSES:
            notes.append(f"status {job.status.value!r} -> 'pending' (alpha reset), end_at cleared")
            job.status = JobStatus.PENDING
            job.end_at = None
        elif job.end_at is not None:
            # Specs define no end boundary; a hand-added future end_at would
            # silently expire the job mid-life (same tamper class as alpha).
            notes.append(f"end_at {job.end_at} -> None (system jobs do not expire)")
            job.end_at = None
    elif job.status in _RUNNABLE_STATUSES:
        # (iv) enablement chain, flag-off direction: the row must not keep
        # firing after its flag was turned off. CANCELLED is terminal in
        # is_due, non-destructive (runtime fields preserved) and undone by
        # the (alpha) reset when the flag comes back on.
        notes.append(f"status {job.status.value!r} -> 'cancelled' (spec flag disabled)")
        job.status = JobStatus.CANCELLED
    return notes


def ensure_system_jobs(
    store: ScheduledResearchJobStore,
    *,
    enabled_predicate: Optional[SpecGate] = None,
    specs: Optional[Sequence[SystemJobSpec]] = None,
    now_fn: Optional[NowFn] = None,
) -> EnsureSystemJobsReport:
    """Seed/repair every enabled system job into *store* (idempotent).

    Call at lifespan startup BEFORE ``executor.start()`` (contract (i), T12
    wires it). Running it twice changes nothing the second time: conformant
    rows are not rewritten at all, so runtime state and store bytes survive.

    Args:
        store: The scheduled-research job store to reconcile against.
        enabled_predicate: Optional caller-level gate override receiving each
            spec; production (T12) passes nothing so each spec's own
            ``enabled_predicate`` decides (the double-gate chain (iv) — the
            scheduler master switch still gates execution itself). Primarily
            an injection seam for tests and uniform external gating.
        specs: Optional explicit spec sequence; defaults to
            :func:`system_job_specs`. Tests inject framework specs here while
            the production tuple stays empty until T8/T9 append to it.
        now_fn: Injectable wall clock (epoch ms) for seeded timestamps.

    Returns:
        An :class:`EnsureSystemJobsReport` of per-outcome job ids.

    Raises:
        ValueError: Only when a SEEDED spec default fails store validation —
            a spec-authoring bug that must crash CI, not a user-tampered row
            (repairs never raise; they use ``validate=False`` per (ii-b)).
    """
    active_specs = tuple(system_job_specs() if specs is None else specs)
    now = _now_ms() if now_fn is None else int(now_fn())
    gate: SpecGate = enabled_predicate if enabled_predicate is not None else (lambda s: s.enabled_predicate())
    seeded: list[str] = []
    repaired: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    halted: list[str] = []
    for spec in active_specs:
        enabled = bool(gate(spec))
        existing = store.get(spec.id)
        if existing is None:
            if not enabled:
                # QA (a): disabled + no row -> nothing is written at all.
                skipped.append(spec.id)
                continue
            # (iii) repair-replay shares this path: a hand-deleted system job
            # is re-seeded at the next startup. validate=True on purpose —
            # the spec defaults are ours and were checked in __post_init__.
            store.upsert(_seed_job(spec, now))
            seeded.append(spec.id)
            logger.info("system job %s seeded (schedule=%r)", spec.id, spec.schedule)
            continue
        notes = _reconcile(existing, spec, enabled=enabled)
        if not notes:
            (unchanged if enabled else halted).append(spec.id)
            continue
        # (ii-b): repair/halt writes must always land — a row whose schedule
        # no longer validates must still be correctable without crashing
        # startup.
        store.upsert(existing, validate=False)
        (repaired if enabled else halted).append(spec.id)
        logger.info("system job %s reconciled at startup: %s", spec.id, "; ".join(notes))
    return EnsureSystemJobsReport(
        seeded=tuple(seeded),
        repaired=tuple(repaired),
        unchanged=tuple(unchanged),
        skipped_disabled=tuple(skipped),
        halted=tuple(halted),
    )
