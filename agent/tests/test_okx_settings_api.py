"""Security and behavior tests for the OKX Demo Settings API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import okx_settings_routes
from src.trading.connectors.okx import sdk as okx_sdk


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    path = tmp_path / "okx.json"
    monkeypatch.setattr(okx_sdk, "config_path", lambda: path)
    monkeypatch.setattr(okx_settings_routes, "okx_available", lambda: True)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    existing_runners = dict(api_server._quant_runners)
    api_server._quant_runners.clear()
    with TestClient(api_server.app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client
    api_server._quant_runners.clear()
    api_server._quant_runners.update(existing_runners)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "api_key": "demo-api-key",
        "api_secret": "demo-api-secret",
        "passphrase": "demo-passphrase",
        "expected_uid": "demo-uid",
        "allowed_swap_instruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "max_swap_leverage": 8,
    }
    payload.update(overrides)
    return payload


def test_get_status_is_side_effect_free_and_never_returns_secrets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.get("/quant/okx/config/status")

    assert response.status_code == 200
    assert response.json() == {
        "broker": "okx",
        "profile": "paper",
        "environment": "demo",
        "is_demo": True,
        "configured": False,
        "credentials_configured": False,
        "api_key_configured": False,
        "api_secret_configured": False,
        "passphrase_configured": False,
        "expected_uid": "",
        "allowed_swap_instruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "max_swap_leverage": 10.0,
        "proxy_configured": False,
        "sdk_installed": True,
        "runner_running": False,
        "missing_fields": ["api_key", "api_secret", "passphrase", "expected_uid"],
    }
    assert not (tmp_path / "okx.json").exists()


def test_put_saves_demo_config_and_returns_only_credential_state(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.put(
        "/quant/okx/config",
        json=_payload(
            allowed_swap_instruments=["BTC-USDT-SWAP"],
            proxy="http://proxy-user:proxy-password@127.0.0.1:7890",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["credentials_configured"] is True
    assert body["profile"] == "paper"
    assert body["environment"] == "demo"
    assert body["allowed_swap_instruments"] == ["BTC-USDT-SWAP"]
    assert body["max_swap_leverage"] == 8
    assert body["proxy_configured"] is True
    for secret in (
        "demo-api-key",
        "demo-api-secret",
        "demo-passphrase",
        "proxy-password",
    ):
        assert secret not in response.text

    saved = json.loads((tmp_path / "okx.json").read_text(encoding="utf-8"))
    assert saved["api_key"] == "demo-api-key"
    assert saved["api_secret"] == "demo-api-secret"
    assert saved["passphrase"] == "demo-passphrase"
    assert saved["profile"] == "paper"
    assert saved["host"] == okx_sdk.DEFAULT_HOST
    assert saved["readonly"] is False
    assert saved["expected_uid"] == "demo-uid"


def test_put_with_blank_credentials_preserves_existing_values(
    client: TestClient,
    tmp_path: Path,
) -> None:
    assert client.put("/quant/okx/config", json=_payload()).status_code == 200

    response = client.put(
        "/quant/okx/config",
        json={
            "expected_uid": "updated-demo-uid",
            "allowed_swap_instruments": ["ETH-USDT-SWAP"],
            "max_swap_leverage": 5,
        },
    )

    assert response.status_code == 200
    saved = json.loads((tmp_path / "okx.json").read_text(encoding="utf-8"))
    assert saved["api_key"] == "demo-api-key"
    assert saved["api_secret"] == "demo-api-secret"
    assert saved["passphrase"] == "demo-passphrase"
    assert saved["expected_uid"] == "updated-demo-uid"
    assert saved["allowed_swap_instruments"] == ["ETH-USDT-SWAP"]


def test_put_rejects_partial_credentials_without_persisting_them(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.put(
        "/quant/okx/config",
        json={
            "api_key": "partial-secret-key",
            "expected_uid": "demo-uid",
            "allowed_swap_instruments": ["BTC-USDT-SWAP"],
            "max_swap_leverage": 3,
        },
    )

    assert response.status_code == 400
    assert "provided together" in response.json()["detail"]
    assert "partial-secret-key" not in response.text
    assert not (tmp_path / "okx.json").exists()


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"proxy": "socks5://proxy-user:proxy-secret@127.0.0.1:1080"}, "HTTP(S) proxy"),
        ({"expected_uid": "   "}, "OKX Demo UID is required"),
    ],
)
def test_put_rejects_invalid_non_secret_settings_without_leaking_credentials(
    client: TestClient,
    tmp_path: Path,
    overrides: dict[str, object],
    detail: str,
) -> None:
    response = client.put("/quant/okx/config", json=_payload(**overrides))

    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert "demo-api-secret" not in response.text
    assert "proxy-secret" not in response.text
    assert not (tmp_path / "okx.json").exists()


def test_put_forbids_environment_overrides_and_only_accepts_btc_eth_swaps(
    client: TestClient,
) -> None:
    live_response = client.put(
        "/quant/okx/config",
        json=_payload(profile="live", host="https://attacker.invalid"),
    )
    unsupported_response = client.put(
        "/quant/okx/config",
        json=_payload(allowed_swap_instruments=["SOL-USDT-SWAP"]),
    )

    assert live_response.status_code == 422
    assert unsupported_response.status_code == 422
    assert "demo-api-secret" not in live_response.text
    assert "demo-api-secret" not in unsupported_response.text


def test_put_rejects_configuration_changes_while_runner_is_active(
    client: TestClient,
) -> None:
    assert client.put("/quant/okx/config", json=_payload()).status_code == 200
    api_server._quant_runners["okx"] = object()

    response = client.put(
        "/quant/okx/config",
        json={
            "expected_uid": "another-uid",
            "allowed_swap_instruments": ["BTC-USDT-SWAP"],
            "max_swap_leverage": 2,
        },
    )

    assert response.status_code == 409
    assert "Stop the OKX quant runner" in response.json()["detail"]


def test_connection_check_is_read_only_and_returns_sanitized_summary(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.put("/quant/okx/config", json=_payload()).status_code == 200
    before = (tmp_path / "okx.json").read_bytes()
    monkeypatch.setattr(
        okx_settings_routes,
        "check_status",
        lambda config: {
            "status": "ok",
            "account": {
                "is_demo": True,
                "uid": config.expected_uid,
                "total_equity": "1234.56",
            },
        },
    )

    response = client.post("/quant/okx/config/check")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["is_demo"] is True
    assert body["uid_verified"] is True
    assert body["total_equity"] == "1234.56"
    assert (tmp_path / "okx.json").read_bytes() == before
    assert "demo-api-key" not in response.text
    assert "demo-api-secret" not in response.text
    assert "demo-passphrase" not in response.text


def test_connection_check_redacts_secrets_from_connector_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.put(
        "/quant/okx/config",
        json=_payload(proxy="http://proxy-user:proxy-password@127.0.0.1:7890"),
    ).status_code == 200
    monkeypatch.setattr(
        okx_settings_routes,
        "check_status",
        lambda _config: {
            "status": "error",
            "error": (
                "bad demo-api-key demo-api-secret demo-passphrase "
                "http://proxy-user:proxy-password@127.0.0.1:7890"
            ),
        },
    )

    response = client.post("/quant/okx/config/check")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    for secret in (
        "demo-api-key",
        "demo-api-secret",
        "demo-passphrase",
        "proxy-password",
    ):
        assert secret not in response.text
    assert "[redacted]" in response.text


def test_connection_check_surfaces_okx_environment_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.put("/quant/okx/config", json=_payload()).status_code == 200
    message = "APIKey does not match current environment. (OKX code 50101)"
    monkeypatch.setattr(
        okx_settings_routes,
        "check_status",
        lambda _config: {"status": "error", "error": message},
    )

    response = client.post("/quant/okx/config/check")

    assert response.status_code == 200
    assert response.json()["message"] == message
    assert response.json()["uid_verified"] is False


def test_live_profile_is_visible_but_cannot_pass_demo_connection_check(
    client: TestClient,
    tmp_path: Path,
) -> None:
    okx_sdk.save_config(
        okx_sdk.OKXConfig(
            api_key="live-key",
            api_secret="live-secret",
            passphrase="live-passphrase",
            profile="live",
            expected_uid="live-uid",
        )
    )

    status_response = client.get("/quant/okx/config/status")
    check_response = client.post("/quant/okx/config/check")
    reuse_response = client.put(
        "/quant/okx/config",
        json={
            "expected_uid": "demo-uid",
            "allowed_swap_instruments": ["BTC-USDT-SWAP"],
            "max_swap_leverage": 3,
        },
    )

    assert status_response.status_code == 200
    assert status_response.json()["environment"] == "live"
    assert status_response.json()["configured"] is False
    assert "demo_profile" in status_response.json()["missing_fields"]
    assert check_response.status_code == 409
    assert reuse_response.status_code == 400
    assert "required for initial setup" in reuse_response.json()["detail"]
    assert "live-key" not in status_response.text
    assert "live-secret" not in check_response.text
    assert "live-secret" not in reuse_response.text
    assert (tmp_path / "okx.json").exists()


def test_config_routes_reject_remote_clients_without_api_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "okx.json"
    monkeypatch.setattr(okx_sdk, "config_path", lambda: path)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    remote_client = TestClient(api_server.app, client=("203.0.113.10", 50000))

    read_response = remote_client.get("/quant/okx/config/status")
    write_response = remote_client.put("/quant/okx/config", json=_payload())
    check_response = remote_client.post("/quant/okx/config/check")

    assert read_response.status_code == 403
    assert write_response.status_code == 403
    assert check_response.status_code == 403
    assert "demo-api-key" not in write_response.text
    assert "demo-api-secret" not in write_response.text
    assert not path.exists()
