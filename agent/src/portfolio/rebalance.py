"""Deterministic allocation proposals; this module has no execution transport."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.portfolio.fx import from_usd
from src.portfolio.imports import aware_time


def propose_rebalance(snapshot: dict[str, Any], target_weights: dict[str, Any]) -> dict[str, Any]:
    """Compare a complete, dated snapshot with user-supplied allocation fractions.

    Args:
        snapshot: Immutable portfolio snapshot including FX and source metadata.
        target_weights: Canonical holding IDs (or CASH) to fractions summing to one.

    Returns:
        Value-only proposals in the snapshot's display currency, never orders.

    Raises:
        ValueError: If evidence is incomplete/stale or the target is malformed.
    """
    if not snapshot.get("complete") or snapshot.get("fx", {}).get("stale"):
        raise ValueError("rebalancing requires a complete snapshot and a successful FX refresh")
    for account in snapshot["accounts"]:
        metadata = account.get("import_metadata")
        if metadata and aware_time(metadata["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("imported evidence is stale; import a new export before rebalancing")
    valuation = snapshot["valuation"]
    if Decimal(str(valuation.get("unpriced_or_other_usd", 0))) != 0 or any(
        not row.get("priced") for row in snapshot["positions"]
    ):
        raise ValueError("rebalancing requires every included asset to be identified and priced")
    current = {row["canonical_id"]: Decimal(str(row["market_value_usd"])) for row in snapshot["combined_holdings"]}
    current["CASH"] = Decimal(str(valuation["cash_usd"]))
    if any(value < 0 for value in current.values()):
        raise ValueError("allocation proposals currently support long-only holdings")
    if not isinstance(target_weights, dict) or not target_weights or set(target_weights) - current.keys():
        raise ValueError("targets must name held canonical_id values or CASH")
    target: dict[str, Decimal] = {}
    for key, raw in target_weights.items():
        try:
            if isinstance(raw, bool):
                raise ValueError("boolean allocation")
            weight = Decimal(str(raw))
            if not weight.is_finite() or not 0 <= weight <= 1:
                raise ValueError("invalid weight")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("target weights must be finite fractions between 0 and 1") from exc
        target[key] = weight
    if abs(sum(target.values()) - Decimal(1)) > Decimal("0.00000001"):
        raise ValueError("target weights must sum to 1")
    total = sum(current.values())
    if total <= 0:
        raise ValueError("portfolio value must be positive")
    currency = snapshot["display_currency"]
    rates = {k: Decimal(str(v)) for k, v in snapshot["fx"]["rates"].items()}
    rows = []
    for key in sorted(current):
        weight = target.get(key, Decimal(0))
        desired = total * weight
        rows.append({
            "canonical_id": key, "current_weight": float(current[key] / total),
            "target_weight": float(weight), "drift": float(weight - current[key] / total),
            "current_value": float(from_usd(current[key], currency, rates)),
            "target_value": float(from_usd(desired, currency, rates)),
            "change_value": float(from_usd(desired - current[key], currency, rates)),
        })
    return {
        "mode": "proposal_only", "snapshot_id": snapshot["snapshot_id"],
        "created_at": snapshot["created_at"], "currency": currency,
        "fx_as_of": snapshot["fx"].get("as_of"), "rows": rows,
        "limitations": ["No orders, quantities, taxes, fees or execution prices are generated.",
                        "Cash and assets absent from an imported export are outside this allocation."]
                        + snapshot.get("warnings", []),
    }
