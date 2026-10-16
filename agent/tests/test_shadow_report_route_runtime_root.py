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


def test_rendered_report_is_readable_through_the_route(monkeypatch, tmp_path) -> None:
    from src.shadow_account import (
        AttributionBreakdown,
        ShadowBacktestResult,
        ShadowProfile,
        render_shadow_report,
    )

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "state-root"))
    profile = ShadowProfile(
        shadow_id="shadow_0123abcd",
        created_at="2026-01-06T00:00:00Z",
        journal_hash="fixture",
        source_market="us",
        profitable_roundtrips=0,
        total_roundtrips=0,
        date_range=("2026-01-02", "2026-01-05"),
        profile_text="Runtime root report integration fixture",
        rules=(),
        preferred_markets=("us",),
        typical_holding_days=(0.0, 0.0),
    )
    result = ShadowBacktestResult(
        shadow_id=profile.shadow_id,
        per_market={},
        combined={},
        equity_curves={},
        attribution=AttributionBreakdown(0.0, 0.0, 0.0, 0.0, 0.0),
        shadow_total_pnl=None,
        real_total_pnl=0.0,
        delta_pnl=None,
    )
    rendered = render_shadow_report(profile, result)
    with _client() as client:
        response = client.get(f"/shadow-reports/{profile.shadow_id}?format=html")
        assert response.status_code == 200
        assert response.text == Path(rendered["html_path"]).read_text(encoding="utf-8")
        assert profile.profile_text in response.text
        if rendered["pdf_path"]:
            response = client.get(f"/shadow-reports/{profile.shadow_id}?format=pdf")
            assert response.status_code == 200
            assert response.content == Path(rendered["pdf_path"]).read_bytes()
