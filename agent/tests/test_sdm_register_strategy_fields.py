"""A strategy registration keeps how the strategy trades, not just its signal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.tools.sdm_register_tool import SdmRegisterTool

RULE_FIELDS = ("entry_rules", "exit_rules", "position_sizing")


@pytest.fixture()
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SdmRegisterTool:
    """The register tool backed by a fresh store, so registrations cannot leak."""
    monkeypatch.setenv(
        "VIBE_TRADING_STRATEGY_STORE_DB_PATH", str(tmp_path / "store.db")
    )
    import src.strategy_store._shared as shared

    monkeypatch.setattr(shared, "_store", None)
    return SdmRegisterTool()


def _register(tool: SdmRegisterTool, **kwargs: Any) -> dict[str, Any]:
    payload = {
        "artifact_type": "strategy",
        "name": "inverse vol weighting",
        "universe": "equity_hk",
        **kwargs,
    }
    envelope = json.loads(tool.execute(**payload))
    assert envelope["status"] == "ok", envelope
    return envelope["artifact"]


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("equal weight, max 10 positions", "equal weight, max 10 positions"),
        (
            ["持有满 5 个交易日", "position_pnl < -0.05"],
            "持有满 5 个交易日\nposition_pnl < -0.05",
        ),
        ([], None),
    ],
    ids=["string", "list", "empty-list"],
)
def test_rule_fields_are_stored_as_text(
    tool: SdmRegisterTool, given: object, stored: str | None
) -> None:
    artifact = _register(tool, **{field: given for field in RULE_FIELDS})

    assert [artifact[field] for field in RULE_FIELDS] == [stored] * 3


def test_omitted_rules_stay_none_rather_than_blank(tool: SdmRegisterTool) -> None:
    """None reads as "not stated"; an empty string would claim there are none."""
    artifact = _register(tool, signal_definition="1/sigma")

    assert [artifact[field] for field in RULE_FIELDS] == [None] * 3


def test_rules_passed_as_lists_like_the_old_skill_example_register(
    tool: SdmRegisterTool,
) -> None:
    """The strategy-dev-manager example used to pass extracted rules as lists."""
    artifact = _register(
        tool,
        entry_rules=[
            "normalized_order_flow < -2.0 (heavy selling, buy signal)",
            "normalized_order_flow > 2.0 (heavy buying, sell signal)",
        ],
        exit_rules=[
            "hold_days >= 5 (time-based exit)",
            "position_pnl < -0.05 (stop-loss)",
        ],
        position_sizing="equal_weight",
    )

    assert artifact["exit_rules"] == (
        "hold_days >= 5 (time-based exit)\nposition_pnl < -0.05 (stop-loss)"
    )
    assert artifact["position_sizing"] == "equal_weight"


def test_rule_fields_are_in_the_tool_schema(tool: SdmRegisterTool) -> None:
    """A field the schema does not expose cannot be written by an agent."""
    props = tool.parameters["properties"]

    for field in RULE_FIELDS:
        assert props[field]["type"] == "string", f"{field} missing from the schema"
