"""Backward-compatibility tests for scheduler-unification model fields.

Proves — with REAL old-format JSON text, not dicts rebuilt from the new
``to_dict`` — that store files written before ``executor_kind`` /
``internal_callable`` / ``source_type="system"`` existed still load with the
new fields defaulted, roundtrip idempotently, and that corrupt-file
quarantine semantics are unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.scheduled_research.models import JobStatus, ScheduledResearchJob
from src.scheduled_research.store import CorruptStoreError, ScheduledResearchJobStore

# A verbatim job record as ``ScheduledResearchJob.to_dict`` wrote it BEFORE
# scheduler unification: no ``executor_kind``, no ``internal_callable``,
# ``source_type`` is ``"prompt"``.
_OLD_JOB_JSON = """{
  "id": "job-legacy-001",
  "prompt": "analyse AAPL momentum",
  "schedule": "0 */6 * * *",
  "title": "Legacy momentum briefing",
  "source_type": "prompt",
  "playbook_slug": null,
  "end_at": null,
  "next_run_at": 1760000000000,
  "status": "pending",
  "created_at": 1759900000000,
  "last_run_at": null,
  "consecutive_failures": 0,
  "last_error": null,
  "failure_kind": null,
  "config": {},
  "timezone": "Asia/Shanghai",
  "delivery_channel": null,
  "delivery_target": null,
  "delivery_target_ref": null,
  "delivery_target_label": null,
  "delivery_format": null,
  "protect_pdf": false,
  "delivery": {
    "status": "none",
    "session_id": null,
    "key": null,
    "error": null,
    "attempts": 0,
    "updated_at": null,
    "provider_message_id": null
  },
  "last_verdict": null
}"""

# The verbatim on-disk envelope shape (``{"schema_version": 1, "jobs": [...]}``)
# of ``~/.vibe-trading/scheduled_research/scheduled_research_jobs.json``
# written before scheduler unification.
_OLD_STORE_JSON = '{"schema_version": 1, "jobs": [' + _OLD_JOB_JSON + "]}"


def test_fixture_is_genuinely_old_format() -> None:
    assert "executor_kind" not in _OLD_JOB_JSON
    assert "internal_callable" not in _OLD_JOB_JSON
    assert '"source_type": "prompt"' in _OLD_JOB_JSON


class TestOldFormatFromDict:
    def test_old_json_loads_with_new_field_defaults(self) -> None:
        job = ScheduledResearchJob.from_dict(json.loads(_OLD_JOB_JSON))
        assert job.id == "job-legacy-001"
        assert job.prompt == "analyse AAPL momentum"
        assert job.source_type == "prompt"
        assert job.executor_kind == "prompt"
        assert job.internal_callable is None
        assert job.timezone == "Asia/Shanghai"
        assert job.status is JobStatus.PENDING

    def test_system_source_type_loads(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["source_type"] = "system"
        job = ScheduledResearchJob.from_dict(raw)
        assert job.source_type == "system"
        assert job.executor_kind == "prompt"

    def test_unknown_source_type_still_rejected(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["source_type"] = "bogus"
        with pytest.raises(ValueError, match="source_type"):
            ScheduledResearchJob.from_dict(raw)


class TestOldFormatStore:
    def test_store_loads_old_envelope_file(self, tmp_path: Path) -> None:
        store_path = tmp_path / "scheduled_research_jobs.json"
        store_path.write_text(_OLD_STORE_JSON, encoding="utf-8")
        jobs = ScheduledResearchJobStore(path=store_path).load()
        assert set(jobs) == {"job-legacy-001"}
        assert jobs["job-legacy-001"].executor_kind == "prompt"
        assert jobs["job-legacy-001"].internal_callable is None

    def test_load_upsert_save_load_roundtrip_idempotent(self, tmp_path: Path) -> None:
        store_path = tmp_path / "scheduled_research_jobs.json"
        store_path.write_text(_OLD_STORE_JSON, encoding="utf-8")
        store = ScheduledResearchJobStore(path=store_path)
        first = store.load()["job-legacy-001"]

        store.upsert(first)
        reloaded = ScheduledResearchJobStore(path=store_path).load()["job-legacy-001"]
        assert reloaded.to_dict() == first.to_dict()

        envelope = json.loads(store_path.read_text(encoding="utf-8"))
        assert envelope["schema_version"] == 1
        assert envelope["jobs"][0]["executor_kind"] == "prompt"
        assert envelope["jobs"][0]["internal_callable"] is None

    def test_corrupt_json_still_quarantines(self, tmp_path: Path) -> None:
        store_path = tmp_path / "scheduled_research_jobs.json"
        store_path.write_text("{{not valid json}}", encoding="utf-8")
        with pytest.raises(CorruptStoreError) as exc_info:
            ScheduledResearchJobStore(path=store_path).load()
        assert not store_path.exists()
        quarantined = exc_info.value.quarantined
        assert quarantined.exists()
        assert ".corrupt-" in quarantined.name


class TestInternalExecutorValidation:
    def _internal_job(self) -> ScheduledResearchJob:
        now = int(time.time() * 1000)
        return ScheduledResearchJob(
            id="sys-001",
            prompt="",
            schedule="60000",
            source_type="system",
            executor_kind="internal",
            internal_callable="system_health_check",
            next_run_at=now + 60_000,
            created_at=now,
        )

    def test_empty_prompt_internal_job_roundtrips_to_dict(self) -> None:
        job = self._internal_job()
        raw = job.to_dict()
        assert raw["prompt"] == ""
        assert raw["executor_kind"] == "internal"
        assert raw["internal_callable"] == "system_health_check"
        assert ScheduledResearchJob.from_dict(raw) == job

    def test_empty_prompt_internal_job_persists_via_store(self, tmp_path: Path) -> None:
        store_path = tmp_path / "scheduled_research_jobs.json"
        job = self._internal_job()
        ScheduledResearchJobStore(path=store_path).upsert(job)
        fetched = ScheduledResearchJobStore(path=store_path).get("sys-001")
        assert fetched == job
        assert fetched.prompt == ""
        assert fetched.internal_callable == "system_health_check"

    def test_internal_without_callable_rejected(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["executor_kind"] = "internal"
        raw["internal_callable"] = None
        with pytest.raises(ValueError, match="internal_callable"):
            ScheduledResearchJob.from_dict(raw)

    def test_internal_with_empty_callable_rejected(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["executor_kind"] = "internal"
        raw["internal_callable"] = ""
        with pytest.raises(ValueError, match="internal_callable"):
            ScheduledResearchJob.from_dict(raw)

    def test_unknown_executor_kind_rejected(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["executor_kind"] = "shell"
        with pytest.raises(ValueError, match="executor_kind"):
            ScheduledResearchJob.from_dict(raw)

    def test_non_string_internal_callable_rejected(self) -> None:
        raw = json.loads(_OLD_JOB_JSON)
        raw["internal_callable"] = 13
        with pytest.raises(TypeError, match="internal_callable"):
            ScheduledResearchJob.from_dict(raw)
