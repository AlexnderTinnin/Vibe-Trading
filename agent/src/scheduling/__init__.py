"""Shared scheduling kernel (scheduler unification).

This package is the single home of the trigger vocabulary every scheduler in
the repo can adopt:

* :mod:`src.scheduling.triggers` — pure, clock-injected trigger grammar
  (interval-ms + 5-field cron + IANA timezones with the DST gap-skip /
  fold=0 policy) and next-run math, plus the ``once`` trigger kind absorbed
  from the live runtime scheduler (provided for future adoption; not wired).

Later todos of the scheduler-unification plan add ``types.py`` (protocols),
``kernel.py`` (tick loop), ``registry.py`` (internal callable allowlist) and
``system_jobs.py`` (protected system jobs) beside this module.
"""

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

__all__ = [
    "CRON_BOUNDS",
    "CRON_SEARCH_LIMIT_DAYS",
    "DEFAULT_TRIGGER_INTERVAL_MS",
    "ONCE_SPEC",
    "day_matches",
    "is_interval_schedule",
    "local_wall_time_to_epoch_ms",
    "next_cron_due",
    "next_due",
    "next_once_due",
    "parse_cron_field",
    "trigger_interval_ms",
    "validate_schedule",
    "validate_timezone",
    "validate_timezone_shape",
]
