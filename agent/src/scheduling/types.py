"""Shared vocabulary types for the scheduling kernel (scheduler-unification T2).

Three pieces live here, all pure and domain-free:

* :data:`ExecutorKind` — how a job is executed (``"prompt"`` dispatches an
  agent session; ``"internal"`` runs a registered in-process callable). The
  value domain is defined here once so job models and registries cannot
  drift apart.
* :class:`JobLike` — the structural job surface a generic scheduler may rely
  on. Deliberately minimal: delivery / verdict / briefing / playbook fields
  belong to the ``scheduled_research`` domain and are NOT part of this
  protocol.
* :class:`TriggerSpec` — one trigger specification (schedule string plus the
  IANA timezone a cron schedule is evaluated in) behind a single value
  object, unifying the scheduled-research grammar with the ``once`` kind
  absorbed from the live runtime (see :mod:`src.scheduling.triggers`).

The callback type aliases at the bottom are the injection seams of
:class:`src.scheduling.kernel.SchedulerKernel`; they are declared here so the
kernel module stays free of domain imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional, Protocol, runtime_checkable

from src.scheduling.triggers import (
    ONCE_SPEC,
    next_due,
    next_once_due,
    validate_schedule,
    validate_timezone,
)

# ---------------------------------------------------------------------------
# Executor kind
# ---------------------------------------------------------------------------

#: How a job is executed: ``"prompt"`` dispatches an agent session (the only
#: kind that exists today), ``"internal"`` runs a callable from the explicit
#: in-process registry (added by later todos of the scheduler-unification
#: plan; nothing consumes it yet).
ExecutorKind = Literal["prompt", "internal"]

#: The closed value domain of :data:`ExecutorKind`, for runtime validation.
EXECUTOR_KINDS: tuple[str, ...] = ("prompt", "internal")


def is_executor_kind(value: object) -> bool:
    """Return whether *value* is a member of :data:`EXECUTOR_KINDS`."""
    return value in EXECUTOR_KINDS


# ---------------------------------------------------------------------------
# Job protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class JobLike(Protocol):
    """The structural job surface the kernel and its callbacks may rely on.

    Only the attributes a generic scheduler needs to decide due-ness, advance
    the next run and reason about retries are required here. Domain fields
    (delivery outbox, verdict records, briefing text, playbook provenance)
    stay in ``scheduled_research`` — consumers that need them accept the
    concrete job type in their own callbacks.

    ``isinstance`` checks work structurally (``runtime_checkable`` verifies
    attribute presence, not types), so existing dataclass jobs satisfy this
    protocol without inheriting from it.
    """

    #: Stable job identifier.
    id: str
    #: Epoch-millisecond timestamp of the next intended run.
    next_run_at: int
    #: Lifecycle status. Domain status enums are ``str``-valued and satisfy
    #: this annotation without the kernel importing them.
    status: str
    #: Trigger schedule string (see :mod:`src.scheduling.triggers`).
    schedule: str
    #: IANA timezone key a cron schedule is evaluated in, or ``None`` (UTC).
    timezone: Optional[str]
    #: Consecutive dispatch failures, driving retry backoff.
    consecutive_failures: int


# ---------------------------------------------------------------------------
# Trigger specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """One trigger specification: a schedule string plus its timezone.

    Wraps the two trigger kinds of the shared vocabulary behind a single
    value object:

    * the scheduled-research grammar — a bare ``"<ms>"`` interval string or a
      simplified 5-field cron expression evaluated on the wall clock of
      ``timezone`` (UTC when ``None``);
    * the ``once`` kind absorbed from the live runtime — fires exactly once
      and has no next run, so :meth:`next_due_after` returns ``None``.

    The live runtime's ``"interval:<ms>"`` spelling is intentionally out of
    scope for this value object; callers that need it use
    :func:`src.scheduling.triggers.next_once_due` directly.

    Construction is tolerant (no implicit validation), mirroring the repo's
    forgiving ``from_dict`` philosophy; call :meth:`validate` explicitly to
    reject an unusable spec.
    """

    schedule: str
    timezone: Optional[str] = None

    @property
    def is_once(self) -> bool:
        """Return whether this is the one-shot ``once`` kind."""
        return self.schedule.strip().lower() == ONCE_SPEC

    def validate(self) -> None:
        """Raise when the spec cannot be used to compute next runs.

        Delegates to :func:`src.scheduling.triggers.validate_schedule` and
        :func:`src.scheduling.triggers.validate_timezone` for the research
        grammar. The ``once`` kind carries no grammar to validate and is
        always accepted (its timezone is ignored, as for interval schedules).
        """
        if self.is_once:
            return
        validate_schedule(self.schedule)
        validate_timezone(self.timezone)

    def next_due_after(self, after_ms: int) -> Optional[int]:
        """Return the next due epoch-ms strictly after *after_ms*.

        ``None`` means the trigger never fires again (the one-shot ``once``
        kind). Cron window exhaustion propagates the ``ValueError`` raised by
        :func:`src.scheduling.triggers.next_due` rather than being swallowed.
        """
        if self.is_once:
            return next_once_due(self.schedule, after_ms)
        return next_due(self.schedule, after_ms, self.timezone)


# ---------------------------------------------------------------------------
# Kernel callback seams
# ---------------------------------------------------------------------------

#: Injectable wall-clock source returning epoch milliseconds.
NowFn = Callable[[], int]

#: Called by the kernel loop once per tick with the current epoch-ms. The
#: kernel isolates exceptions per tick (one failing tick never kills the
#: loop); ``asyncio.CancelledError`` propagates so ``stop()`` stays bounded.
OnTickFn = Callable[[int], Awaitable[None]]

#: ``(job, now_ms) -> due?`` Pure predicate deciding whether *job* should
#: fire at *now_ms*. The domain owns the gates (terminal statuses, end
#: boundaries, in-flight deliveries); the kernel never interprets them.
IsDueFn = Callable[[JobLike, int], bool]

#: ``(job, from_ms) -> next_run_at | None`` Pure computation of the next run
#: epoch-ms strictly after *from_ms*, or ``None`` when the trigger does not
#: fire again. Grammar errors (e.g. cron window exhaustion) surface as
#: ``ValueError`` for the domain's error hook to record.
AdvanceFn = Callable[[JobLike, int], Optional[int]]

#: ``(job, now_ms, exc)`` Hook for per-job failures, invoked by the domain's
#: tick composition inside its per-job isolation. Synchronous by contract:
#: it records (log / persist) and must not re-raise.
JobErrorFn = Callable[[JobLike, int, Exception], None]
