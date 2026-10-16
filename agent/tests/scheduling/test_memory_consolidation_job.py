"""Scheduled memory-consolidation system job (scheduler-unification T8).

Covers the plan's acceptance criteria and the three Metis traps mechanically:

* (i) ``run_gc`` defaults to ``dry_run=True`` — the due-tick test asserts a
  REAL archive artifact on disk (file moved into ``archive/``), never a
  "function was called" mock; the break-probe for this is flipping the
  explicit ``dry_run=False`` in ``consolidation.py``;
* (ii) honest no-op — with ``VT_MEMORY_GC`` off the callable logs the reason
  and touches NOTHING (no archive dir, no gc.log, entry file untouched);
* (iii) lock semantics — an ``.lock`` held by another descriptor makes the
  per-action ``memory_lock`` time out: no crash, no ``.tmp`` residue, no
  orphan ``.relations.json`` sidecar, entry left in place for the next run;
* default-off + preset isolation — ``VT_MEMORY_CONSOLIDATION`` defaults
  False, is absent from ``_PRESET_FLAGS`` / ``_MEMORY_FLAG_ALIASES``, and
  ``VT_MEMORY=full`` leaves it False with no job seeded;
* end-to-end through the T5 supervisor with the PRODUCTION registry entry
  (``memory.consolidate``) and the production spec tuple.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest
from src.config.accessor import get_env_config, reset_env_config
from src.config.env_schema import _MEMORY_FLAG_ALIASES, _PRESET_FLAGS, MemoryConfig
from src.memory import persistent as persistent_mod
from src.memory.consolidation import consolidate, consolidation_enabled
from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import JobStatus, ScheduledResearchJob
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduling.registry import is_registered, resolve_internal
from src.scheduling.system_jobs import SystemJobSpec, ensure_system_jobs, system_job_specs
from src.scheduling.triggers import next_due

JOB_ID = "system:memory-consolidation"


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Same convention as test_memory_gc.py: setenv must reach the singleton."""
    reset_env_config()
    yield
    reset_env_config()


def _set_env(monkeypatch: pytest.MonkeyPatch, **vars: str) -> None:
    for key, value in vars.items():
        monkeypatch.setenv(key, value)
    reset_env_config()


def _store(tmp_path: Path) -> ScheduledResearchJobStore:
    return ScheduledResearchJobStore(path=tmp_path / "jobs.json")


def _create_memory_file(
    tmp_path: Path,
    name: str,
    *,
    quality_score: float = 0.01,
    created_at: str = "2024-01-01T00:00:00",
) -> Path:
    """Old, low-quality entry — qualified for archival (age > 7d, imp < 0.15).

    Frontmatter shape mirrors test_memory_gc.py's helper.
    """
    slug = name.lower().replace(" ", "_")
    path = tmp_path / f"project_{slug}.md"
    path.write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: {name}\n"
        f"type: project\n"
        f"id: ab12cd\n"
        f"created_at: {created_at}\n"
        f"updated_at: {created_at}\n"
        f"keywords: []\n"
        f"quality_score: {quality_score}\n"
        f"access_count: 0\n"
        f"last_accessed: {created_at}\n"
        f"importance: {quality_score}\n"
        f"related_memories: []\n"
        f"---\n\n"
        f"stale body for {name}\n",
        encoding="utf-8",
    )
    return path


def _resolver(job: ScheduledResearchJob):
    return resolve_internal(job.internal_callable)


async def _drain(executor: ScheduledResearchExecutor) -> None:
    tasks = list(executor._internal._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)


def _production_spec() -> SystemJobSpec:
    matches = [s for s in system_job_specs() if s.id == JOB_ID]
    assert len(matches) == 1, f"expected exactly one {JOB_ID} spec, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# Env flag: default off, preset isolation, explicit env wins
# ---------------------------------------------------------------------------


def test_consolidation_flag_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VT_MEMORY_CONSOLIDATION", raising=False)
    assert get_env_config().memory.consolidation_enabled is False
    assert consolidation_enabled() is False


def test_consolidation_flag_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1")
    assert get_env_config().memory.consolidation_enabled is True
    assert consolidation_enabled() is True


def test_flag_absent_from_preset_maps() -> None:
    """Mechanical proof the preset machinery can never touch this field."""
    assert "consolidation_enabled" not in _MEMORY_FLAG_ALIASES
    for preset_name, flags in _PRESET_FLAGS.items():
        assert "consolidation_enabled" not in flags, preset_name


def test_vt_memory_full_leaves_consolidation_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VT_MEMORY="full")
    cfg = get_env_config().memory
    assert cfg.gc_enabled is True  # preset itself works
    assert cfg.consolidation_enabled is False  # ...but never raises this flag
    assert consolidation_enabled() is False
    assert MemoryConfig().consolidation_enabled is False


def test_explicit_env_wins_over_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VT_MEMORY="full", VT_MEMORY_CONSOLIDATION="1")
    assert get_env_config().memory.consolidation_enabled is True


# ---------------------------------------------------------------------------
# Production spec + registry surfaces
# ---------------------------------------------------------------------------


def test_production_spec_shape() -> None:
    spec = _production_spec()
    assert spec.schedule == "0 4 * * *"
    assert spec.timezone == "UTC"
    assert spec.internal_callable == "memory.consolidate"
    assert spec.source_type == "system"
    assert spec.executor_kind == "internal"
    assert spec.prompt == ""
    assert spec.enabled_predicate is consolidation_enabled


def test_registry_production_entry_delegates_to_consolidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert is_registered("memory.consolidate") is True
    # The registered entry is a literal wrapper with a deferred domain import:
    # calling it must delegate to the real consolidation entry point.
    calls = []
    monkeypatch.setattr(
        "src.memory.consolidation.consolidate", lambda *a, **k: calls.append("fired")
    )
    resolve_internal("memory.consolidate")()
    assert calls == ["fired"]


# ---------------------------------------------------------------------------
# ensure_system_jobs: default off / flag on / VT_MEMORY=full
# ---------------------------------------------------------------------------


def test_default_off_seeds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """QA (a): default flag off → no row, store file never created."""
    monkeypatch.delenv("VT_MEMORY_CONSOLIDATION", raising=False)
    store = _store(tmp_path)
    report = ensure_system_jobs(store, now_fn=lambda: 1000)
    # T9: the production tuple also holds system:br-data-sync (default-off),
    # so membership replaces the exact tuple; "nothing written" stays strict.
    assert JOB_ID in report.skipped_disabled
    assert report.seeded == ()
    assert store.get(JOB_ID) is None
    assert not store.path.exists()


def test_flag_on_seeds_row_with_spec_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1")
    store = _store(tmp_path)
    report = ensure_system_jobs(store, now_fn=lambda: 1000)
    assert report.seeded == (JOB_ID,)
    job = store.get(JOB_ID)
    assert job is not None
    spec = _production_spec()
    assert job.source_type == "system"
    assert job.executor_kind == "internal"
    assert job.internal_callable == "memory.consolidate"
    assert job.schedule == spec.schedule
    assert job.timezone == "UTC"
    assert job.status == JobStatus.PENDING
    # cron + timezone seeds the NEXT 04:00 UTC slot, not "now"
    assert job.next_run_at == next_due(spec.schedule, 1000, "UTC")
    assert job.next_run_at > 1000


def test_vt_memory_full_seeds_no_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VT_MEMORY="full")
    store = _store(tmp_path)
    report = ensure_system_jobs(store, now_fn=lambda: 1000)
    assert JOB_ID in report.skipped_disabled  # T9: br-data-sync joins the skipped set
    assert store.get(JOB_ID) is None


# ---------------------------------------------------------------------------
# consolidate(): honest no-op (trap ii)
# ---------------------------------------------------------------------------


def test_consolidate_noop_when_gc_off(tmp_path: Path, monkeypatch, caplog) -> None:
    """VT_MEMORY_GC off → log the reason, touch NOTHING (no fake work)."""
    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1")
    monkeypatch.delenv("VT_MEMORY_GC", raising=False)
    entry = _create_memory_file(tmp_path, "stale-note")

    with caplog.at_level(logging.INFO, logger="src.memory.consolidation"):
        consolidate(memory_dir=tmp_path)

    assert entry.exists()  # untouched, not archived, not deleted
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "gc.log").exists()
    assert "VT_MEMORY_GC is off" in caplog.text
    assert "no files were touched" in caplog.text


def test_consolidate_noop_when_own_flag_off(tmp_path: Path, monkeypatch, caplog) -> None:
    """Defense in depth: flag flipped off between startup and firing."""
    monkeypatch.delenv("VT_MEMORY_CONSOLIDATION", raising=False)
    _set_env(monkeypatch, VT_MEMORY_GC="1")
    entry = _create_memory_file(tmp_path, "stale-note")

    with caplog.at_level(logging.INFO, logger="src.memory.consolidation"):
        consolidate(memory_dir=tmp_path)

    assert entry.exists()
    assert not (tmp_path / "archive").exists()
    assert "VT_MEMORY_CONSOLIDATION is off" in caplog.text


# ---------------------------------------------------------------------------
# consolidate(): real archival (trap i) + archive-only (QA d)
# ---------------------------------------------------------------------------


def test_consolidate_really_archives_not_dry_run(tmp_path: Path, monkeypatch) -> None:
    """dry_run=False is explicit: a qualified entry MOVES into archive/.

    Archive-only semantics (ENABLE_DELETE=False): the file must exist under
    archive/ — never vanish. Break-probe target: passing dry_run=True in
    consolidation.py reddens this test.
    """
    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1", VT_MEMORY_GC="1", VT_MEMORY_DECAY="1")
    entry = _create_memory_file(tmp_path, "garbage")

    consolidate(memory_dir=tmp_path)

    archived = tmp_path / "archive" / entry.name
    assert archived.is_file(), "real archive artifact must exist (not a dry run)"
    assert not entry.exists(), "entry must have MOVED into archive/"
    assert "stale body" in archived.read_text(encoding="utf-8")
    gc_log = (tmp_path / "gc.log").read_text(encoding="utf-8")
    assert "mode=execute" in gc_log  # not mode=dry_run


# ---------------------------------------------------------------------------
# consolidate(): lock conflict (trap iii)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl is unix-only; win32 memory_lock is a documented no-op")
def test_consolidate_survives_held_memory_lock(tmp_path: Path, monkeypatch) -> None:
    """Another holder owns <dir>/.lock: best-effort skip, no crash, no residue.

    The per-action memory_lock times out and yields False; the entry stays in
    place for the next daily run. No .tmp files, no orphan .relations.json.
    """
    import fcntl

    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1", VT_MEMORY_GC="1", VT_MEMORY_DECAY="1")
    monkeypatch.setattr(persistent_mod, "_LOCK_TIMEOUT_S", 0.3)
    entry = _create_memory_file(tmp_path, "contended")
    lock_path = tmp_path / ".lock"
    lock_path.touch(exist_ok=True)

    holder = open(lock_path, "w")  # noqa: SIM115 — second open file description
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        consolidate(memory_dir=tmp_path)  # must not raise
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert entry.exists(), "lock-timeout skip leaves the entry for the next run"
    assert not (tmp_path / "archive" / entry.name).exists()
    residue = [p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]
    assert residue == []
    sidecars = [p.name for p in tmp_path.rglob("*.relations.json")]
    assert sidecars == []


# ---------------------------------------------------------------------------
# End-to-end: flag on → seeded → due tick → supervisor → real consolidation
# ---------------------------------------------------------------------------


def test_due_tick_runs_consolidation_once_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_registry
) -> None:
    """QA happy, end-to-end through the PRODUCTION spec + registry entry and
    the T5 supervisor: the due internal job archives a qualified memory file
    exactly once; prompt dispatch is never called for it."""
    _set_env(monkeypatch, VT_MEMORY_CONSOLIDATION="1", VT_MEMORY_GC="1", VT_MEMORY_DECAY="1")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(persistent_mod, "MEMORY_BASE", memory_dir)
    entry = _create_memory_file(memory_dir, "e2e-garbage")

    store = _store(tmp_path)
    report = ensure_system_jobs(store, now_fn=lambda: 1000)
    assert report.seeded == (JOB_ID,)
    jobs = store.load()
    jobs[JOB_ID].next_run_at = 1200  # make it due for the tick below
    store.save(jobs)

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

    assert (memory_dir / "archive" / entry.name).is_file()  # REAL artifact
    assert not entry.exists()
    assert prompt_calls == []
    saved = store.get(JOB_ID)
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.failure_kind is None
    assert saved.last_run_at == 1500
    # daily cadence: next fire is the following 04:00 UTC slot after completion
    assert saved.next_run_at == next_due("0 4 * * *", 1500, "UTC")
