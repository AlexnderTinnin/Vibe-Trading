"""A source failure must reach the scheduled job's retry bookkeeping."""

import asyncio

import pytest

from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.internal_dispatch import resolve_scheduled_internal_callable
from src.scheduled_research.models import JobStatus, ScheduledResearchJob
from src.scheduled_research.store import ScheduledResearchJobStore


@pytest.mark.parametrize("failed", [False, True])
def test_sync_report_drives_job_outcome(tmp_path, monkeypatch, failed):
    from src.br_data import sync

    reports = [
        {"db": "/private/database", "bcb": {"rows": 12},
         "tesouro": {"error": "private upstream diagnostic"} if failed else {"rows": 0}},
        {"bcb": {"rows": 12}, "tesouro": {"rows": 0}},
    ]
    monkeypatch.setattr(sync, "run_sync", lambda: reports.pop(0))
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(ScheduledResearchJob(
        id="system:br-data-sync", prompt="", schedule="1000",
        next_run_at=1000, created_at=0, source_type="system",
        executor_kind="internal", internal_callable="br_data.run_sync",
    ))

    async def scenario():
        executor = ScheduledResearchExecutor(
            store, lambda job: None, enabled=True, now_fn=lambda: 1000,
            retry_base_delay_ms=0, retry_max_delay_ms=0,
            internal_dispatcher=resolve_scheduled_internal_callable,
        )
        await executor.tick(1000)
        await asyncio.gather(*executor._internal._tasks)
        job = store.get("system:br-data-sync")
        assert job.status == (JobStatus.PENDING if failed else JobStatus.COMPLETED)
        assert job.consecutive_failures == int(failed)
        if failed:
            assert job.failure_kind == "dispatch"
            assert "tesouro" in job.last_error
            assert "private" not in job.last_error
            await executor.tick(job.next_run_at)
            await asyncio.gather(*executor._internal._tasks)
            recovered = store.get(job.id)
            assert recovered.status == JobStatus.COMPLETED
            assert recovered.consecutive_failures == 0
            assert recovered.last_error is None
        await executor.stop()

    asyncio.run(scenario())
