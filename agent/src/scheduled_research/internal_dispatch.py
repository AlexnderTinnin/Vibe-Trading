"""Fire-and-forget supervision for registry-backed internal jobs.

Extracted beside the scheduled-research executor (scheduler unification) so
``executor.py`` stays under its 800-line cap; the semantics ARE the
executor's internal-dispatch contract:

* Resolve synchronously through the injected dispatcher — in production an
  exact-key allowlist lookup in :mod:`src.scheduling.registry`. A missing
  dispatcher, an unregistered name or a tampered ``internal_callable``
  raises, and the firing routes into the executor's existing
  dispatch-failure path (``failure_kind="dispatch"``, bounded retries)
  without the job ever going RUNNING.
* Never block the serialized tick loop: a resolved callable runs in a
  background ``asyncio`` task — sync callables through ``asyncio.to_thread``,
  async callables awaited directly — because registry callables (the first
  ``br_data`` sync, memory consolidation) can block for hours. Awaiting
  inside the tick would freeze every other due job and the delivery sweep
  until the 5-minute outbox lease expires.
* The background task owns the terminal write: it calls the executor's
  shared ``finish_run`` bookkeeping (COMPLETED/FAILED, ``next_run_at``
  advance, reload-skip in ``_persist_completion``, ``consecutive_failures``
  rules) with the COMPLETION time, so a run longer than its own interval
  advances past completion instead of re-firing immediately.
* PINNED shutdown semantics: :meth:`InternalRunSupervisor.cancel_all`
  cancels the asyncio TASK, never the thread underneath ``to_thread`` — a
  sync callable already running in its thread finishes harmlessly and its
  result is discarded — and NO terminal write happens on cancel: the job
  stays RUNNING in the store, so the next process start's
  ``recover_stale_running`` resets it to PENDING for a fresh firing.
  Internal callables must therefore be safe to re-run. ``stop()`` never
  waits for those threads, so it stays bounded.
* HONEST process-exit caveat: ``asyncio.to_thread`` runs on the loop's
  default ``ThreadPoolExecutor``, whose non-daemon worker threads are joined
  during interpreter finalization. A sync internal run still in flight (an
  hours-long ``br_data`` sync, say) can therefore stall graceful process
  exit past ``stop()`` until the orchestrator's kill timeout (compose's
  ``stop_grace_period``) escalates to SIGKILL. That is safe by the contract
  above — the job stays RUNNING for stale-recovery, store writes are atomic
  (tmp + ``os.replace``) and the sync's SQLite writes are crash-safe — but
  "bounded stop" covers ``stop()`` itself, not interpreter finalization.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from src.scheduled_research.models import JobStatus, ScheduledResearchJob
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling.registry import InternalRunnable

logger = logging.getLogger(__name__)

#: (job, error-or-None, reference-time-ms) -> terminal persistence. Bound to
#: the executor's ``_finish_run``; ``None`` error means success.
FinishRunFn = Callable[[ScheduledResearchJob, Optional[Exception], int], None]
#: Resolves one internal job to its registered callable; raises on any
#: unregistered/tampered name (allowlist lookup, never string parsing).
InternalDispatchCallback = Callable[[ScheduledResearchJob], InternalRunnable]


def resolve_scheduled_internal_callable(job: ScheduledResearchJob) -> InternalRunnable:
    """Resolve one internal job's callable through the registry allowlist.

    Production ``internal_dispatcher`` for the singleton executor (wired in
    ``src/api/scheduled_routes.py``): a pure exact-key lookup in
    :mod:`src.scheduling.registry` — no import paths, no string parsing, no
    shell. Any failure (unregistered or tampered ``internal_callable`` name,
    malformed shape) raises and routes the firing into the executor's
    dispatch-failure path. The supervisor runs the returned zero-arg callable
    off the tick loop (``asyncio.to_thread`` when sync, direct await when
    async) in a fire-and-forget background task.
    """
    from src.scheduling.registry import resolve_internal

    return resolve_internal(job.internal_callable)


class InternalRunSupervisor:
    """Owns the non-blocking run lifecycle of ``executor_kind="internal"`` jobs.

    The executor keeps every scheduling decision; this class only moves the
    blocking work off the tick loop and hands the outcome back through
    *finish_run*. See the module docstring for the full contract, including
    the pinned cancel-not-kill shutdown semantics.
    """

    def __init__(
        self,
        *,
        store: ScheduledResearchJobStore,
        dispatcher: Optional[InternalDispatchCallback],
        finish_run: FinishRunFn,
        now_fn: Callable[[], int],
    ) -> None:
        """Initialize the supervisor.

        Args:
            store: Durable job store (RUNNING mark before the run starts).
            dispatcher: Registry resolver, or ``None`` to keep internal jobs
                fail-closed through the dispatch-failure path.
            finish_run: The executor's shared terminal-write bookkeeping.
            now_fn: Injectable wall-clock source returning epoch milliseconds.
        """
        self._store = store
        self._dispatcher = dispatcher
        self._finish_run = finish_run
        self._now_fn = now_fn
        self._tasks: set[asyncio.Task] = set()

    def resolve(self, job: ScheduledResearchJob) -> InternalRunnable:
        """Return the registered callable for *job*, or raise.

        ``None`` dispatcher keeps the fail-closed default wired in T4: the
        RuntimeError lands in the dispatch-failure path exactly as before the
        registry existed. Resolution itself is the dispatcher's allowlist
        lookup; this class never resolves names by string parsing.
        """
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("internal dispatcher is not configured")
        runnable = dispatcher(job)
        if not callable(runnable):
            raise TypeError("internal dispatcher returned a non-callable")
        return runnable

    def launch(self, job: ScheduledResearchJob, now_ms: int) -> None:
        """Mark *job* RUNNING and hand its callable to a background task.

        Synchronous and non-blocking: returns as soon as the task exists (or
        the resolve failure has been persisted). The caller — the executor's
        ``_run_job`` — returns to the tick immediately afterwards.
        """
        try:
            runnable = self.resolve(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("internal dispatch failed for job %s", job.id, exc_info=True)
            job.consecutive_failures += 1
            self._finish_run(job, exc, now_ms)
            return

        job.status = JobStatus.RUNNING
        # Lifecycle writes bypass validation: this record is already persisted,
        # and a write that cannot land would strand the job mid-flight.
        self._store.upsert(job, validate=False)
        task = asyncio.create_task(
            self._run_to_terminal(job, runnable),
            name=f"scheduled-research-internal-{job.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_to_terminal(self, job: ScheduledResearchJob, runnable: InternalRunnable) -> None:
        """Run the callable off the loop, then own the terminal write.

        Sync callables go through ``asyncio.to_thread`` (they may block for
        hours), async callables are awaited directly. On cancellation the
        pinned shutdown semantics apply (module docstring): log, leave the
        job RUNNING, re-raise — no terminal write, no failure count.
        """
        try:
            if asyncio.iscoroutinefunction(runnable):
                await runnable()
            else:
                await asyncio.to_thread(runnable)
        except asyncio.CancelledError:
            logger.warning(
                "internal job %s cancelled at shutdown; left RUNNING for stale-recovery", job.id
            )
            raise
        except Exception as exc:
            logger.error("internal job %s failed", job.id, exc_info=True)
            job.consecutive_failures += 1
            self._finish_run(job, exc, self._now_fn())
        else:
            job.consecutive_failures = 0
            self._finish_run(job, None, self._now_fn())

    async def cancel_all(self) -> None:
        """Cancel every in-flight internal task; bounded (module docstring).

        Cancellation lands at the task's await point on the next loop pass
        and the handler above does no I/O, so the gather returns promptly
        even while a ``to_thread`` thread keeps running underneath. Threads
        are never killed: their callables finish harmlessly, results
        discarded, jobs left RUNNING for stale-recovery.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
