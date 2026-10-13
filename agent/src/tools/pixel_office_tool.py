"""Agent surface for shared research task planning."""

import json
from typing import Any
from src.agent.tools import BaseTool
from src.pixel_agents.office import OfficeManager, office_action


class PixelOfficeTool(BaseTool):
    name = "pixel_office"
    description = (
        "Manage the shared manual research-task board. list reads agents/tasks; create takes task_id, title, description, "
        "optional assigned_to and priority (low/normal/high); assign takes task_id, agent_id; complete takes task_id and "
        "a truthful result; rank takes agent_id and rank (Operator/Senior/Lead). Assignment does not execute a task. "
        "Ranks do not grant trading permissions. Never mark a task complete without a real result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "create", "assign", "complete", "rank"]},
            "arguments": {"type": "object"},
        },
        "required": ["action"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        return json.dumps(
            office_action(OfficeManager(), kwargs["action"], kwargs.get("arguments") or {}), ensure_ascii=False
        )
