"""Protected system jobs: definitions and idempotent registration.

A *system job* is a built-in scheduled task the product owns end to end: its
row in the scheduled-research store is seeded and repaired by this module at
startup, it executes an in-process callable through the registry allowlist
(:mod:`src.scheduling.registry`) — never a prompt dispatch — and the
route/proposal/CLI guards keep users from deleting, rewriting or forging it
through any API surface.

Contracts implemented here:

* **Timing** — :func:`ensure_system_jobs` runs from the ``api_server``
  lifespan BEFORE the executor's ``start()``, so every enabled system job is
  in the store before the first tick can run.
* **Idempotence** — an existing row is never re-seeded: runtime fields
  (next_run_at/last_run_at/consecutive_failures/last_error/failure_kind/
  created_at/config/title/prompt/delivery*) and a live status
  (PENDING/RUNNING/COMPLETED) are left as they are; tampered spec-owned
  fields (source_type/executor_kind/internal_callable) ARE corrected. The
  one deliberate exception is the retry-budget reset below, which clears
  ``consecutive_failures``/``last_error``/``failure_kind`` together with a
  FAILED status it would be inconsistent to keep.
* **Schedule trust boundary (local trust)** — a syntactically VALID
  user-edited ``schedule``/``timezone`` is LEFT ALONE: whoever can edit the
  store file could equally flip the env flags off, so local tampering is not
  a security boundary (the same reasoning as the win32 no-lock memory
  contract). An INVALID one IS repaired to the spec default: the
  ``validate=False`` repair write would otherwise persist garbage the
  executor could only mark FAILED (``failure_kind="schedule"``) — repair
  keeps "protected" functional.
* **Startup status reset** — a system row found in CANCELLED or EXPIRED is
  reset to PENDING with ``end_at`` cleared. WHY safe: no API path (REST
  DELETE/PATCH, proposals, playbook/CLI) can put a system job there, so
  those states can only come from a manual store-file edit (or this module's
  own flag-off halt) — resetting makes "protected" true at the local-file
  layer. An enabled row's ``end_at`` is cleared even while PENDING: specs
  define no end boundary, and a hand-added future ``end_at`` would be
  honoured by ``_expire_elapsed_jobs`` mid-life — same tamper class.
* **Retry-budget reset** — a system row found in FAILED (the executor's
  terminal state after ``max_consecutive_failures`` consecutive dispatch
  failures) is reset to PENDING with ``end_at`` cleared and
  ``consecutive_failures``/``last_error``/``failure_kind`` zeroed, but ONLY
  for an enabled spec. Without this, a transient multi-day outage would
  permanently brick a product-owned job that no API surface may resurrect
  (PATCH/DELETE refuse system ids by design). The reset is bounded: a
  genuinely broken callable simply re-fails into FAILED within its retry
  budget, and the reset itself happens once per process start. A DISABLED
  spec leaves FAILED untouched — it is already never-due.
* **Repair writes** — every repair/halt write uses
  ``store.upsert(job, validate=False)`` so a tampered row can never crash
  startup; the fix is "correct + log" (the executor's lifecycle-write
  convention — ``store.py`` upsert docstring).
* **Repair-replay** — a system job deleted from the store by hand is
  re-seeded at the next startup, else "protected" would be vacuous.
* **DOUBLE-gate enablement chain** — a system job executes only when BOTH
  gates hold: the master switch ``VIBE_TRADING_ENABLE_SCHEDULER`` (without
  it the executor never starts — ``scheduled_routes`` early-return) AND the
  job's own flag as the spec's ``enabled_predicate`` (the domain modules
  bind predicates reading the env flags they declare; THIS MODULE READS NO
  ENVIRONMENT ITSELF — predicates and schedule derivations are injected,
  keeping the env surface owned by the domain modules and the AST gate
  clean). Flag-off direction: a DISABLED spec with an existing row is
  halted — a runnable status (PENDING/RUNNING/COMPLETED) becomes CANCELLED
  so the never-due set excludes it; terminal statuses are untouched, and
  re-enabling undoes the halt via the startup status reset. A disabled spec
  with NO row writes nothing.
* **Registry independence** — seeding does NOT require ``internal_callable``
  to be registered. A seeded job whose key is unregistered fails at
  EXECUTION time in the supervisor's resolve step, recorded as
  ``failure_kind="dispatch"`` with a readable ``last_error`` — never
  silently skipped, never prompt-dispatched.

ID convention: every system job id starts with ``system:`` — the colon is
deliberately part of the convention because :data:`SAFE_JOB_ID_RE` (the
single job-id shape rule every create/mutate surface enforces, matching the
``_SAFE_PATH_PARAM_RE`` the host routes apply to path parameters) rejects
``:``, so forging or path-addressing a system id through those surfaces is
structurally impossible. :func:`is_system_job_id` is the single prefix
predicate the guards consume.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from src.br_data.scheduling import daily_cron_from_env as _br_data_daily_cron
from src.br_data.scheduling import sync_job_enabled as _br_data_sync_enabled
from src.memory.consolidation import consolidation_enabled as _memory_consolidation_enabled
from src.scheduled_research.models import (
    JobStatus,
    ScheduledResearchJob,
    validate_schedule,
    validate_timezone_shape,
)
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling.triggers import is_interval_schedule, next_due
from src.scheduling.types import ExecutorKind

logger = logging.getLogger(__name__)

#: Mandatory id prefix. Contains ``:`` on purpose — :data:`SAFE_JOB_ID_RE`
#: rejects every id carrying it (module-docstring ID convention).
SYSTEM_JOB_ID_PREFIX = "system:"

#: The single job-id shape rule, shared by every surface that authors or
#: addresses a job id (REST create, playbook route, service drafts, CLI) and
#: identical to the ``_SAFE_PATH_PARAM_RE`` the host routes enforce on path
#: parameters, so a job can never be created under an id the address-based
#: routes would refuse. Rejecting ``:`` is what makes
#: :data:`SYSTEM_JOB_ID_PREFIX` unforgeable through user-facing surfaces.
SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: Single home of the user-facing job-id rule wording (the REST 422 detail,
#: the service ``ValueError`` and the CLI error string all quote it).
JOB_ID_RULE_MESSAGE = "job id must be 1-128 characters of letters, digits, '_' or '-'"

#: Per-spec enablement gate (the domain modules bind these to the env flags
#: they declare).
EnabledPredicate = Callable[[], bool]
#: Lazy per-spec schedule derivation, evaluated at :func:`ensure_system_jobs`
#: time — after dotenv load — instead of at module import (same rationale as
#: :data:`EnabledPredicate`; must return a schedule passing
#: :func:`validate_schedule`).
ScheduleFn = Callable[[], str]
#: Caller-level override gate for :func:`ensure_system_jobs`; receives the
#: spec, returns enablement.
SpecGate = Callable[["SystemJobSpec"], bool]
#: Injectable wall clock returning epoch milliseconds.
NowFn = Callable[[], int]

#: Statuses that keep a row executable by the tick loop (``is_due``'s
#: complement of the never-due set); a disabled spec halts exactly these.
_RUNNABLE_STATUSES = frozenset({JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COMPLETED})
#: Statuses only reachable via manual file edits or this module's own
#: flag-off halt; the startup reset restores them when the spec is enabled.
_TAMPER_TERMINAL_STATUSES = frozenset({JobStatus.CANCELLED, JobStatus.EXPIRED})


def is_safe_job_id(job_id: object) -> bool:
    """Whether *job_id* is authorable through user-facing surfaces.

    The single shape predicate behind every create/mutate guard; never
    raises, non-strings are ``False``. Every ``system:``-prefixed id fails
    by construction (the pattern rejects ``:``).
    """
    return isinstance(job_id, str) and bool(SAFE_JOB_ID_RE.fullmatch(job_id))


def _always_enabled() -> bool:
    """Default spec gate: enabled; production specs bind env-flag readers."""
    return True


def _now_ms() -> int:
    """Return current wall-clock time in epoch milliseconds."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class SystemJobSpec:
    """Declarative definition of one protected system job.

    Attributes:
        id: Stable, predictable job id; MUST start with
            :data:`SYSTEM_JOB_ID_PREFIX` (the convention the guards and
            :data:`SAFE_JOB_ID_RE` interplay depend on).
        schedule: Static default schedule (interval-ms string or 5-field
            cron), validated at authoring time. Mutually exclusive with
            ``schedule_fn``. Seeded verbatim; on an existing row a VALID
            edited schedule is trusted (local-trust boundary) and only an
            INVALID one is repaired back to this.
        schedule_fn: Lazy schedule derivation evaluated inside
            :func:`ensure_system_jobs` (never at import time), for specs
            whose default comes from environment configuration — evaluating
            at import would freeze the value before dotenv load and make the
            wrong seed permanent under the schedule-trust boundary. Must
            return a schedule passing :func:`validate_schedule`.
        internal_callable: Registry KEY name (never an import path) resolved
            through :mod:`src.scheduling.registry` at execution time.
        title: Human-readable label seeded with the row.
        timezone: IANA key for cron schedules, ``None`` for UTC/interval.
        prompt: Always ``""`` — internal/system jobs carry no prompt
            (``from_dict`` accepts that).
        source_type: Fixed ``"system"``.
        executor_kind: Fixed ``"internal"``.
        enabled_predicate: Zero-arg callable deciding the spec's own gate —
            the second half of the double-gate chain (module docstring).
            This dataclass never reads the environment itself.
    """

    id: str
    schedule: str = ""
    internal_callable: str = ""
    title: str = ""
    timezone: Optional[str] = None
    prompt: str = ""
    source_type: str = "system"
    executor_kind: ExecutorKind = "internal"
    enabled_predicate: EnabledPredicate = field(default=_always_enabled)
    schedule_fn: Optional[ScheduleFn] = None

    def __post_init__(self) -> None:
        """Fail fast on spec-authoring bugs (specs are appended by hand)."""
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
        if bool(self.schedule) == (self.schedule_fn is not None):
            raise ValueError(
                f"system job spec {self.id!r} requires exactly one of schedule / schedule_fn"
            )
        if self.schedule_fn is not None and not callable(self.schedule_fn):
            raise TypeError(f"system job spec {self.id!r} requires a callable schedule_fn")
        # A static default must be valid at authoring time; seeding uses
        # validate=True so a bad spec surfaces in CI, never at a user startup.
        # A schedule_fn result is validated by the same store write at seed
        # time (the derivation itself is contractually never-raising).
        if self.schedule:
            validate_schedule(self.schedule)
        validate_timezone_shape(self.timezone)

    def resolved_schedule(self) -> str:
        """Return the effective default schedule (static or lazily derived)."""
        if self.schedule_fn is not None:
            return self.schedule_fn()
        return self.schedule


# ---------------------------------------------------------------------------
# Production spec surface
# ---------------------------------------------------------------------------

#: The production specs (memory consolidation, br_data sync). Each job is ONE
#: literal here plus ONE registry entry in ``registry.py``'s
#: ``_INTERNAL_CALLABLES`` block; ensure_system_jobs, the guards and the
#: lifespan wiring consume this tuple generically. Predicates and schedule
#: derivation live in the DOMAIN modules (src.memory.consolidation,
#: src.br_data.scheduling) so THIS module keeps reading zero env. A valid
#: edited schedule/timezone on a seeded row is trusted per the local-trust
#: boundary.
_SYSTEM_JOB_SPECS: tuple[SystemJobSpec, ...] = (
    SystemJobSpec(
        id="system:memory-consolidation",
        schedule="0 4 * * *",
        timezone="UTC",
        internal_callable="memory.consolidate",
        title="Memory consolidation",
        enabled_predicate=_memory_consolidation_enabled,
    ),
    # Daily Brazilian official-dataset sync on the São Paulo wall clock
    # (sync.TZ key). The cron derives from VIBE_BR_SYNC_TIME lazily — at
    # ensure time, after dotenv load — and drives the FIRST seed only: the
    # local-trust boundary accepts an edited row afterwards.
    SystemJobSpec(
        id="system:br-data-sync",
        schedule_fn=_br_data_daily_cron,
        timezone="America/Sao_Paulo",
        internal_callable="br_data.run_sync",
        title="Brazilian dataset sync",
        enabled_predicate=_br_data_sync_enabled,
    ),
)


def system_job_specs() -> tuple[SystemJobSpec, ...]:
    """Return every declared production system-job spec (immutable tuple)."""
    return _SYSTEM_JOB_SPECS


def is_system_job_id(job_id: object) -> bool:
    """Whether *job_id* carries the protected ``system:`` prefix.

    The single predicate the mutation guards consume. Pure prefix test —
    never raises, non-strings are ``False``. The prefix contains ``:``,
    which :data:`SAFE_JOB_ID_RE` rejects, so a user-facing create path can
    never mint an id this function returns ``True`` for.
    """
    return isinstance(job_id, str) and job_id.startswith(SYSTEM_JOB_ID_PREFIX)


@dataclass(frozen=True)
class EnsureSystemJobsReport:
    """Per-outcome id lists from one :func:`ensure_system_jobs` pass.

    ``seeded``: missing rows created (enabled specs; repair-replay lands
    here too). ``repaired``: enabled rows rewritten (tamper correction,
    invalid-schedule repair, startup status/retry-budget reset, ``end_at``
    clearing). ``unchanged``: conformant enabled rows, NOT written.
    ``skipped_disabled``: disabled specs with no row. ``halted``: disabled
    specs with an existing row (made non-runnable/corrected, or already
    terminal, no write needed).
    """

    seeded: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped_disabled: tuple[str, ...] = ()
    halted: tuple[str, ...] = ()


def _seed_job(spec: SystemJobSpec, now_ms: int) -> ScheduledResearchJob:
    """Build a fresh row for *spec* (mirrors ``build_job_from_draft``'s next-run
    convention: cron-with-tz waits for its next slot; interval/UTC-cron due now)."""
    schedule = spec.resolved_schedule()
    next_run_at = now_ms
    if spec.timezone is not None and not is_interval_schedule(schedule):
        next_run_at = next_due(schedule, now_ms, spec.timezone)
    return ScheduledResearchJob(
        id=spec.id,
        prompt=spec.prompt,
        schedule=schedule,
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
    exactly the check ``store.upsert(validate=True)`` applies, so the
    local-trust boundary uses the system-wide grammar: valid-but-different is
    trusted local editing; invalid is garbage a ``validate=False`` write must
    not keep.
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
    internal_callable, invalid schedule/timezone, the startup status and
    retry-budget resets, ``end_at``, and the flag-off halt). Other runtime
    fields — next_run_at, last_run_at, created_at, config, title, prompt,
    delivery* — are never modified here; consecutive_failures/last_error/
    failure_kind are cleared ONLY together with a FAILED-status reset (an
    enabled spec's retry-budget reset — keeping the failure counters without
    the terminal status would immediately re-terminalize the row).
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
    # Local-trust boundary: a VALID edited schedule/timezone is trusted and
    # left alone; an INVALID one is repaired to the spec default — the
    # validate=False repair write below would otherwise persist garbage.
    if not _schedule_shape_ok(job.schedule, job.timezone):
        notes.append(
            f"invalid schedule/timezone ({job.schedule!r}, {job.timezone!r}) "
            f"-> spec default ({spec.resolved_schedule()!r}, {spec.timezone!r})"
        )
        job.schedule = spec.resolved_schedule()
        job.timezone = spec.timezone
    if enabled:
        # Startup status reset: no API path can put a system job in these
        # statuses, so they can only come from manual file edits (or our own
        # flag-off halt); resetting them keeps the "protected" claim true at
        # the local-file layer.
        if job.status in _TAMPER_TERMINAL_STATUSES:
            notes.append(f"status {job.status.value!r} -> 'pending' (startup reset), end_at cleared")
            job.status = JobStatus.PENDING
            job.end_at = None
        elif job.status is JobStatus.FAILED:
            # Retry-budget reset (module docstring): FAILED IS reachable at
            # runtime through the executor's own retry exhaustion, and no API
            # surface may resurrect a system row — without this reset a
            # transient outage would permanently brick a product-owned job.
            notes.append(
                f"status 'failed' -> 'pending' (retry budget reset at startup), "
                f"end_at cleared, {job.consecutive_failures} consecutive failure(s) cleared"
            )
            job.status = JobStatus.PENDING
            job.end_at = None
            job.consecutive_failures = 0
            job.last_error = None
            job.failure_kind = None
        elif job.end_at is not None:
            # Specs define no end boundary; a hand-added future end_at would
            # silently expire the job mid-life (same tamper class).
            notes.append(f"end_at {job.end_at} -> None (system jobs do not expire)")
            job.end_at = None
    elif job.status in _RUNNABLE_STATUSES:
        # Enablement chain, flag-off direction: the row must not keep firing
        # after its flag turned off. CANCELLED is terminal in is_due,
        # non-destructive (runtime fields preserved) and undone by the
        # startup status reset when the flag comes back on.
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

    Call at lifespan startup BEFORE ``executor.start()`` (timing contract,
    module docstring). Running it twice changes nothing the second time:
    conformant rows are not rewritten at all, so runtime state and store
    bytes survive.

    Args:
        store: The scheduled-research job store to reconcile against.
        enabled_predicate: Optional caller-level gate override receiving each
            spec; production passes nothing so each spec's own
            ``enabled_predicate`` decides (double-gate chain). Primarily an
            injection seam for tests and uniform external gating.
        specs: Optional explicit spec sequence; defaults to
            :func:`system_job_specs` (the production tuple). Tests inject
            framework specs here.
        now_fn: Injectable wall clock (epoch ms) for seeded timestamps.

    Returns:
        An :class:`EnsureSystemJobsReport` of per-outcome job ids.

    Raises:
        ValueError: Only when a SEEDED spec default fails store validation —
            a spec-authoring bug that must crash CI (repairs never raise;
            they use ``validate=False`` per the repair-write contract).
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
                # Disabled + no row -> nothing is written at all.
                skipped.append(spec.id)
                continue
            # Repair-replay shares this path: a hand-deleted job is re-seeded
            # at the next startup. validate=True on purpose — the spec
            # defaults are ours, checked in __post_init__ (static) or by this
            # write (lazily derived).
            store.upsert(_seed_job(spec, now))
            seeded.append(spec.id)
            logger.info("system job %s seeded (schedule=%r)", spec.id, spec.resolved_schedule())
            continue
        notes = _reconcile(existing, spec, enabled=enabled)
        if not notes:
            (unchanged if enabled else halted).append(spec.id)
            continue
        # Repair/halt writes must always land — a row whose schedule no
        # longer validates must still be correctable without a crash.
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
