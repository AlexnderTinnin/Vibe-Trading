"""Tests for the generic scheduler kernel (src/scheduling/kernel.py) and the
shared vocabulary types (src/scheduling/types.py), scheduler-unification T2.

House style (see test_runtime_scheduler.py): async scenarios are driven with
``asyncio.run`` — no pytest-asyncio in this repo — with an injected ``now_fn``
instead of the wall clock, ``asyncio.sleep(0)`` yield loops or ``asyncio.Event``
handshakes instead of real waiting, and ``asyncio.wait_for`` timeouts so a wedged
kernel fails the test instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time

import pytest

from src.scheduling.kernel import (
    DEFAULT_TASK_NAME,
    SchedulerKernel,
    _now_ms,
)
from src.scheduling.types import (
    EXECUTOR_KINDS,
    is_executor_kind,
)

# ---- happy path: tick ordering with an injected clock ----------------------


def test_three_ticks_run_in_order_with_injected_clock() -> None:
    """QA happy: 3 ticks, on_tick receives strictly increasing now_ms."""
    ticks: list[int] = []
    clock = itertools.count(1000, 1000)

    async def scenario() -> None:
        done = asyncio.Event()

        async def on_tick(now_ms: int) -> None:
            ticks.append(now_ms)
            if len(ticks) >= 3:
                done.set()

        kernel = SchedulerKernel(tick_interval_ms=1, now_fn=lambda: next(clock), on_tick=on_tick)
        kernel.start()
        await asyncio.wait_for(done.wait(), timeout=5.0)
        assert kernel.is_running
        await asyncio.wait_for(kernel.stop(), timeout=5.0)

    asyncio.run(scenario())

    assert len(ticks) >= 3
    assert ticks[:3] == [1000, 2000, 3000]
    assert all(b > a for a, b in zip(ticks, ticks[1:]))


def test_start_stop_roundtrip_is_idempotent() -> None:
    async def scenario() -> None:
        ticks: list[int] = []

        async def on_tick(now_ms: int) -> None:
            ticks.append(now_ms)

        kernel = SchedulerKernel(tick_interval_ms=5, on_tick=on_tick)
        kernel.start()
        kernel.start()  # idempotent: still exactly one task
        assert kernel.is_running
        await asyncio.sleep(0.02)
        await asyncio.wait_for(kernel.stop(), timeout=5.0)
        assert not kernel.is_running
        assert kernel._task is None
        assert ticks  # the loop really ticked
        await asyncio.wait_for(kernel.stop(), timeout=5.0)  # idempotent

    asyncio.run(scenario())


def test_task_name_is_injectable() -> None:
    async def scenario() -> None:
        kernel = SchedulerKernel(tick_interval_ms=60_000, task_name="my-kernel")
        kernel.start()
        assert kernel._task is not None
        assert kernel._task.get_name() == "my-kernel"
        await asyncio.wait_for(kernel.stop(), timeout=5.0)

        default = SchedulerKernel(tick_interval_ms=60_000)
        default.start()
        assert default._task is not None
        assert default._task.get_name() == DEFAULT_TASK_NAME == "scheduler-kernel"
        await asyncio.wait_for(default.stop(), timeout=5.0)

    asyncio.run(scenario())


def test_disabled_kernel_start_and_stop_are_noops() -> None:
    async def scenario() -> None:
        called: list[int] = []

        async def on_tick(now_ms: int) -> None:
            called.append(now_ms)

        kernel = SchedulerKernel(enabled=False, on_tick=on_tick)
        kernel.start()
        assert not kernel.is_running
        await asyncio.wait_for(kernel.stop(), timeout=0.5)
        assert called == []

    asyncio.run(scenario())


def test_default_now_fn_is_wall_clock_ms() -> None:
    before = time.time() * 1000
    assert abs(_now_ms() - before) < 2000
    assert SchedulerKernel()._now_fn is _now_ms


# ---- wakeup shortens the inter-tick sleep ----------------------------------


def test_request_wake_shortens_a_long_sleep() -> None:
    """Without the wake the second tick would be a full 60s away."""
    ticks: list[int] = []

    async def scenario() -> None:
        second = asyncio.Event()

        async def on_tick(now_ms: int) -> None:
            ticks.append(now_ms)
            if len(ticks) >= 2:
                second.set()

        kernel = SchedulerKernel(tick_interval_ms=60_000, on_tick=on_tick)
        kernel.start()
        for _ in range(1000):
            await asyncio.sleep(0)
            if ticks:
                break
        assert ticks, "first tick did not run"
        kernel.request_wake()
        await asyncio.wait_for(second.wait(), timeout=2.0)
        await asyncio.wait_for(kernel.stop(), timeout=5.0)

    asyncio.run(scenario())

    assert len(ticks) >= 2


def test_request_wake_from_another_thread_wakes_the_loop() -> None:
    """The real cross-thread path: call_soon_threadsafe onto the running loop."""
    ticks: list[int] = []

    async def scenario() -> None:
        second = asyncio.Event()

        async def on_tick(now_ms: int) -> None:
            ticks.append(now_ms)
            if len(ticks) >= 2:
                second.set()

        kernel = SchedulerKernel(tick_interval_ms=60_000, on_tick=on_tick)
        kernel.start()
        for _ in range(1000):
            await asyncio.sleep(0)
            if ticks:
                break
        assert ticks, "first tick did not run"
        thread = threading.Thread(target=kernel.request_wake)
        thread.start()
        await asyncio.wait_for(second.wait(), timeout=2.0)
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        await asyncio.wait_for(kernel.stop(), timeout=5.0)

    asyncio.run(scenario())

    assert len(ticks) >= 2


# ---- request_wake: no-loop no-op, coalescing, closed-loop tolerance ---------


class _RecordingLoop:
    """Stand-in exposing exactly the loop surface request_wake touches."""

    def __init__(self, *, running: bool = True, raise_on_schedule: bool = False) -> None:
        self._running = running
        self._raise_on_schedule = raise_on_schedule
        self.scheduled: list[tuple] = []

    def is_running(self) -> bool:
        return self._running

    def call_soon_threadsafe(self, fn, *args) -> None:
        if self._raise_on_schedule:
            raise RuntimeError("Event loop is closed")
        self.scheduled.append((fn, args))


def test_request_wake_without_a_running_loop_is_a_noop() -> None:
    """Construction happens before the owner's loop exists."""
    kernel = SchedulerKernel()

    kernel.request_wake()  # must not raise

    assert kernel._loop is None
    assert kernel._wakeup is None


def test_request_wake_coalesces_and_tolerates_a_closed_loop() -> None:
    async def scenario() -> None:
        kernel = SchedulerKernel()
        loop = _RecordingLoop()
        kernel._loop = loop
        kernel._wakeup = asyncio.Event()

        # Nothing pending -> exactly one wake is scheduled.
        kernel.request_wake()
        assert len(loop.scheduled) == 1
        fn, args = loop.scheduled[0]
        fn(*args)
        assert kernel._wakeup.is_set()

        # A wake is pending (set but unconsumed) -> coalesced, nothing new.
        kernel.request_wake()
        kernel.request_wake()
        assert len(loop.scheduled) == 1

        # Once consumed, the next request schedules again.
        kernel._wakeup.clear()
        kernel.request_wake()
        assert len(loop.scheduled) == 2

        # Loop not running -> no-op.
        stopped = _RecordingLoop(running=False)
        kernel._loop = stopped
        kernel._wakeup.clear()
        kernel.request_wake()
        assert stopped.scheduled == []

        # Loop closes between the check and the call -> RuntimeError tolerated.
        closing = _RecordingLoop(raise_on_schedule=True)
        kernel._loop = closing
        kernel.request_wake()  # must not raise

    asyncio.run(scenario())


# ---- _sleep_or_wake ---------------------------------------------------------


def test_sleep_or_wake_without_a_wakeup_event_uses_bare_sleep() -> None:
    async def scenario() -> None:
        kernel = SchedulerKernel()
        assert kernel._wakeup is None
        started = time.monotonic()
        await asyncio.wait_for(kernel._sleep_or_wake(20), timeout=1.0)
        assert time.monotonic() - started >= 0.015

    asyncio.run(scenario())


def test_sleep_or_wake_returns_at_once_when_stopping() -> None:
    """stop() between the loop's check and the sleep must not wait a tick."""
    async def scenario() -> None:
        kernel = SchedulerKernel()
        kernel._wakeup = asyncio.Event()
        kernel._stopping = True
        started = time.monotonic()
        await asyncio.wait_for(kernel._sleep_or_wake(60_000), timeout=0.5)
        assert time.monotonic() - started < 0.5

    asyncio.run(scenario())


# ---- failure isolation and bounded stop -------------------------------------


def test_on_tick_exception_does_not_kill_the_loop() -> None:
    """QA (a): the first two ticks raise; the kernel survives and keeps ticking."""
    attempts: list[int] = []
    clock = itertools.count(1000, 1000)

    async def on_tick(now_ms: int) -> None:
        attempts.append(now_ms)
        if len(attempts) <= 2:
            raise RuntimeError("boom")

    async def scenario() -> None:
        kernel = SchedulerKernel(tick_interval_ms=1, now_fn=lambda: next(clock), on_tick=on_tick)
        kernel.start()

        async def wait_for_third_attempt() -> None:
            while len(attempts) < 3:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_third_attempt(), timeout=5.0)
        assert kernel.is_running  # survived both exceptions
        await asyncio.wait_for(kernel.stop(), timeout=5.0)

    asyncio.run(scenario())

    assert len(attempts) >= 3
    assert attempts[:3] == [1000, 2000, 3000]


def test_stop_is_bounded_when_a_tick_blocks() -> None:
    """QA (b): on_tick wedges forever; stop() returns via cancel() within 1s."""

    async def scenario() -> None:
        entered = asyncio.Event()

        async def on_tick(_now_ms: int) -> None:
            entered.set()
            await asyncio.Event().wait()  # wedge forever

        kernel = SchedulerKernel(tick_interval_ms=5, on_tick=on_tick)
        kernel.start()
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        # If cancel() did not break the wedged tick, wait_for raises
        # TimeoutError and this test FAILs.
        await asyncio.wait_for(kernel.stop(), timeout=1.0)
        assert not kernel.is_running

    asyncio.run(scenario())


# ---- retry backoff ------------------------------------------------------------


def test_retry_delay_backoff_sequence_is_clamped() -> None:
    kernel = SchedulerKernel(retry_base_delay_ms=1000, retry_max_delay_ms=5000)
    assert kernel.retry_delay_ms(1) == 1000
    assert kernel.retry_delay_ms(2) == 2000
    assert kernel.retry_delay_ms(3) == 4000
    assert kernel.retry_delay_ms(4) == 5000  # 8000 clamped to max
    assert kernel.retry_delay_ms(5) == 5000
    assert kernel.retry_delay_ms(100) == 5000  # exponent capped, still clamped
    assert kernel.retry_delay_ms(0) == 1000  # exponent floors at 0


def test_retry_delay_zero_base_disables_backoff() -> None:
    kernel = SchedulerKernel(retry_base_delay_ms=0, retry_max_delay_ms=0)
    assert kernel.retry_delay_ms(1) == 0
    assert kernel.retry_delay_ms(10) == 0


# ---- constructor validation ----------------------------------------------------


def test_ctor_rejects_invalid_retry_policy() -> None:
    """QA (c): invalid knobs raise ValueError with the executor's messages."""
    with pytest.raises(ValueError, match="max_consecutive_failures must be at least 1"):
        SchedulerKernel(max_consecutive_failures=0)
    with pytest.raises(ValueError, match="retry_base_delay_ms must be non-negative"):
        SchedulerKernel(retry_base_delay_ms=-1)
    with pytest.raises(ValueError, match="retry_max_delay_ms must be at least retry_base_delay_ms"):
        SchedulerKernel(retry_base_delay_ms=1000, retry_max_delay_ms=999)


# ---- callback seam and policy surface -------------------------------------------


def test_callback_and_knobs_are_exposed_read_only() -> None:
    async def on_tick(now_ms: int) -> None:
        return None

    kernel = SchedulerKernel(
        tick_interval_ms=1234,
        enabled=False,
        max_consecutive_failures=5,
        retry_base_delay_ms=10,
        retry_max_delay_ms=20,
        on_tick=on_tick,
        task_name="k",
    )

    assert kernel.on_tick is on_tick
    assert kernel.tick_interval_ms == 1234
    assert kernel.max_consecutive_failures == 5
    assert (kernel.retry_base_delay_ms, kernel.retry_max_delay_ms) == (10, 20)
    assert kernel.enabled is False
    assert kernel.task_name == "k"
    with pytest.raises(AttributeError):
        kernel.tick_interval_ms = 1  # read-only property


def test_kernel_rejects_the_removed_speculative_seams() -> None:
    # The kernel's only callback is on_tick; due-ness/advance/error seams were
    # dead storage and are gone — a caller passing them must fail loudly.
    for removed_kwarg in ("is_due_fn", "advance_fn", "on_job_error"):
        with pytest.raises(TypeError):
            SchedulerKernel(**{removed_kwarg: lambda *a, **k: None})  # type: ignore[arg-type]


# ---- types: ExecutorKind ----------------------------------------------------------


def test_executor_kind_domain_is_closed() -> None:
    assert EXECUTOR_KINDS == ("prompt", "internal")
    assert is_executor_kind("prompt")
    assert is_executor_kind("internal")
    assert not is_executor_kind("shell")
    assert not is_executor_kind(None)
    assert not is_executor_kind("")
