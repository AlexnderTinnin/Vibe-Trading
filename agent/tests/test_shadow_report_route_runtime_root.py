"""The shadow-report route must read from where reports are written."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.uploads_routes import register_uploads_routes


def _client() -> TestClient:
    app = FastAPI()
    register_uploads_routes(app, require_auth=lambda: None)
    return TestClient(app)


def test_shadow_report_is_served_from_the_configured_runtime_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.shadow_account.storage import reports_dir

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "state-root"))
    report = reports_dir() / "shadow_0123abcd.html"
    report.write_text("<html>report</html>", encoding="utf-8")

    response = _client().get("/shadow-reports/shadow_0123abcd")

    assert response.status_code == 200
    assert "report" in response.text
