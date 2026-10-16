"""Shared fixtures for scheduling tests."""

from __future__ import annotations

import pytest

from src.scheduling.registry import _INTERNAL_CALLABLES


@pytest.fixture
def clean_registry():
    """Snapshot/restore the internal-callable registry.

    Test registrations must never leak into other tests or into the
    production (empty-until-T8/T9) allowlist surface.
    """
    saved = dict(_INTERNAL_CALLABLES)
    try:
        yield _INTERNAL_CALLABLES
    finally:
        _INTERNAL_CALLABLES.clear()
        _INTERNAL_CALLABLES.update(saved)
