"""CLI tests for secure OKX Demo connector configuration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli import _legacy as legacy
from src.trading.connectors.okx import sdk as ox

pytestmark = pytest.mark.unit


def test_connector_configure_parser_routes_okx_options() -> None:
    parser = legacy._build_parser()
    args = parser.parse_args(
        [
            "connector",
            "configure",
            "okx-paper-trade",
            "--expected-uid",
            "uid-1",
            "--swap-instruments",
            "BTC-USDT-SWAP",
            "--max-swap-leverage",
            "8",
            "--yes",
        ]
    )
    with patch.object(legacy, "cmd_connector_configure", return_value=0) as configure:
        assert legacy._dispatch_connector(args) == 0
    configure.assert_called_once_with(
        "okx-paper-trade",
        host="127.0.0.1",
        port=None,
        client_id=77,
        account=None,
        expected_uid="uid-1",
        swap_instruments="BTC-USDT-SWAP",
        max_swap_leverage=8.0,
        yes=True,
    )


def test_okx_demo_configure_reads_secrets_from_environment(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setattr(ox, "get_runtime_root", lambda: tmp_path)
    monkeypatch.setenv("OKX_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "demo-pass")
    monkeypatch.setenv("OKX_PROXY", "http://proxy-user:proxy-pass@127.0.0.1:7890")

    result = legacy.cmd_connector_configure(
        "okx-paper-trade",
        expected_uid="demo-uid",
        swap_instruments="BTC-USDT-SWAP",
        max_swap_leverage=8,
        yes=True,
    )

    assert result == legacy.EXIT_SUCCESS
    cfg = ox.load_config()
    assert cfg.profile == "paper"
    assert cfg.api_key == "demo-key"
    assert cfg.api_secret == "demo-secret"
    assert cfg.passphrase == "demo-pass"
    assert cfg.expected_uid == "demo-uid"
    assert cfg.allowed_swap_instruments == ("BTC-USDT-SWAP",)
    assert cfg.max_swap_leverage == 8
    assert cfg.proxy == "http://proxy-user:proxy-pass@127.0.0.1:7890"
    output = capsys.readouterr().out
    assert "demo-secret" not in output
    assert "demo-pass" not in output
    assert "proxy-pass" not in output
    assert "Demo only" in output


def test_okx_live_configure_is_rejected_without_prompting(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ox, "get_runtime_root", lambda: tmp_path)
    with patch.object(legacy.Prompt, "ask", side_effect=AssertionError("must not prompt")):
        result = legacy.cmd_connector_configure("okx-live-trade", yes=True)
    assert result == legacy.EXIT_USAGE_ERROR
    assert not ox.config_path().exists()


def test_okx_configure_parser_exposes_no_secret_flags() -> None:
    parser = legacy._build_parser()
    connector_action = next(action for action in parser._actions if action.dest == "command")
    connector_parser = connector_action.choices["connector"]
    sub_action = next(action for action in connector_parser._actions if action.dest == "connector_command")
    configure = sub_action.choices["configure"]
    option_strings = {option for action in configure._actions for option in action.option_strings}
    assert "--api-secret" not in option_strings
    assert "--passphrase" not in option_strings
