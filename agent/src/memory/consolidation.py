"""Scheduled memory consolidation (scheduler unification).

Thin orchestration surface consumed by the scheduler; it adds NO semantics of
its own — every behavior lives in the existing memory modules and this module
only decides *whether* and *how* to call them:

* :func:`consolidation_enabled` is the ``enabled_predicate`` of the
  ``system:memory-consolidation`` spec in :mod:`src.scheduling.system_jobs`.
  It reads ``VT_MEMORY_CONSOLIDATION`` (``MemoryConfig.consolidation_enabled``)
  through :func:`src.config.accessor.get_env_config` — the sanctioned config
  path; the flag is deliberately NOT part of the ``VT_MEMORY`` presets, so
  ``VT_MEMORY=full`` never silently starts a background timer.
* :func:`consolidate` is the callable registered under the literal key
  ``"memory.consolidate"`` in :mod:`src.scheduling.registry`. It is SYNC by
  design: the executor's internal-run supervisor offloads sync callables via
  ``asyncio.to_thread``, so the blocking GC/compression pass never runs on the
  event loop.

Honest no-op contract: archive GC is gated by
``VT_MEMORY_GC`` and compression is only ever invoked through GC's
archive→compress handoff (``lifecycle.run_gc`` is the sole production caller
of ``CompressionPipeline``), so with GC off there is nothing this job can
truthfully do — it logs the reason and returns WITHOUT touching any file,
rather than faking work. ``VT_MEMORY_DECAY`` off degrades importance to raw
quality scores (existing ``compute_importance`` behavior); that is noted in
the log but still a real consolidation pass.

Concurrency contract: writes are serialized by the memory
layer's own ``memory_lock`` (``fcntl.flock`` on ``<memory_dir>/.lock``) taken
PER ACTION inside ``lifecycle`` — this module must NOT hold that lock around
``run_gc`` because the inner per-action acquisitions would then conflict with
the outer one and every action would time out. A lock timeout yields
``False`` and the action is skipped best-effort (the daily cadence re-fires
the next day). Known pre-existing limitation, unchanged by the scheduler:
on win32 ``memory_lock`` yields ``True`` WITHOUT any lock, so scheduled
consolidation and session writes have NO mutual exclusion on Windows — the
timer raises the race frequency but not the semantic. No failure path here
leaves half-written state: the underlying writers are atomic (tmp +
``os.replace``) and archive-only (``ENABLE_DELETE = False`` — low-importance
memories are moved to ``<memory_dir>/archive/``, never deleted).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def consolidation_enabled() -> bool:
    """Whether the scheduled consolidation system job is enabled.

    Reads ``VT_MEMORY_CONSOLIDATION`` via the cached ``EnvConfig`` singleton
    (default ``False``; presets never raise it). Bound as the
    ``enabled_predicate`` of the ``system:memory-consolidation`` spec.
    """
    from src.config.accessor import get_env_config

    return get_env_config().memory.consolidation_enabled


def consolidate(memory_dir: Optional[Path] = None) -> None:
    """Run one consolidation pass: archive-only GC (+ its compression handoff).

    Invoked with no arguments by the internal-run supervisor (``memory_dir``
    then defaults to ``MEMORY_BASE``); the optional parameter is a test seam.
    Safe to re-run (supervisor shutdown semantics discard in-flight results
    and leave the job for stale-recovery).

    Args:
        memory_dir: Memory directory to consolidate; ``None`` uses the
            process-wide ``MEMORY_BASE`` resolved at call time.
    """
    from src.config.accessor import get_env_config

    cfg = get_env_config().memory
    if not cfg.consolidation_enabled:
        # Defense in depth: normally the row is not seeded (or is halted) when
        # the flag is off; this covers the flag flipping off between startup
        # and firing. Honest no-op — nothing is read or written.
        logger.info("memory consolidation skipped: VT_MEMORY_CONSOLIDATION is off")
        return
    if not cfg.gc_enabled:
        # GC gates BOTH the archive pass and the archive→compress handoff, so
        # without it there is nothing to truthfully consolidate.
        logger.info(
            "memory consolidation no-op: VT_MEMORY_GC is off — archive GC and the "
            "archive→compress handoff are both gated by it; no files were touched"
        )
        return
    if not cfg.decay_enabled:
        logger.info(
            "memory consolidation: VT_MEMORY_DECAY is off — importance falls back "
            "to raw quality scores for this pass"
        )

    # Imported lazily: keeps this module's import footprint stdlib-only so the
    # scheduling package can import the predicate without pulling in the memory
    # stack, and mirrors the lazy-import style of the memory modules themselves.
    from src.memory.lifecycle import MemoryLifecycle
    from src.memory.persistent import PersistentMemory

    memory = PersistentMemory(memory_dir=memory_dir)
    lifecycle = MemoryLifecycle(memory)
    # run_gc's default is dry_run=True (audit-only). The scheduled pass MUST
    # archive for real, so dry_run=False is passed explicitly.
    actions = lifecycle.run_gc(dry_run=False)
    archived = sum(1 for a in actions if a.get("action") == "archive")
    logger.info(
        "memory consolidation finished: %d action(s), %d archived (archive-only, "
        "no deletions); lock-timeout skips, if any, are retried by the next daily run",
        len(actions),
        archived,
    )
