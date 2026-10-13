"""Prepare a description-preserving child and validate its own backtest evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from src.strategy_store.adaptation import AdaptationPatch, register_adaptation

_DESCRIPTION_FIELDS = ("signal_definition", "entry_rules", "exit_rules", "position_sizing")


def prepare_adaptation(
    *, strategy_id: str, name: str, universe: str, config: dict[str, Any],
    position_sizing: str | None = None, store=None, evidence_store=None,
    today: date | None = None,
) -> dict:
    """Create a child, seed its config, and return the generation instructions.

    Args:
        strategy_id: Existing SDM strategy id, with its ``sdm:`` prefix.
        name: New child's name.
        universe: Description of the target universe.
        config: Explicit backtest config including codes and the date window.
        position_sizing: Optional allowed description change.
        store: Injectable SDM store; defaults to the shared store.
        evidence_store: Injectable evidence cache.
        today: Reference date for the existing parent freshness gate.

    Returns:
        Child identity, description, assigned paths and next tool calls.

    Raises:
        ValueError: Invalid inputs or inadequate/stale parent evidence.
    """
    from src.config.paths import get_runtime_root
    from src.strategy_discovery.evidence_store import EvidenceStore
    from src.strategy_store._shared import get_store

    if not isinstance(strategy_id, str) or not strategy_id.startswith("sdm:"):
        raise ValueError("adaptation requires an sdm: strategy id; a factor is not a strategy")
    for label, value in (("name", name), ("universe", universe)):
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f"{label} must be a non-empty string of at most 500 characters")
    if position_sizing is not None and (not isinstance(position_sizing, str) or not position_sizing.strip()):
        raise ValueError("position_sizing must be a non-empty description")
    if not isinstance(config, dict):
        raise ValueError("config must be a backtest configuration object")
    config = json.loads(json.dumps(config, allow_nan=False))
    if not isinstance(config.get("source"), str) or not config["source"].strip():
        raise ValueError("config requires an explicit data source (for example auto or local)")
    codes = config.get("codes")
    if (not isinstance(codes, list) or not codes or len(codes) > 500
            or any(not isinstance(code, str) or not code.strip() for code in codes)
            or len(set(codes)) != len(codes)):
        raise ValueError("config.codes must contain distinct non-empty instrument codes")
    try:
        start = date.fromisoformat(config["start_date"])
        end = date.fromisoformat(config["end_date"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("config requires ISO start_date and end_date") from exc
    if start >= end:
        raise ValueError("start_date must precede end_date")
    from backtest.runner import BacktestConfigSchema

    BacktestConfigSchema(**config)
    store = store if store is not None else get_store()
    evidence_store = evidence_store if evidence_store is not None else EvidenceStore()
    parent_id = strategy_id.removeprefix("sdm:")
    parent = store.get_artifact(parent_id)
    if parent is None or not parent.signal_definition:
        raise ValueError("parent strategy must have an explicit signal_definition")
    token = uuid.uuid4().hex
    run_dir = (get_runtime_root() / "runs" / f"adaptation-{token}").resolve()
    config["strategy_adaptation"] = {"token": token, "parent_id": strategy_id}
    description = {key: getattr(parent, key) for key in _DESCRIPTION_FIELDS}
    if position_sizing is not None:
        description["position_sizing"] = position_sizing
    binding = {
        "run_dir": str(run_dir), "parent_id": parent_id,
        "codes": codes, "start_date": config["start_date"], "end_date": config["end_date"],
        "strategy_adaptation": config["strategy_adaptation"], "description": description,
        "universe": universe.strip(),
    }
    child_id = register_adaptation(
        store, parent_id, AdaptationPatch(name=name.strip(), universe=universe.strip(), position_sizing=position_sizing),
        parent_evidence=evidence_store.get_rows(strategy_id=strategy_id),
        today=today or date.today(), run_binding=binding,
    )
    # Registration and binding are atomic. If disk creation fails, the child
    # remains CREATED without evidence and cannot be refreshed from another run.
    (run_dir / "code").mkdir(parents=True, exist_ok=False)
    (run_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "ok", "strategy_id": f"sdm:{child_id}", "derived_from": strategy_id,
        "artifact": asdict(store.get_artifact(child_id)), "run_dir": str(run_dir),
        "parent_evidence": [asdict(row) for row in evidence_store.get_rows(strategy_id=strategy_id)],
        "next_steps": [
            "Load strategy-generate. Generate code/signal_engine.py in the assigned run_dir from the child's description; preserve signal_definition, entry_rules and exit_rules.",
            "Run backtest with this run_dir. Do not reuse a parent's artifacts or alter the bound codes/date window/strategy_adaptation token.",
            "Call refresh_strategy_evidence(runs=[{strategy_id: the child id, run_dir: this directory}], scope='strategies'). Report the child's own evidence quality and warnings, never the parent's metrics as the child's.",
        ],
    }


def validate_adapted_run(strategy_id: str, run_dir: str | Path, *, store=None) -> None:
    """Refuse evidence from another run or changed/unsealed child artifacts.

    Hash checks bind the evidence to persisted backtest output. They do not
    certify that generated Python implements its natural-language description.
    """
    if not strategy_id.startswith("sdm:"):
        return
    from src.strategy_store._shared import get_store

    store = store if store is not None else get_store()
    child_id = strategy_id.removeprefix("sdm:")
    child = store.get_artifact(child_id)
    if child is None or not child.derived_from:
        return
    binding = store.get_adaptation_binding(child_id)
    if not binding:
        raise ValueError("adapted strategy has no bound run; prepare a new adaptation before refreshing")
    root = Path(run_dir).resolve()
    if str(root) != binding["run_dir"] or child.run_dir != binding["run_dir"]:
        raise ValueError("adaptation evidence must come from the child's assigned run_dir")
    if child.derived_from != binding["parent_id"] or child.universe != binding["universe"]:
        raise ValueError("adaptation identity changed after preparation")
    if any(getattr(child, key) != value for key, value in binding["description"].items()):
        raise ValueError("adaptation description changed after preparation")
    if child.signal_engine_path != str(root / "code/signal_engine.py"):
        raise ValueError("adaptation must use its own signal_engine.py")
    try:
        def read(relative):
            path = root / relative
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                raise ValueError("adaptation artifact escapes its run directory")
            return path.read_bytes()

        config = json.loads(read("config.json"))
        for key in ("codes", "start_date", "end_date", "strategy_adaptation"):
            if config.get(key) != binding[key]:
                raise ValueError(f"adaptation config changed: {key}")
        card = json.loads(read("run_card.json"))
        manifest = {item["path"]: item["sha256"] for item in card["artifacts"]}
        required = ("config.json", "code/signal_engine.py", "artifacts/metrics.csv",
                    "artifacts/equity.csv", "artifacts/trades.csv")
        for relative in required:
            digest = hashlib.sha256(read(relative)).hexdigest()
            if digest != manifest.get(relative):
                raise ValueError(f"adaptation artifact is not sealed by the run card: {relative}")
        if card["reproducibility"].get("strategy_hash") != manifest["code/signal_engine.py"]:
            raise ValueError("run card does not identify the child's strategy code")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("adaptation requires a complete run card and its unchanged backtest artifacts") from exc
