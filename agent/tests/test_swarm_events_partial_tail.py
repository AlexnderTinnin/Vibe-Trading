"""``SwarmStore.read_events`` must tolerate a half-written final event line."""

from __future__ import annotations

import pytest

from src.swarm.models import SwarmEvent
from src.swarm.store import SwarmStore


def _store(tmp_path):
    store = SwarmStore(tmp_path)
    (tmp_path / "run1").mkdir()
    return store


def test_partial_trailing_line_is_skipped_not_raised(tmp_path):
    store = _store(tmp_path)
    store.append_event("run1", SwarmEvent(type="run_started", timestamp="2026-01-01T00:00:00+00:00"))
    events_file = tmp_path / "run1" / "events.jsonl"
    with events_file.open("a", encoding="utf-8") as f:
        f.write('{"type": "task_started", "agent_')  # writer mid-line / crashed host

    events = store.read_events("run1")
    assert [e.type for e in events] == ["run_started"]

    # Once the writer finishes the line, the next poll sees the event.
    with events_file.open("a", encoding="utf-8") as f:
        f.write('id": "a1", "data": {}, "timestamp": "2026-01-01T00:00:01+00:00"}\n')
    events = store.read_events("run1", after_index=1)
    assert [e.type for e in events] == ["task_started"]


def test_corrupt_terminated_line_still_raises(tmp_path):
    store = _store(tmp_path)
    store.append_event("run1", SwarmEvent(type="run_started", timestamp="2026-01-01T00:00:00+00:00"))
    with (tmp_path / "run1" / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write("not json\n")
    with pytest.raises(ValueError):
        store.read_events("run1")


def test_partial_utf8_tail_is_retried_after_completion(tmp_path):
    store = _store(tmp_path)
    event = SwarmEvent(type="run_started", timestamp="2026-01-01T00:00:00+00:00", data={"text": "中文"})
    encoded = (event.model_dump_json() + "\n").encode("utf-8")
    split = encoded.index("中".encode("utf-8")) + 1
    path = tmp_path / "run1" / "events.jsonl"
    path.write_bytes(encoded[:split])
    assert store.read_events("run1") == []
    with path.open("ab") as stream:
        stream.write(encoded[split:])
    assert store.read_events("run1") == [event]


def test_complete_json_with_invalid_schema_is_not_a_partial_tail(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "run1" / "events.jsonl").write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.read_events("run1")


def test_complete_unterminated_event_is_read(tmp_path):
    store = _store(tmp_path)
    event = SwarmEvent(type="run_started", timestamp="2026-01-01T00:00:00+00:00")
    (tmp_path / "run1" / "events.jsonl").write_text(event.model_dump_json(), encoding="utf-8")
    assert store.read_events("run1") == [event]
