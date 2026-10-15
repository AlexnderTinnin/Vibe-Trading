"""Golden-vector equivalence proof for the extracted scheduling kernel (T1).

The vectors in ``golden_next_due.json`` were captured mechanically from the
PRE-MOVE implementation (``src.scheduled_research.executor`` /
``src.scheduled_research.models``, byte-identical to origin/main 29aa084f)
*before* any code moved into ``src/scheduling/triggers.py``. This file replays
every vector against BOTH the new kernel and the legacy import paths, asserts
the legacy names are the identical objects the kernel defines, and covers the
new ``once`` trigger kind whose semantics were copied (not imported) from
``agent/src/live/runtime/scheduler.py``.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.scheduled_research import executor as legacy_executor
from src.scheduled_research import models as legacy_models
from src.scheduling import triggers

_GOLDEN = json.loads((Path(__file__).parent / "golden_next_due.json").read_text())
_VECTORS = _GOLDEN["vectors"]

_DEFAULT_TZ_SENTINEL = "__DEFAULT__"


def _norm(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def _actual(fn, args):
    try:
        return {"ok": _norm(fn(*args))}
    except Exception as exc:  # noqa: BLE001 - golden records type + message verbatim
        return {"error": type(exc).__name__, "message": str(exc)}


def _as_set(values):
    return None if values is None else set(values)


def _call_args(vector):
    kind = vector["kind"]
    inputs = vector["inputs"]
    if kind in ("next_due", "next_cron_due"):
        if inputs["tz"] == _DEFAULT_TZ_SENTINEL:
            return (inputs["schedule"], inputs["after_ms"])
        return (inputs["schedule"], inputs["after_ms"], inputs["tz"])
    if kind == "local_wall":
        zone = timezone.utc if inputs["tz"] is None else ZoneInfo(inputs["tz"])
        return (date(*inputs["day"]), inputs["hour"], inputs["minute"], zone)
    if kind == "day_matches":
        return (
            date(*inputs["day"]),
            _as_set(inputs["doms"]),
            _as_set(inputs["months"]),
            _as_set(inputs["dows"]),
        )
    if kind == "parse_cron_field":
        return (inputs["part"], inputs["low"], inputs["high"])
    if kind in ("validate_schedule", "is_interval_schedule"):
        return (inputs["schedule"],)
    if kind in ("validate_timezone", "validate_timezone_shape"):
        return (inputs["tz"],)
    raise AssertionError(f"unknown vector kind: {kind}")


_KERNEL_FNS = {
    "next_due": lambda: triggers.next_due,
    "next_cron_due": lambda: triggers.next_cron_due,
    "local_wall": lambda: triggers.local_wall_time_to_epoch_ms,
    "day_matches": lambda: triggers.day_matches,
    "parse_cron_field": lambda: triggers.parse_cron_field,
    "validate_schedule": lambda: triggers.validate_schedule,
    "validate_timezone": lambda: triggers.validate_timezone,
    "validate_timezone_shape": lambda: triggers.validate_timezone_shape,
    "is_interval_schedule": lambda: triggers.is_interval_schedule,
}
_LEGACY_FNS = {
    "next_due": lambda: legacy_executor.next_due,
    "next_cron_due": lambda: legacy_executor._next_cron_due,
    "local_wall": lambda: legacy_executor._local_wall_time_to_epoch_ms,
    "day_matches": lambda: legacy_executor._day_matches,
    "parse_cron_field": lambda: legacy_models.parse_cron_field,
    "validate_schedule": lambda: legacy_models.validate_schedule,
    "validate_timezone": lambda: legacy_models.validate_timezone,
    "validate_timezone_shape": lambda: legacy_models.validate_timezone_shape,
    "is_interval_schedule": lambda: legacy_models.is_interval_schedule,
}
_CONSTS = {
    "CRON_BOUNDS": (
        lambda: [list(b) for b in triggers.CRON_BOUNDS],
        lambda: [list(b) for b in legacy_models.CRON_BOUNDS],
    ),
    "_CRON_SEARCH_LIMIT_DAYS": (
        lambda: triggers._CRON_SEARCH_LIMIT_DAYS,
        lambda: legacy_executor._CRON_SEARCH_LIMIT_DAYS,
    ),
}


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["id"] for v in _VECTORS])
def test_golden_vector_equivalence(vector):
    if vector["kind"] == "const":
        kernel_get, legacy_get = _CONSTS[vector["inputs"]["name"]]
        assert kernel_get() == vector["expect"]["ok"]
        assert legacy_get() == vector["expect"]["ok"]
        return
    args = _call_args(vector)
    assert _actual(_KERNEL_FNS[vector["kind"]](), args) == vector["expect"]
    assert _actual(_LEGACY_FNS[vector["kind"]](), args) == vector["expect"]


def test_golden_matrix_meets_plan_coverage():
    assert len(_VECTORS) >= 60
    kinds = Counter(v["kind"] for v in _VECTORS)
    assert kinds["next_due"] >= 40
    ids = {v["id"] for v in _VECTORS}
    required = {
        "next_due/cron-utc/weekday-2000-fri-to-mon",
        "next_due/cron-utc/every-15m-hour-rollover",
        "next_due/cron-utc/playbook-cadence-from-jan1",
        "next_due/cron-utc/weekday-0830-fri-to-mon",
        "next_due/cron-utc/new-year-midnight",
        "next_due/cron-utc/sunday-is-zero",
        "next_due/cron-utc/dow-7-rejected",
        "next_due/cron-utc/comma-range-mix-skips-2",
        "next_due/cron-utc/feb-31-window-exhausted",
        "next_due/cron-utc/feb-29-leap-day-2028",
        "next_due/cron-utc/reversed-range-rejected",
        "next_due/interval/basic-60000",
        "next_due/interval/basic-21600000",
        "next_due/interval/15-digit-upper-bound-ok",
        "next_due/interval/16-digit-rejected",
        "next_due/interval/advances-under-unresolvable-tz",
        "next_due/dst-ny/spring-gap-skipped",
        "next_due/dst-ny/fall-fold-first-occurrence",
        "next_due/dst-ny/fall-fold-fires-once",
        "next_due/dst-akl/spring-gap-skipped",
        "next_due/dst-akl/fall-fold-first-occurrence",
        "next_due/cron-utc/dom-dow-or-dow-wins",
        "next_due/cron-utc/dom-dow-or-dom-wins",
        "next_due/cron-utc/dom-wildcard-dow",
        "next_due/cron-utc/dow-wildcard-dom",
        "next_due/tz-sha/noon-local-from-utc-ref",
    }
    missing = required - ids
    assert not missing, f"golden matrix lost required plan dimensions: {sorted(missing)}"


@pytest.mark.parametrize(
    "name",
    [
        "next_due",
        "next_cron_due",
        "_next_cron_due",
        "local_wall_time_to_epoch_ms",
        "_local_wall_time_to_epoch_ms",
        "day_matches",
        "_day_matches",
        "_CRON_SEARCH_LIMIT_DAYS",
    ],
)
def test_executor_reexports_are_the_kernel_objects(name):
    assert getattr(legacy_executor, name) is getattr(triggers, name)


@pytest.mark.parametrize(
    "name",
    [
        "CRON_BOUNDS",
        "parse_cron_field",
        "validate_schedule",
        "validate_timezone",
        "validate_timezone_shape",
        "is_interval_schedule",
    ],
)
def test_models_reexports_are_the_kernel_objects(name):
    assert getattr(legacy_models, name) is getattr(triggers, name)


def test_underscore_aliases_are_the_public_functions():
    assert triggers._next_cron_due is triggers.next_cron_due
    assert triggers._local_wall_time_to_epoch_ms is triggers.local_wall_time_to_epoch_ms
    assert triggers._day_matches is triggers.day_matches
    assert triggers._CRON_SEARCH_LIMIT_DAYS == triggers.CRON_SEARCH_LIMIT_DAYS == 6 * 366 + 1


def test_dow_seven_still_rejected_by_kernel_and_legacy_path():
    for fn in (triggers.next_due, legacy_executor.next_due, triggers.validate_schedule):
        with pytest.raises(ValueError, match=r"cron field '7' is out of range; expected 0-6"):
            if fn is triggers.validate_schedule:
                fn("0 12 * * 7")
            else:
                fn("0 12 * * 7", 0, None)


class TestOnceTriggerKind:
    """Semantics copied from live/runtime/scheduler.py Job.interval_ms() (read-only
    reference; nothing imported from src.live and no live behavior changed)."""

    def test_once_spec_has_no_next_run(self):
        assert triggers.trigger_interval_ms("once") is None
        assert triggers.next_once_due("once", 1_000) is None

    def test_once_spec_is_case_insensitive_and_stripped(self):
        assert triggers.trigger_interval_ms("  ONCE ") is None
        assert triggers.next_once_due("Once", 1_000) is None

    def test_interval_spec_advances_one_interval_past_reference(self):
        assert triggers.trigger_interval_ms("interval:5000") == 5000
        assert triggers.next_once_due("interval:5000", 1_000) == 6_000

    def test_unusable_interval_spec_falls_back_to_default_cadence(self):
        for spec in ("interval:0", "interval:-5", "interval:abc", "interval:"):
            assert triggers.trigger_interval_ms(spec) == triggers.DEFAULT_TRIGGER_INTERVAL_MS

    def test_bare_or_unknown_spec_uses_default_cadence(self):
        for spec in ("", None, "garbage", "60000"):
            assert triggers.trigger_interval_ms(spec) == triggers.DEFAULT_TRIGGER_INTERVAL_MS
        assert triggers.next_once_due(None, 1_000) == 1_000 + triggers.DEFAULT_TRIGGER_INTERVAL_MS

    def test_default_cadence_value_mirrors_live_scheduler(self):
        assert triggers.DEFAULT_TRIGGER_INTERVAL_MS == 60 * 1000
        assert triggers.ONCE_SPEC == "once"
