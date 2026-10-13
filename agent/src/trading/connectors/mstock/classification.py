"""Curated mStock operations; unsupported or unknown calls never become reads."""

from src.live.classification import ToolClass

MSTOCK_TOOL_CLASS = {
    "get_positions": ToolClass.READ,
    "get_account_snapshot": ToolClass.READ,
    "place_order": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
}
