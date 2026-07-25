"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture(autouse=True)
def _reset_env_config(monkeypatch: pytest.MonkeyPatch):
    """Keep developer dotenv/cache state from leaking into isolated tests."""
    from src.config.accessor import reset_env_config

    # A developer may intentionally enable the persistent loader cache in
    # agent/.env. Tests must opt in themselves or mocked provider failures can
    # be hidden by unrelated real cache entries from the user's home directory.
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "0")
    reset_env_config()
    yield
    reset_env_config()
