"""Shared scheduling kernel (scheduler unification).

This package is the single home of the scheduling machinery every scheduler
in the repo can adopt:

* :mod:`src.scheduling.triggers` — pure, clock-injected trigger grammar
  (interval-ms + 5-field cron + IANA timezones with the DST gap-skip /
  fold=0 policy) and next-run math, plus the ``once`` trigger kind absorbed
  from the live runtime scheduler (provided for future adoption; not wired).
* :mod:`src.scheduling.types` — the shared vocabulary: ``ExecutorKind``,
  the minimal ``JobLike`` protocol, ``TriggerSpec`` and the kernel callback
  type aliases.
* :mod:`src.scheduling.kernel` — ``SchedulerKernel``: the generic tick loop
  with ``asyncio.Event`` wakeup, bounded exponential backoff, an injectable
  clock and callback seams for domain logic (consumed by the scheduled
  research executor since T4).
* :mod:`src.scheduling.registry` — the internal-callable allowlist: an
  explicit module-level dict mapping literal registry keys to zero-arg
  sync/async callables, with exact-key resolution only (no dynamic import,
  no string parsing, no shell).

Later todos of the scheduler-unification plan add ``system_jobs.py``
(protected system jobs) beside these modules.
"""

from src.scheduling.kernel import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_RETRY_BASE_DELAY_MS,
    DEFAULT_RETRY_MAX_DELAY_MS,
    DEFAULT_TICK_INTERVAL_MS,
    DEFAULT_TASK_NAME,
    SchedulerKernel,
)
from src.scheduling.registry import (
    InternalCallableError,
    InternalRunnable,
    is_registered,
    list_internal,
    register_internal,
    resolve_internal,
)
from src.scheduling.triggers import (
    CRON_BOUNDS,
    CRON_SEARCH_LIMIT_DAYS,
    DEFAULT_TRIGGER_INTERVAL_MS,
    ONCE_SPEC,
    day_matches,
    is_interval_schedule,
    local_wall_time_to_epoch_ms,
    next_cron_due,
    next_due,
    next_once_due,
    parse_cron_field,
    trigger_interval_ms,
    validate_schedule,
    validate_timezone,
    validate_timezone_shape,
)
from src.scheduling.types import (
    EXECUTOR_KINDS,
    AdvanceFn,
    ExecutorKind,
    IsDueFn,
    JobErrorFn,
    JobLike,
    NowFn,
    OnTickFn,
    TriggerSpec,
    is_executor_kind,
)

__all__ = [
    "CRON_BOUNDS",
    "CRON_SEARCH_LIMIT_DAYS",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_RETRY_BASE_DELAY_MS",
    "DEFAULT_RETRY_MAX_DELAY_MS",
    "DEFAULT_TASK_NAME",
    "DEFAULT_TICK_INTERVAL_MS",
    "DEFAULT_TRIGGER_INTERVAL_MS",
    "EXECUTOR_KINDS",
    "ONCE_SPEC",
    "AdvanceFn",
    "ExecutorKind",
    "InternalCallableError",
    "InternalRunnable",
    "IsDueFn",
    "JobErrorFn",
    "JobLike",
    "NowFn",
    "OnTickFn",
    "SchedulerKernel",
    "TriggerSpec",
    "day_matches",
    "is_executor_kind",
    "is_interval_schedule",
    "is_registered",
    "list_internal",
    "local_wall_time_to_epoch_ms",
    "next_cron_due",
    "next_due",
    "next_once_due",
    "parse_cron_field",
    "register_internal",
    "resolve_internal",
    "trigger_interval_ms",
    "validate_schedule",
    "validate_timezone",
    "validate_timezone_shape",
]
