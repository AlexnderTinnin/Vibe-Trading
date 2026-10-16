"""Contract tests for token-usage fields on ``GET /swarm/runs/{run_id}``.

The detail response must surface the run's stored usage — totals, provider,
model and the ``token_source`` provenance label — with the same key names the
MCP run payload uses. ``provider``/``model`` additionally pass through the same
public-metadata gate MCP applies on read, so a run.json written before that
gate existed cannot leak an endpoint- or credential-shaped string to a browser
client. A run that never executed must report literal int ``0`` totals (not
``null``, not a missing key) so a cost UI can never mistake an unmeasured run
for a missing field.

The swarm runtime singleton is monkeypatched to a ``SimpleNamespace`` holding a
real ``SwarmStore`` rooted at ``tmp_path``, so every response is read back from
persisted ``run.json`` state — the same isolation idiom as
``test_swarm_route_contracts.py`` and ``test_scheduled_routes.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
import src.api.swarm_routes as swarm_routes
from src.swarm.models import RunStatus, SwarmRun
from src.swarm.store import SwarmStore


@pytest.fixture
def swarm_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SwarmStore:
    """Route the process-wide swarm endpoint at an isolated on-disk store."""
    store = SwarmStore(base_dir=tmp_path / "runs")
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    monkeypatch.setattr(swarm_routes, "_swarm_runtime", SimpleNamespace(_store=store))
    return store


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _seed_run(store: SwarmStore, run_id: str, **usage: object) -> SwarmRun:
    run = SwarmRun(
        id=run_id,
        preset_name="usage-test",
        status=RunStatus.completed,
        created_at="2026-10-16T00:00:00+00:00",
        completed_at="2026-10-16T00:00:01+00:00",
        **usage,  # type: ignore[arg-type]
    )
    store.create_run(run)
    return run


def test_detail_returns_stored_token_totals_and_provider_model(
    swarm_store: SwarmStore,
) -> None:
    """Given a persisted run with measured usage, When GET detail, Then the
    response carries the stored values under the MCP-parity keys (an allowlisted
    provider/model survives the public-metadata gate unchanged)."""
    _seed_run(
        swarm_store,
        "run-usage",
        total_input_tokens=4200,
        total_output_tokens=1300,
        provider="deepseek",
        model="deepseek-v4-pro",
        token_source="provider",
    )

    response = _client().get("/swarm/runs/run-usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_input_tokens"] == 4200
    assert payload["total_output_tokens"] == 1300
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["token_source"] == "provider"


def test_detail_returns_int_zero_totals_for_never_run(
    swarm_store: SwarmStore,
) -> None:
    """Given a run that never executed, When GET detail, Then every usage key
    is present with honest defaults — int 0 totals, None token_source."""
    _seed_run(swarm_store, "run-never-executed")

    response = _client().get("/swarm/runs/run-never-executed")

    assert response.status_code == 200
    payload = response.json()
    for key in (
        "total_input_tokens",
        "total_output_tokens",
        "provider",
        "model",
        "token_source",
    ):
        assert key in payload, f"{key} missing from detail response"
    assert payload["total_input_tokens"] == 0
    assert type(payload["total_input_tokens"]) is int
    assert payload["total_output_tokens"] == 0
    assert type(payload["total_output_tokens"]) is int
    assert payload["token_source"] is None


def test_detail_reflects_updated_stored_run_not_stale_snapshot(
    swarm_store: SwarmStore,
) -> None:
    """Given usage written to the store after creation, When GET detail, Then
    the response reflects the persisted update (stale-state guard)."""
    run = _seed_run(swarm_store, "run-updated", total_input_tokens=1)
    swarm_store.update_run(
        run.model_copy(
            update={
                "total_input_tokens": 9000,
                "total_output_tokens": 700,
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "token_source": "estimated",
            }
        )
    )

    payload = _client().get("/swarm/runs/run-updated").json()

    assert payload["total_input_tokens"] == 9000
    assert payload["total_output_tokens"] == 700
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-sonnet-5"
    assert payload["token_source"] == "estimated"


def test_detail_gates_non_public_provider_and_endpoint_shaped_model(
    swarm_store: SwarmStore,
) -> None:
    """Given a stored run whose provider/model predate the write-time public
    gate, When GET detail, Then both are redacted rather than leaked raw."""
    _seed_run(
        swarm_store,
        "run-unsanitized",
        provider="internal-proxy",
        model="http://10.0.0.5:8080/v1",
    )

    payload = _client().get("/swarm/runs/run-unsanitized").json()

    assert payload["provider"] == "[redacted]"
    assert payload["model"] == "[redacted]"
