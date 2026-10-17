"""Internal-dispatch execution semantics (scheduler unification T5).

Proves the plan's acceptance criteria (a)-(f) mechanically, with injected
clocks and Event-driven stuck callables (no real sleeps > 1s):

(a) a stuck internal run never blocks the serialized tick — other due jobs
    are still dispatched and ``sweep_deliveries`` still runs;
(b) the running job stays RUNNING and is not re-dispatched across ticks;
(c) the background task owns the terminal write (COMPLETED/FAILED,
    ``next_run_at`` advance, reload-skip, failure cap) and crash-restart
    interplay with ``recover_stale_running``;
(d) ``stop()`` is bounded against an in-flight ``to_thread`` task and the
    pinned cancel-not-kill semantics leave the job RUNNING;
(f) the production singleton from ``_get_scheduled_research_executor()``
    routes internal jobs through the registry branch (``failure_kind`` is
    not ``"dispatch"``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import JobStatus, ScheduledResearchJob
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling.registry import InternalCallableError, register_internal, resolve_internal


def _store(tmp_path: Path) -> ScheduledResearchJobStore:
    return ScheduledResearchJobStore(path=tmp_path / "jobs.json")


def _internal_job(
    job_id: str = "slow",
    *,
    callable_name: str = "test.slow",
    schedule: str = "1000",
    next_run_at: int = 1000,
    created_at: int = 0,
    **overrides: object,
) -> ScheduledResearchJob:
    return ScheduledResearchJob(
        id=job_id,
        prompt="",
        schedule=schedule,
        next_run_at=next_run_at,
        created_at=created_at,
        executor_kind="internal",
        internal_callable=callable_name,
        **overrides,  # type: ignore[arg-type]
    )


def _resolver(job: ScheduledResearchJob):
    """Production-shaped dispatcher: exact-key registry lookup, may raise."""
    return resolve_internal(job.internal_callable)


class _StuckCallable:
    """Sync callable that blocks a to_thread worker until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self.started.set()
        self.release.wait(10)
        self.finished.set()


async def _drain(executor: ScheduledResearchExecutor) -> None:
    """Await every in-flight internal background task (test-only reach-in)."""
    tasks = list(executor._internal._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)


# ---------------------------------------------------------------------------
# (a)+(b)+(c) non-blocking dispatch, no re-dispatch, background terminal write
# ---------------------------------------------------------------------------


def test_stuck_internal_run_does_not_block_tick_and_terminal_written_by_background(
    tmp_path: Path, clean_registry
) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("a-slow", callable_name="test.slow", next_run_at=1000))
    store.upsert(
        ScheduledResearchJob(
            id="b-other", prompt="p", schedule="1000", next_run_at=1000, created_at=0
        )
    )
    stuck = _StuckCallable()
    register_internal("test.slow", stuck)
    prompt_calls: list[str] = []
    clock = [1500]

    async def dispatch(job: ScheduledResearchJob) -> None:
        prompt_calls.append(job.id)

    sweep_results: list[int] = []

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, dispatch, internal_dispatcher=_resolver, now_fn=lambda: clock[0]
        )
        original_sweep = executor.sweep_deliveries

        async def counting_sweep() -> int:
            result = await original_sweep()
            sweep_results.append(result)
            return result

        executor.sweep_deliveries = counting_sweep  # type: ignore[method-assign]
        try:
            # Tick 1: spawns the stuck internal run AND dispatches the other
            # due job AND sweeps — all in the same tick, without waiting.
            await executor.tick(1500)
            assert await asyncio.to_thread(stuck.started.wait, 5), "to_thread worker never started"
            assert prompt_calls == ["b-other"], "tick was blocked by the internal run"
            assert len(sweep_results) == 1, "sweep_deliveries did not run in tick 1"
            running = store.get("a-slow")
            assert running is not None and running.status == JobStatus.RUNNING
            assert running.last_run_at is None  # terminal write not due yet

            # Tick 2 while still stuck: (b) no re-dispatch of the RUNNING job.
            await executor.tick(2500)
            assert stuck.calls == 1, "RUNNING internal job was re-dispatched"
            assert len(sweep_results) == 2, "sweep_deliveries did not run in tick 2"
            assert store.get("a-slow").status == JobStatus.RUNNING

            # (c) release: the BACKGROUND task owns the terminal write.
            clock[0] = 4000
            stuck.release.set()
            await _drain(executor)
        finally:
            stuck.release.set()

    asyncio.run(scenario())

    assert stuck.finished.is_set()
    saved = store.get("a-slow")
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.failure_kind is None
    assert saved.last_error is None
    assert saved.consecutive_failures == 0
    # Completion-time bookkeeping: last_run_at/advance use the completion
    # clock (4000), so a run longer than its interval cannot re-fire at once.
    assert saved.last_run_at == 4000
    assert saved.next_run_at == 5000
    # Internal runs produce no session: the delivery outbox is never armed.
    assert not saved.delivery.session_id
    assert prompt_calls == ["b-other", "b-other"]  # tick 2 fired b-other again


def test_async_internal_callable_is_awaited_directly(tmp_path: Path, clean_registry) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("async-job", callable_name="test.async"))
    done = asyncio.Event()
    calls: list[str] = []

    async def fake_async() -> None:
        calls.append("ran")
        done.set()

    register_internal("test.async", fake_async)
    clock = [1500]

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: clock[0]
        )
        await executor.tick(1500)
        await asyncio.wait_for(done.wait(), timeout=5)
        await _drain(executor)

    asyncio.run(scenario())

    assert calls == ["ran"]
    saved = store.get("async-job")
    assert saved is not None and saved.status == JobStatus.COMPLETED
    assert saved.next_run_at == 2500


# ---------------------------------------------------------------------------
# (c) failure bookkeeping: retry, cap, success-resets
# ---------------------------------------------------------------------------


def test_internal_failure_retries_then_caps_like_the_prompt_path(
    tmp_path: Path, clean_registry
) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("bad", callable_name="test.bad"))

    def boom() -> None:
        raise RuntimeError("boom")

    register_internal("test.bad", boom)
    clock = [1500]

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store,
            lambda _j: None,
            internal_dispatcher=_resolver,
            now_fn=lambda: clock[0],
            max_consecutive_failures=2,
            retry_base_delay_ms=1000,
            retry_max_delay_ms=100_000,
        )
        await executor.tick(1500)
        await _drain(executor)

        first = store.get("bad")
        assert first is not None
        assert first.status == JobStatus.PENDING  # retryable, 1/2
        assert first.failure_kind == "dispatch"
        assert first.consecutive_failures == 1
        assert "boom" in (first.last_error or "")
        # max(next_due=2500, completion 1500 + retry delay 1000) = 2500
        assert first.next_run_at == 2500

        clock[0] = first.next_run_at
        await executor.tick(clock[0])
        await _drain(executor)

    asyncio.run(scenario())

    second = store.get("bad")
    assert second is not None
    assert second.status == JobStatus.FAILED  # cap reached, terminal
    assert second.failure_kind == "dispatch"
    assert second.consecutive_failures == 2


def test_internal_success_resets_consecutive_failures(tmp_path: Path, clean_registry) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("flaky", callable_name="test.ok", consecutive_failures=1))
    register_internal("test.ok", lambda: None)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        await _drain(executor)

    asyncio.run(scenario())

    saved = store.get("flaky")
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.consecutive_failures == 0
    assert saved.failure_kind is None


# ---------------------------------------------------------------------------
# Fail-closed: missing dispatcher / tampered name → dispatch-failure path
# ---------------------------------------------------------------------------


def test_internal_job_without_dispatcher_fails_closed(tmp_path: Path) -> None:
    """T4 default preserved: None dispatcher → dispatch-failure path."""
    store = _store(tmp_path)
    store.upsert(_internal_job("orphan", callable_name="test.whatever"))
    prompt_calls: list[str] = []

    async def dispatch(job: ScheduledResearchJob) -> None:
        prompt_calls.append(job.id)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(store, dispatch, now_fn=lambda: 1500)
        await executor.tick(1500)
        assert not executor._internal._tasks  # resolve failed synchronously

    asyncio.run(scenario())

    saved = store.get("orphan")
    assert saved is not None
    assert saved.status == JobStatus.PENDING
    assert saved.failure_kind == "dispatch"
    assert "internal dispatcher is not configured" in (saved.last_error or "")
    assert prompt_calls == []  # prompt dispatch never called for internal jobs


def test_tampered_callable_name_takes_dispatch_failure_path(
    tmp_path: Path, clean_registry
) -> None:
    """Break-probe as a test: a job file tampered to name a real import path
    (a stdlib process-spawning function) is rejected by the allowlist; the
    prompt dispatch is never called and nothing outside the registry runs."""
    store = _store(tmp_path)
    store.upsert(_internal_job("tampered", callable_name="os.system"))
    prompt_calls: list[str] = []

    async def dispatch(job: ScheduledResearchJob) -> None:
        prompt_calls.append(job.id)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, dispatch, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)

    asyncio.run(scenario())

    saved = store.get("tampered")
    assert saved is not None
    assert saved.status == JobStatus.PENDING
    assert saved.failure_kind == "dispatch"
    assert "not registered" in (saved.last_error or "")
    assert prompt_calls == []


# ---------------------------------------------------------------------------
# (c) reload-skip semantics + crash-restart interplay
# ---------------------------------------------------------------------------


def test_internal_job_deleted_while_running_is_not_resurrected(
    tmp_path: Path, clean_registry
) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("doomed", callable_name="test.slow"))
    stuck = _StuckCallable()
    register_internal("test.slow", stuck)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        assert await asyncio.to_thread(stuck.started.wait, 5)
        jobs = store.load()
        jobs.pop("doomed")
        store.save(jobs)  # user DELETE while the run is in flight
        stuck.release.set()
        await _drain(executor)

    try:
        asyncio.run(scenario())
    finally:
        stuck.release.set()

    assert stuck.finished.is_set()
    assert store.get("doomed") is None  # completion write skipped, no resurrection


def test_internal_job_replaced_while_running_skips_completion_write(
    tmp_path: Path, clean_registry
) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("replaced", callable_name="test.slow", created_at=0))
    stuck = _StuckCallable()
    register_internal("test.slow", stuck)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        assert await asyncio.to_thread(stuck.started.wait, 5)
        store.upsert(
            _internal_job(
                "replaced",
                callable_name="test.slow",
                created_at=999,
                status=JobStatus.PENDING,
                next_run_at=10**15,
            )
        )
        stuck.release.set()
        await _drain(executor)

    try:
        asyncio.run(scenario())
    finally:
        stuck.release.set()

    saved = store.get("replaced")
    assert saved is not None
    assert saved.created_at == 999  # the replacement owns its lifecycle
    assert saved.status == JobStatus.PENDING
    assert saved.last_run_at is None


def test_crash_midrun_recovers_running_on_next_start(tmp_path: Path, clean_registry) -> None:
    store = _store(tmp_path)
    store.upsert(_internal_job("crashed", callable_name="test.slow"))
    stuck = _StuckCallable()
    register_internal("test.slow", stuck)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        assert await asyncio.to_thread(stuck.started.wait, 5)
        # Simulate the process dying mid-run: cancel the task (as stop() does)
        # WITHOUT any terminal write, then let the thread finish harmlessly.
        await asyncio.wait_for(executor.stop(), timeout=2)
        stuck.release.set()
        assert await asyncio.to_thread(stuck.finished.wait, 5)
        # The discarded thread result must NOT have written a terminal state.
        abandoned = store.get("crashed")
        assert abandoned is not None and abandoned.status == JobStatus.RUNNING
        assert abandoned.last_run_at is None

    try:
        asyncio.run(scenario())
    finally:
        stuck.release.set()

    # Restart: a fresh executor recovers the stale RUNNING row to PENDING.
    executor2 = ScheduledResearchExecutor(
        store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
    )
    assert executor2.recover_stale_running() == 1
    recovered = store.get("crashed")
    assert recovered is not None and recovered.status == JobStatus.PENDING


# ---------------------------------------------------------------------------
# (d) bounded stop() with an in-flight to_thread task
# ---------------------------------------------------------------------------


def test_stop_is_bounded_and_leaves_inflight_job_running(tmp_path: Path, clean_registry) -> None:
    """Pinned shutdown semantics: cancel the asyncio TASK, never the thread.

    ``stop()`` returns within the wait_for bound while the sync callable is
    still blocked in its to_thread worker; the job is left RUNNING (no
    terminal write, no failure count) for stale-recovery on the next start,
    and the thread later finishes harmlessly without changing the store.
    """
    store = _store(tmp_path)
    store.upsert(_internal_job("stuck", callable_name="test.slow"))
    stuck = _StuckCallable()
    register_internal("test.slow", stuck)

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(
            store, lambda _j: None, internal_dispatcher=_resolver, now_fn=lambda: 1500
        )
        await executor.tick(1500)
        assert await asyncio.to_thread(stuck.started.wait, 5)
        await asyncio.wait_for(executor.stop(), timeout=2.0)  # bounded
        assert not executor._internal._tasks  # task cancelled and drained
        assert not stuck.finished.is_set()  # thread still running, unharmed
        stuck.release.set()
        assert await asyncio.to_thread(stuck.finished.wait, 5)  # finishes harmlessly, result discarded

    try:
        asyncio.run(scenario())
    finally:
        stuck.release.set()

    saved = store.get("stuck")
    assert saved is not None
    assert saved.status == JobStatus.RUNNING
    assert saved.last_run_at is None
    assert saved.consecutive_failures == 0


# ---------------------------------------------------------------------------
# (f) production wiring: the singleton executor runs registry callables
# ---------------------------------------------------------------------------


def test_production_resolver_resolves_registered_and_rejects_tampered(clean_registry) -> None:
    from src.scheduled_research.internal_dispatch import resolve_scheduled_internal_callable

    register_internal("test.prod", lambda: None)
    job = _internal_job("j", callable_name="test.prod")
    assert resolve_internal("test.prod") is resolve_scheduled_internal_callable(job)
    tampered = _internal_job("j", callable_name="os.system")
    with pytest.raises(InternalCallableError):
        resolve_scheduled_internal_callable(tampered)


def test_production_singleton_routes_internal_jobs_through_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_registry
) -> None:
    """(f) Mechanical proof the production construction site is wired.

    The executor is obtained ONLY through ``_get_scheduled_research_executor()``
    (the singleton the API server uses), with the store redirected to a temp
    file; an internal job must go through the registry branch and its
    ``failure_kind`` must not be ``"dispatch"``.
    """
    from src.api import scheduled_routes

    isolated = ScheduledResearchJobStore(path=tmp_path / "prod_jobs.json")
    monkeypatch.setattr(scheduled_routes, "_scheduled_research_store", isolated)
    monkeypatch.setattr(scheduled_routes, "_scheduled_research_executor", None)

    calls: list[str] = []

    def prod_callable() -> None:
        calls.append("ran")

    register_internal("test.prod", prod_callable)
    now = int(time.time() * 1000)
    isolated.upsert(_internal_job("prod-job", callable_name="test.prod", next_run_at=now - 1000))

    executor = scheduled_routes._get_scheduled_research_executor()
    assert executor._internal_dispatcher is not None  # grep-gate companion

    async def scenario() -> None:
        await executor.tick(now)
        await _drain(executor)

    asyncio.run(scenario())

    assert calls == ["ran"]
    saved = isolated.get("prod-job")
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.failure_kind != "dispatch"
    assert saved.failure_kind is None
