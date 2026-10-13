"""File evidence remains dated, complete, distinct from broker account identity."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.portfolio.config import PortfolioSettingsStore
from src.portfolio.fx import FxQuote
from src.portfolio.imports import PortfolioImportStore
from src.portfolio.rebalance import propose_rebalance
from src.portfolio.service import PortfolioService
from src.portfolio.store import PortfolioStore

HEADER = "ISIN;Name;Typ;Anzahl;Kaufpreis;Aktueller Kurs;Aktueller Wert;Währung;Wechselkurs;Region;Land;Sektor;Portfolioname;Portfolio ID"
CSV = HEADER + "\n" + "IE00B4L5Y983;Example ETF;ETF;10;8;10;100;EUR;1;Welt;;;Private account;one\n" + "IE00B4L5Y983;Example ETF;ETF;20;8;10;200;EUR;1;Welt;;;Other private account;two\n"
NOW = datetime.now(timezone.utc)


def create(store, **overrides):
    values = dict(csv_text=CSV, label="Private import", source_mtime=(NOW - timedelta(hours=1)).isoformat(),
                  expected_positions=2, max_age_days=7)
    values.update(overrides)
    return store.create(**values)


def service(tmp_path, *, rates=None):
    settings = PortfolioSettingsStore(tmp_path / "portfolio.json")
    imported = create(settings.import_store)
    settings.save({"display_currency": "EUR", "sources": [{"connection_id": imported["id"], "source_kind": "extraetf", "label": "Private import"}]})
    def no_broker(*args, **kwargs):
        pytest.fail("file import must never call a broker")
    instance = PortfolioService(
        PortfolioStore(tmp_path / "snapshots.sqlite3"), settings_store=settings,
        get_account=no_broker, get_positions=no_broker, get_quote=no_broker,
        fx_fetcher=lambda: FxQuote(rates or {"USD": Decimal(1), "CNY": Decimal(7), "HKD": Decimal(8), "EUR": Decimal("0.9")},
                                  NOW.isoformat(), NOW.date().isoformat()),
    )
    return instance, imported


def test_import_refresh_context_and_proposal_share_real_store(tmp_path):
    instance, imported = service(tmp_path)
    snapshot = instance.refresh()
    assert snapshot["complete"]
    assert snapshot["totals"]["display"] == pytest.approx(300)
    assert snapshot["totals"]["usd"] == pytest.approx(333.33333333)
    assert len(snapshot["positions"]) == 2
    assert {row["portfolio_id"] for row in snapshot["positions"]} == {"one", "two"}
    assert all(row["updated_at"] is None for row in snapshot["positions"])
    assert snapshot["accounts"][0]["auth"] is None
    assert instance.latest()["snapshot_id"] == snapshot["snapshot_id"]
    assert float(instance.history()[0]["total_eur"]) == pytest.approx(300)
    context = instance.analysis_context(snapshot["snapshot_id"])
    assert context["risk_xray_args"] == {"symbols": [], "weights": {}}
    assert len(context["holdings"]) == 1
    key = context["holdings"][0]["canonical_id"]
    assert key == "ISIN:IE00B4L5Y983"
    serialized = json.dumps(context)
    for private in ("Private import", "Private account", "Other private account", "portfolio_id", str(tmp_path)):
        assert private not in serialized
    assert context["account_allocation"][0]["file_evidence"]["source_mtime"] == imported["source_mtime"]
    result = instance.rebalance(snapshot["snapshot_id"], {key: 0.6, "CASH": 0.4})
    assert result["mode"] == "proposal_only"
    assert result["currency"] == "EUR"
    assert {row["canonical_id"]: row["change_value"] for row in result["rows"]} == pytest.approx({key: -120, "CASH": 120})
    assert "portfolio_id" in instance.export_csv()
    assert instance.sources()[0]["source_kind"] == "extraetf"


@pytest.mark.parametrize("override,match", [
    ({"expected_positions": 3}, "expected 3"),
    ({"source_mtime": "2000-01-01T00:00:00Z"}, "stale"),
    ({"source_mtime": (NOW + timedelta(days=1)).isoformat()}, "future"),
    ({"source_mtime": "2026-10-01T00:00:00"}, "timezone"),
    ({"csv_text": CSV.replace(";10;100;", ";;100;")}, "positive exported price"),
    ({"csv_text": CSV.replace(";two", ";one")}, "twice"),
    ({"expected_positions": True}, "integer"),
    ({"max_age_days": False}, "integer"),
    ({"csv_text": CSV.replace("EUR", "XYZ")}, "currency"),
])
def test_bad_import_never_persists_a_partial_source(tmp_path, override, match):
    store = PortfolioImportStore(tmp_path / "imports.sqlite3")
    with pytest.raises(ValueError, match=match):
        create(store, **override)
    assert store.list() == []


def test_expired_import_is_an_error_and_never_uses_old_positions(tmp_path):
    instance, imported = service(tmp_path)
    healthy = instance.refresh()
    with instance.settings_store.import_store._connect() as db:
        payload = instance.settings_store.import_store.get(imported["id"])
        payload["expires_at"] = "2000-01-01T00:00:00Z"
        db.execute("UPDATE imports SET payload = ? WHERE id = ?", (json.dumps(payload), imported["id"]))
    assert instance.latest() is None
    failed = instance.refresh()
    assert failed["complete"] is False
    assert failed["positions"] == []
    assert failed["totals"]["usd"] == 0
    assert failed["accounts"][0]["status"] == "error"
    assert failed["accounts"][0]["last_success_at"] == healthy["created_at"]


def test_missing_eur_rate_fails_the_import_source_without_usd_fallback(tmp_path):
    instance, _ = service(tmp_path, rates={"USD": Decimal(1), "CNY": Decimal(7), "HKD": Decimal(8)})
    instance.save_settings({**instance.settings(), "display_currency": "USD"})
    result = instance.refresh()
    assert result["complete"] is False
    assert "EUR" in result["accounts"][0]["error"]
    assert result["positions"] == []


@pytest.mark.parametrize("targets", [{"UNKNOWN": 1}, {"CASH": 0.9}, {"CASH": True}, {"CASH": float("nan")}, {"CASH": 1.1}])
def test_rebalance_rejects_guessed_or_invalid_targets(tmp_path, targets):
    instance, _ = service(tmp_path)
    snapshot = instance.refresh()
    with pytest.raises(ValueError):
        instance.rebalance(snapshot["snapshot_id"], targets)


def test_rebalance_requires_complete_unexpired_evidence(tmp_path):
    instance, _ = service(tmp_path)
    snapshot = instance.refresh()
    snapshot["accounts"][0]["import_metadata"]["expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="stale"):
        propose_rebalance(snapshot, {"CASH": 1})
    snapshot["complete"] = False
    with pytest.raises(ValueError, match="complete"):
        propose_rebalance(snapshot, {"CASH": 1})


def test_fx_http_shape_date_and_eur_then_coherent_cache(tmp_path, monkeypatch):
    import io
    import src.portfolio.service as module
    payload = {"amount": 1, "base": "USD", "date": NOW.date().isoformat(), "rates": {"CNY": 7, "HKD": 8, "EUR": 0.9}}
    seen = []
    def fetch(request, **kwargs):
        seen.append(request.full_url)
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(module.urllib.request, "urlopen", fetch)
    instance, _ = service(tmp_path)
    instance._fx_fetcher = instance._fetch_fx
    snapshot = instance.refresh()
    assert seen == ["https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,EUR"]
    assert snapshot["fx"]["as_of"] == NOW.date().isoformat()
    payload["rates"]["EUR"] = -1
    cached = instance.refresh()
    assert cached["fx"]["stale"]
    assert cached["fx"]["rates"] == snapshot["fx"]["rates"]
    assert cached["fx"]["fetched_at"] == snapshot["fx"]["fetched_at"]


def test_http_import_settings_refresh_and_rebalance_use_same_store(tmp_path, monkeypatch):
    from src.api import portfolio_routes
    from src.api.security import require_auth, require_settings_write_auth
    instance, _ = service(tmp_path)
    monkeypatch.setattr(portfolio_routes, "PortfolioService", lambda **kwargs: instance)
    app = FastAPI()
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_settings_write_auth] = lambda: None
    portfolio_routes.register_portfolio_routes(app)
    client = TestClient(app)
    result = client.post("/api/portfolio/imports/extraetf", json={
        "csv_text": CSV, "label": "Web import", "source_mtime": (NOW - timedelta(hours=1)).isoformat(),
        "expected_positions": 2, "max_age_days": 7,
    })
    assert result.status_code == 200, result.text
    imported = result.json()["import"]
    assert "positions" not in imported
    settings = {"display_currency": "EUR", "sources": [{"connection_id": imported["id"], "source_kind": "extraetf", "label": "Web import"}]}
    assert client.put("/api/portfolio/settings", json=settings).status_code == 200
    snapshot = client.post("/api/portfolio/refresh").json()["snapshot"]
    proposal = client.post("/api/portfolio/rebalance", json={"snapshot_id": snapshot["snapshot_id"], "target_weights": {"CASH": 1}})
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["proposal"]["rows"][0]["change_value"] == pytest.approx(300)


def test_two_imports_cannot_double_count_the_same_underlying_portfolios(tmp_path):
    settings = PortfolioSettingsStore(tmp_path / "portfolio.json")
    first, second = create(settings.import_store), create(settings.import_store)
    sources = [{"connection_id": item["id"], "source_kind": "extraetf", "label": item["label"]} for item in (first, second)]
    with pytest.raises(ValueError, match="overlapping Portfolio IDs"):
        settings.save({"sources": sources})
    sources[0]["enabled"] = False
    assert len(settings.save({"sources": sources}).sources) == 2
