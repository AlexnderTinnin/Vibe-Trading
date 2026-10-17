"""Protected system jobs (scheduler unification T6).

Covers the plan's acceptance criteria and QA trio mechanically:

* idempotence — two ``ensure_system_jobs`` passes leave identical rows and
  the second pass performs NO write (store bytes unchanged);
* enabled/disabled branches — spec-level ``enabled_predicate`` and the
  caller-level override; disabled with no row writes NOTHING (QA (a));
  disabled with an existing runnable row halts it to CANCELLED so the
  double-gate enablement chain (iv) holds in the flag-off direction too;
* ``is_system_job_id`` — prefix convention, incl. the playbook-id false case;
* tamper correction — ``executor_kind``/``source_type``/``internal_callable``
  (QA (b)); valid edited schedule preserved (β local trust), invalid
  schedule repaired to the spec default; (α) CANCELLED/EXPIRED reset to
  PENDING with ``end_at`` cleared; manual deletion re-seeded (iii);
  runtime fields preserved across re-ensure;
* end-to-end through the executor — a seeded system job due → the registry
  fake callable runs exactly once via the internal branch (T5 supervisor),
  prompt dispatch never called; an unregistered callable FAILS with
  ``failure_kind="dispatch"`` and a readable ``last_error`` (QA (c)) — not
  silently skipped, not prompt-dispatched — while ``ensure_system_jobs``
  itself seeds without requiring registry membership.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import (
    DeliveryRecord,
    DeliveryStatus,
    JobStatus,
    ScheduledResearchJob,
)
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling import system_jobs as sj
from src.scheduling.registry import register_internal, resolve_internal
from src.scheduling.system_jobs import (
    SYSTEM_JOB_ID_PREFIX,
    EnsureSystemJobsReport,
    SystemJobSpec,
    ensure_system_jobs,
    is_system_job_id,
    system_job_specs,
)


def _store(tmp_path: Path) -> ScheduledResearchJobStore:
    return ScheduledResearchJobStore(path=tmp_path / "jobs.json")


def _spec(**overrides: object) -> SystemJobSpec:
    base: dict = {
        "id": "system:test-job",
        "schedule": "1000",
        "internal_callable": "test.fake",
        "title": "Test system job",
    }
    base.update(overrides)
    return SystemJobSpec(**base)  # type: ignore[arg-type]


def _resolver(job: ScheduledResearchJob):
    """Production-shaped dispatcher: exact-key registry lookup, may raise."""
    return resolve_internal(job.internal_callable)


async def _drain(executor: ScheduledResearchExecutor) -> None:
    """Await every in-flight internal background task (test-only reach-in)."""
    tasks = list(executor._internal._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)


def _mutate_row(store: ScheduledResearchJobStore, job_id: str, **changes: object) -> None:
    """Simulate a manual store-file edit (bypasses upsert validation)."""
    jobs = store.load()
    job = jobs[job_id]
    for key, value in changes.items():
        setattr(job, key, value)
    store.save(jobs)


# ---------------------------------------------------------------------------
# is_system_job_id — the prefix predicate T7 consumes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_id",
    ["system:memory-consolidation", "system:br-data-sync", "system:", "system:x" * 30],
)
def test_is_system_job_id_true_for_prefix(job_id: str) -> None:
    assert is_system_job_id(job_id) is True


@pytest.mark.parametrize(
    "job_id",
    [
        "playbook-premarket-brief-abcd1234",  # plan's named false case
        "sr-0123456789ab",
        "systemic-job",  # 'system' without the colon is NOT the prefix
        "System:memory-consolidation",  # case-sensitive
        "",
        None,
        123,
    ],
)
def test_is_system_job_id_false_for_everything_else(job_id: object) -> None:
    assert is_system_job_id(job_id) is False


def test_prefix_constant_contains_colon_for_safe_id_re_interplay() -> None:
    """the prefix MUST contain ':' so SAFE_JOB_ID_RE (^[A-Za-z0-9_-]{1,128}$)
    rejects every system id at POST/path-param level — structural anti-forgery."""
    assert SYSTEM_JOB_ID_PREFIX == "system:"
    assert ":" in SYSTEM_JOB_ID_PREFIX


# ---------------------------------------------------------------------------
# Spec surface + validation
# ---------------------------------------------------------------------------


def test_production_specs_are_a_tuple_of_wellformed_specs() -> None:
    specs = system_job_specs()
    assert isinstance(specs, tuple)
    for spec in specs:  # empty until T8/T9 append; the loop pins the invariant
        assert is_system_job_id(spec.id)
        assert spec.source_type == "system"
        assert spec.executor_kind == "internal"
        assert spec.prompt == ""
        assert spec.internal_callable


def test_ensure_consumes_system_job_specs_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8/T9 edit surface: appending to _SYSTEM_JOB_SPECS is enough — the
    default ensure path (no specs kwarg, as T12 will call it) picks it up."""
    store = _store(tmp_path)
    spec = _spec()
    monkeypatch.setattr(sj, "_SYSTEM_JOB_SPECS", (spec,))
    report = ensure_system_jobs(store, now_fn=lambda: 1000)
    assert report.seeded == (spec.id,)
    assert store.get(spec.id) is not None


def test_spec_defaults_are_system_and_internal() -> None:
    spec = _spec()
    assert spec.source_type == "system"
    assert spec.executor_kind == "internal"
    assert spec.prompt == ""
    assert spec.timezone is None
    assert spec.enabled_predicate() is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "memory-consolidation"},  # missing prefix
        {"id": "system:"},  # prefix only, no name
        {"id": ""},
        {"source_type": "prompt"},
        {"executor_kind": "prompt"},
        {"internal_callable": ""},
        {"schedule": "not a schedule!!!"},
        {"schedule": ""},
    ],
)
def test_spec_rejects_malformed_definitions(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _spec(**overrides)


def test_spec_rejects_non_callable_predicate() -> None:
    with pytest.raises(TypeError):
        _spec(enabled_predicate="yes")


# ---------------------------------------------------------------------------
# Seeding: enabled / disabled branches (QA (a))
# ---------------------------------------------------------------------------


def test_enabled_spec_seeds_system_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec(schedule="2000")
    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 5000)
    assert report.seeded == (spec.id,)
    job = store.get(spec.id)
    assert job is not None
    assert job.source_type == "system"
    assert job.executor_kind == "internal"
    assert job.internal_callable == "test.fake"
    assert job.prompt == ""
    assert job.status == JobStatus.PENDING
    assert job.schedule == "2000"
    assert job.next_run_at == 5000  # interval schedule: due immediately
    assert job.created_at == 5000
    assert job.end_at is None
    assert job.delivery_channel is None  # no delivery for this batch (plan)


def test_seeded_cron_with_timezone_waits_for_next_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec(schedule="0 3 * * *", timezone="UTC")
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 0)
    job = store.get(spec.id)
    assert job is not None
    assert job.next_run_at > 0  # next 03:00 UTC slot, not "now"
    assert job.timezone == "UTC"


def test_disabled_spec_writes_nothing(tmp_path: Path) -> None:
    """QA (a): flag false → NO system job written — strongest sense: the
    store file is never even created."""
    store = _store(tmp_path)
    spec = _spec()
    report = ensure_system_jobs(store, specs=[spec], enabled_predicate=lambda _s: False)
    assert report.skipped_disabled == (spec.id,)
    assert report.seeded == ()
    assert store.load() == {}
    assert not store.path.exists()


def test_spec_level_enabled_predicate_gates_seeding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    off = _spec(id="system:off", enabled_predicate=lambda: False)
    on = _spec(id="system:on", enabled_predicate=lambda: True)
    report = ensure_system_jobs(store, specs=[off, on], now_fn=lambda: 1000)
    assert report.seeded == ("system:on",)
    assert report.skipped_disabled == ("system:off",)
    assert store.get("system:on") is not None
    assert store.get("system:off") is None


# ---------------------------------------------------------------------------
# Idempotence + runtime-field preservation
# ---------------------------------------------------------------------------


def test_two_passes_are_idempotent_with_zero_rewrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    first_bytes = store.path.read_bytes()
    first = store.get(spec.id).to_dict()

    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 999_999)
    assert report.unchanged == (spec.id,)
    assert report.seeded == () and report.repaired == ()
    assert store.path.read_bytes() == first_bytes  # conformant row NOT written
    assert store.get(spec.id).to_dict() == first
    assert len(store.load()) == 1


def test_runtime_fields_preserved_across_reensure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    delivery = DeliveryRecord(status=DeliveryStatus.SENT, session_id="s-1", key="k")
    _mutate_row(
        store,
        spec.id,
        next_run_at=777,
        last_run_at=555,
        consecutive_failures=2,
        last_error="earlier boom",
        failure_kind="dispatch",
        status=JobStatus.COMPLETED,
        created_at=111,
        config={"k": 1},
        title="user-renamed",
        prompt="user-typed text",
        delivery_channel="email",
        delivery=delivery,
    )
    before = store.get(spec.id).to_dict()

    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 888_888)
    assert report.unchanged == (spec.id,)
    after = store.get(spec.id).to_dict()
    assert after == before  # every field, byte for byte
    job = store.get(spec.id)
    assert job.status == JobStatus.COMPLETED  # live status untouched
    assert job.next_run_at == 777 and job.last_run_at == 555
    assert job.consecutive_failures == 2 and job.failure_kind == "dispatch"
    assert job.delivery.status == DeliveryStatus.SENT and job.delivery.session_id == "s-1"


# ---------------------------------------------------------------------------
# Tamper correction (QA (b)) + (β) schedule boundary + (α) status reset
# ---------------------------------------------------------------------------


def test_tampered_executor_kind_prompt_is_corrected(tmp_path: Path) -> None:
    """QA (b): user hand-edits executor_kind to 'prompt' → corrected to
    'internal' (and the callable key restored), so the row can never be
    prompt-dispatched again."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(
        store,
        spec.id,
        source_type="prompt",
        executor_kind="prompt",
        internal_callable=None,
    )

    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 2000)
    assert report.repaired == (spec.id,)
    job = store.get(spec.id)
    assert job.executor_kind == "internal"
    assert job.source_type == "system"
    assert job.internal_callable == "test.fake"
    assert job.next_run_at == 1000  # runtime fields still untouched


def test_tampered_callable_key_is_corrected_to_spec(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, internal_callable="os.system")

    ensure_system_jobs(store, specs=[spec])
    assert store.get(spec.id).internal_callable == "test.fake"


def test_valid_edited_schedule_is_preserved_beta_local_trust(tmp_path: Path) -> None:
    """(ii-c) choice (β): a syntactically VALID user-edited schedule is
    trusted local configuration and left alone — with its next_run_at."""
    store = _store(tmp_path)
    spec = _spec(schedule="1000")
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, schedule="5000", next_run_at=4242)

    report = ensure_system_jobs(store, specs=[spec])
    assert report.unchanged == (spec.id,)
    job = store.get(spec.id)
    assert job.schedule == "5000"
    assert job.next_run_at == 4242


def test_invalid_schedule_is_repaired_to_spec_default(tmp_path: Path) -> None:
    """The other side of the (β) boundary: an INVALID schedule must be
    repaired, else the validate=False repair write would persist garbage."""
    store = _store(tmp_path)
    spec = _spec(schedule="1000")
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, schedule="every tuesday maybe")

    report = ensure_system_jobs(store, specs=[spec])
    assert report.repaired == (spec.id,)
    job = store.get(spec.id)
    assert job.schedule == "1000"
    assert job.next_run_at == 1000  # runtime field still preserved


@pytest.mark.parametrize("status", [JobStatus.CANCELLED, JobStatus.EXPIRED])
def test_alpha_reset_cancelled_and_expired_to_pending(tmp_path: Path, status: JobStatus) -> None:
    """(α): CANCELLED/EXPIRED can only come from manual file edits (post-T7)
    or our own flag-off halt — reset to PENDING and clear end_at."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, status=status, end_at=12345)

    report = ensure_system_jobs(store, specs=[spec])
    assert report.repaired == (spec.id,)
    job = store.get(spec.id)
    assert job.status == JobStatus.PENDING
    assert job.end_at is None


def test_hand_added_future_end_at_is_cleared_while_pending(tmp_path: Path) -> None:
    """Same tamper class as (α): specs define no end boundary, so a manually
    added end_at that would expire the job mid-life is removed."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, end_at=10**15)

    ensure_system_jobs(store, specs=[spec])
    assert store.get(spec.id).end_at is None


def test_manual_deletion_is_reseeded(tmp_path: Path) -> None:
    """(iii) repair-replay: hand-deleting the row does not stick."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    assert store.delete(spec.id) is True

    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 2000)
    assert report.seeded == (spec.id,)
    job = store.get(spec.id)
    assert job is not None and job.source_type == "system"
    assert job.created_at == 2000  # genuinely fresh row


# ---------------------------------------------------------------------------
# Flag-off halt + re-enable round trip (enablement chain (iv))
# ---------------------------------------------------------------------------


def test_disabled_flag_halts_existing_row_and_reenable_revives_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, last_run_at=900, consecutive_failures=1)

    off = ensure_system_jobs(store, specs=[spec], enabled_predicate=lambda _s: False)
    assert off.halted == (spec.id,)
    job = store.get(spec.id)
    assert job.status == JobStatus.CANCELLED  # never due again while off
    assert job.last_run_at == 900 and job.consecutive_failures == 1  # non-destructive
    assert job.executor_kind == "internal"  # spec-owned fields still correct

    # Idempotent while still disabled: no further write.
    halted_bytes = store.path.read_bytes()
    off2 = ensure_system_jobs(store, specs=[spec], enabled_predicate=lambda _s: False)
    assert off2.halted == (spec.id,)
    assert store.path.read_bytes() == halted_bytes

    # Flag back on: the (α) reset revives the row.
    on = ensure_system_jobs(store, specs=[spec])
    assert on.repaired == (spec.id,)
    job = store.get(spec.id)
    assert job.status == JobStatus.PENDING
    assert job.last_run_at == 900  # history preserved across the round trip


def test_disabled_flag_leaves_terminal_rows_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, status=JobStatus.FAILED, consecutive_failures=3)
    before = store.path.read_bytes()

    report = ensure_system_jobs(store, specs=[spec], enabled_predicate=lambda _s: False)
    assert report.halted == (spec.id,)
    assert store.path.read_bytes() == before  # FAILED is already never-due


def test_enabled_spec_resets_failed_row_and_clears_retry_budget(tmp_path: Path) -> None:
    """Retry-budget reset: FAILED is reachable at runtime through the
    executor's own retry exhaustion, and no API surface may resurrect a
    system row — the startup reset is the only recovery path, so a transient
    multi-day outage cannot permanently brick a product-owned job."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(
        store,
        spec.id,
        status=JobStatus.FAILED,
        consecutive_failures=3,
        last_error="boom",
        failure_kind="dispatch",
        end_at=12345,
    )

    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 2000)
    assert report.repaired == (spec.id,)
    job = store.get(spec.id)
    assert job.status == JobStatus.PENDING
    assert job.end_at is None
    assert job.consecutive_failures == 0
    assert job.last_error is None
    assert job.failure_kind is None
    assert job.next_run_at == 1000  # other runtime fields still untouched

    # Idempotent: the conformant PENDING row is not rewritten on a second pass.
    before = store.path.read_bytes()
    report2 = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 3000)
    assert report2.unchanged == (spec.id,)
    assert store.path.read_bytes() == before


def test_disabled_flag_still_corrects_tampered_fields(tmp_path: Path) -> None:
    """A disabled row tampered to executor_kind='prompt' must not stay a
    prompt-dispatchable row even while the feature flag is off."""
    store = _store(tmp_path)
    spec = _spec()
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    _mutate_row(store, spec.id, executor_kind="prompt", internal_callable=None)

    ensure_system_jobs(store, specs=[spec], enabled_predicate=lambda _s: False)
    job = store.get(spec.id)
    assert job.executor_kind == "internal"
    assert job.internal_callable == "test.fake"
    assert job.status == JobStatus.CANCELLED


# ---------------------------------------------------------------------------
# End-to-end through the executor (QA happy + QA (c))
# ---------------------------------------------------------------------------


def test_seeded_system_job_runs_registry_callable_once_via_internal_branch(
    tmp_path: Path, clean_registry
) -> None:
    """QA happy: enabled → seeded → due → the registry fake callable is
    invoked exactly once through the T5 supervisor path; the prompt dispatch
    callback is never called for it."""
    store = _store(tmp_path)
    calls: list[int] = []
    register_internal("test.fake", lambda: calls.append(1))
    spec = _spec(schedule="1000")
    ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    prompt_calls: list[str] = []

    async def dispatch(job: ScheduledResearchJob) -> None:
        prompt_calls.append(job.id)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, dispatch, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        await _drain(executor)

    asyncio.run(scenario())

    assert calls == [1]
    assert prompt_calls == []
    saved = store.get(spec.id)
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.failure_kind is None
    assert saved.last_run_at == 1500
    assert saved.next_run_at == 2500  # advanced from completion time


def test_unregistered_callable_seeds_then_fails_as_dispatch(
    tmp_path: Path, clean_registry
) -> None:
    """QA (c): ensure seeds WITHOUT requiring registry membership; at
    execution the unregistered key fails closed — failure_kind='dispatch',
    readable last_error, retryable PENDING — never silently skipped, never
    prompt-dispatched."""
    store = _store(tmp_path)
    spec = _spec(internal_callable="test.never.registered")
    report = ensure_system_jobs(store, specs=[spec], now_fn=lambda: 1000)
    assert report.seeded == (spec.id,)  # registry empty, seeding still works
    prompt_calls: list[str] = []

    async def dispatch(job: ScheduledResearchJob) -> None:
        prompt_calls.append(job.id)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, dispatch, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        assert not executor._internal._tasks  # resolve failed synchronously

    asyncio.run(scenario())

    saved = store.get(spec.id)
    assert saved is not None
    assert saved.status == JobStatus.PENDING  # retryable first failure
    assert saved.failure_kind == "dispatch"
    assert saved.consecutive_failures == 1
    assert "not registered" in (saved.last_error or "")
    assert "test.never.registered" in (saved.last_error or "")
    assert prompt_calls == []


# ---------------------------------------------------------------------------
# Report shape + module purity
# ---------------------------------------------------------------------------


def test_report_is_frozen_with_tuple_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = ensure_system_jobs(store, specs=[])
    assert isinstance(report, EnsureSystemJobsReport)
    assert report == EnsureSystemJobsReport()
    for value in (
        report.seeded,
        report.repaired,
        report.unchanged,
        report.skipped_disabled,
        report.halted,
    ):
        assert isinstance(value, tuple)
    with pytest.raises(Exception):  # frozen dataclass
        report.seeded = ("x",)  # type: ignore[misc]


def test_system_jobs_module_reads_no_environment() -> None:
    """The env surface belongs to T8/T9: this module only consumes injected
    predicates (the ci_env_var_gate AST walk backs this at CI level)."""
    source = Path(sj.__file__).read_text(encoding="utf-8")
    for token in ("os.environ", "getenv", "get_env_config", "environ["):
        assert token not in source
