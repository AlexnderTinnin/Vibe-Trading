"""Channel and provider credentials with no key label are scrubbed from text."""

from __future__ import annotations

import pytest

from src.tools.redaction import redact_text, redact_tool_result

_TELEGRAM = "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"

def test_telegram_bot_token_in_request_url_is_redacted() -> None:
    out = redact_text(f"401 for url: https://api.telegram.org/bot{_TELEGRAM}/sendMessage")
    assert _TELEGRAM not in out
    assert out == "401 for url: https://api.telegram.org/[redacted]/sendMessage"
    assert redact_text(out) == out


@pytest.mark.parametrize("token", ["gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4", "nvapi-" + "a1B2c3D4e5F6g7H8i9J0k1L2", _TELEGRAM])
def test_bare_token_is_redacted(token: str) -> None:
    out = redact_text(f"provider rejected {token} today")
    assert out == "provider rejected [redacted] today"
    assert redact_text(out) == out


def test_slack_webhook_secret_is_redacted() -> None:
    url = "https://hooks.slack.com/" + "services/T00000000/B00000000/" + "X" * 24
    out = redact_text(f"post to {url} failed")
    assert "X" * 24 not in out
    assert out == "post to https://hooks.slack.com/" + "services/T00000000/B00000000/[redacted] failed"
    assert redact_text(out) == out


def test_discord_webhook_secret_is_redacted() -> None:
    secret = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"
    out = redact_text(f"https://discord.com/api/webhooks/123456789012345678/{secret}")
    assert secret not in out
    assert out == "https://discord.com/api/webhooks/123456789012345678/[redacted]"


def test_lookalikes_are_left_alone() -> None:
    text = "12:30:45 ratio 1234567890:short gsk_short nvapi-x https://hooks.slack.com/" + "services/T0/B0/short"
    assert redact_text(text) == text


@pytest.mark.parametrize("bot_id", ["123456", "1234567", "12345678901"])
@pytest.mark.parametrize("in_url", [False, True])
def test_telegram_token_does_not_assume_an_eight_to_ten_digit_id(bot_id, in_url):
    # Telegram's own Bot API documentation uses a six-digit bot id.
    token = bot_id + ":" + "A" * 35
    text = f"https://api.telegram.org/bot{token}/getMe" if in_url else token
    out = redact_tool_result(text)
    assert token not in out
    assert "[redacted]" in out
    assert redact_text(out) == out


@pytest.mark.parametrize("host", ["hooks.slack-gov.com", "HOOKS.SLACK.COM"])
def test_webhook_host_variants_are_redacted_inside_results(host):
    import json

    secret = "X" * 24
    url = f"https://{host}/services/T00000000/B00000000/{secret}"
    out = json.loads(redact_tool_result(json.dumps({"error": f"request failed: {url}"})))
    assert secret not in out["error"]
    assert host in out["error"]
    assert "[redacted]" in out["error"]
