"""Shared manual research-task board. It never dispatches broker operations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root

DB_FILENAME = "pixel-office.sqlite3"


def default_office_path() -> Path:
    """Resolve storage at use time, respecting the selected runtime root."""
    return get_runtime_root() / DB_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Agent:
    id: str
    name: str
    role: str
    rank: str = "Operator"
    color: str = "#c59259"
    office: str = "research"
    specialities: list[str] = field(default_factory=list)
    status: str = "idle"
    load: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Task:
    id: str
    title: str
    description: str
    workstream: str = "research"
    priority: str = "normal"
    owner: str = "user"
    status: str = "queued"
    assigned_to: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: str, field_name: str, *, maximum: int = 2000, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{field_name} must be {'0' if empty else '1'} to {maximum} characters")
    return value.strip()


class OfficeManager:
    """Transactional task storage shared by Web, CLI and the agent tool.

    Assignment and ranks are organizational metadata, not execution state or
    trading authority. Completion is an explicit user/agent-recorded result.
    """

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(storage_path) if storage_path else default_office_path()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        defaults = json.loads(Path(__file__).with_name("defaults.json").read_text())
        self.office_name = defaults["office_name"]
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            for item in defaults["agents"]:
                db.execute(
                    "INSERT OR IGNORE INTO agents VALUES (?, ?)", (item["id"], json.dumps(Agent(**item).to_dict()))
                )
        self.storage_path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.storage_path, timeout=10)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def add_agent(
        self, *, agent_id: str, name: str, role: str, specialties: list[str] | None = None, office: str = "research"
    ) -> Agent:
        """Add a distinct planning role; an existing role is never overwritten."""
        agent = Agent(
            id=_text(agent_id, "id", maximum=100),
            name=_text(name, "name", maximum=100),
            role=_text(role, "role", maximum=100),
            specialities=specialties or [],
            office=office,
        )
        try:
            with self._db() as db:
                db.execute("INSERT INTO agents VALUES (?, ?)", (agent.id, json.dumps(agent.to_dict())))
        except sqlite3.IntegrityError as exc:
            raise ValueError("agent id already exists") from exc
        return agent

    def create_task(
        self,
        *,
        task_id: str,
        title: str,
        description: str = "",
        workstream: str = "research",
        priority: str = "normal",
        owner: str = "user",
        assigned_to: str | None = None,
    ) -> Task:
        """Create a task, optionally assigned, in one transaction."""
        if priority not in {"low", "normal", "high"}:
            raise ValueError("priority must be low, normal or high")
        task = Task(
            id=_text(task_id, "task_id", maximum=100),
            title=_text(title, "title", maximum=200),
            description=_text(description, "description", empty=True),
            workstream=_text(workstream, "workstream", maximum=100),
            priority=priority,
            owner=_text(owner, "owner", maximum=100),
        )
        try:
            with self._db() as db:
                if assigned_to is not None:
                    if not db.execute("SELECT id FROM agents WHERE id=?", (assigned_to,)).fetchone():
                        raise ValueError("agent not found")
                    task.assigned_to, task.status = assigned_to, "assigned"
                db.execute("INSERT INTO tasks VALUES (?, ?)", (task.id, json.dumps(task.to_dict())))
        except sqlite3.IntegrityError as exc:
            raise ValueError("task id already exists") from exc
        return task

    def assign_task(self, task_id: str, agent_id: str | None = None) -> Task:
        """Assign/reassign one unfinished task without changing any other task."""
        with self._db() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task not found")
            task = Task(**json.loads(row[0]))
            if task.status == "completed":
                raise ValueError("completed tasks cannot be assigned")
            candidates = [json.loads(r[0]) for r in db.execute("SELECT payload FROM agents ORDER BY id")]
            if agent_id is None:
                agent_id = next((a["id"] for a in candidates if a["role"] == task.workstream), None)
            if not any(a["id"] == agent_id for a in candidates):
                raise ValueError("no matching agent; choose an agent explicitly")
            task.assigned_to, task.status, task.updated_at = agent_id, "assigned", _utc_now()
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task.to_dict()), task_id))
        return task

    def complete_task(self, task_id: str, result: str) -> Task:
        """Record completion, preserving an already-completed task's result."""
        result = _text(result, "result")
        with self._db() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task not found")
            task = Task(**json.loads(row[0]))
            if task.status != "completed":
                task.status, task.result, task.updated_at = "completed", result, _utc_now()
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task.to_dict()), task_id))
        return task

    def set_rank(self, agent_id: str, rank: str) -> Agent:
        """Change an organizational label; it grants no permissions."""
        if rank not in {"Operator", "Senior", "Lead"}:
            raise ValueError("rank must be Operator, Senior or Lead")
        with self._db() as db:
            row = db.execute("SELECT payload FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("agent not found")
            agent = Agent(**json.loads(row[0]))
            agent.rank = rank
            db.execute("UPDATE agents SET payload=? WHERE id=?", (json.dumps(agent.to_dict()), agent_id))
        return agent

    def dashboard(self) -> dict[str, Any]:
        """Read one coherent snapshot; derive each agent's load from its tasks."""
        with self._db() as db:
            agents = [json.loads(r[0]) for r in db.execute("SELECT payload FROM agents ORDER BY rowid")]
            tasks = [json.loads(r[0]) for r in db.execute("SELECT payload FROM tasks ORDER BY rowid")]
        for agent in agents:
            agent["load"] = sum(t["assigned_to"] == agent["id"] and t["status"] != "completed" for t in tasks)
            agent["status"] = "busy" if agent["load"] else "idle"
        return {
            "office_name": self.office_name,
            "agents": agents,
            "tasks": tasks,
            "agent_count": len(agents),
            "task_count": len(tasks),
            "completed": sum(t["status"] == "completed" for t in tasks),
            "pending": sum(t["status"] != "completed" for t in tasks),
            "mode": "manual_planning",
        }

    def list_agents(self) -> list[dict[str, Any]]:
        return self.dashboard()["agents"]

    def list_tasks(self) -> list[dict[str, Any]]:
        return self.dashboard()["tasks"]


def office_action(manager: OfficeManager, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the bounded action vocabulary used on every surface."""
    methods = {
        "create": manager.create_task,
        "assign": manager.assign_task,
        "complete": manager.complete_task,
        "rank": manager.set_rank,
    }
    if action == "list":
        return manager.dashboard()
    if action not in methods:
        raise ValueError("unknown office action")
    methods[action](**arguments)
    return manager.dashboard()
