"""Exercise shared state through CLI, REST and tools without executing tasks."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.pixel_office_routes import register_pixel_office_routes
from src.pixel_agents.cli import main
from src.pixel_agents.office import OfficeManager
from src.tools.pixel_office_tool import PixelOfficeTool


def test_shared_surfaces_and_result_preservation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    main(["task", "--id", "brief", "--title", "Review", "--description", "Use observed sources"])
    capsys.readouterr()
    app = FastAPI()
    register_pixel_office_routes(app, lambda: None, lambda: None)
    with TestClient(app) as client:
        assert client.get("/api/pixel-office").json()["tasks"][0]["title"] == "Review"
        response = client.post("/api/pixel-office/actions", json={"action": "assign", "arguments": {"task_id": "brief", "agent_id": "research-analyst"}})
        assert response.status_code == 200
        assert response.json()["mode"] == "manual_planning"
        assert response.json()["tasks"][0]["status"] == "assigned"
        assert client.post("/api/pixel-office/actions", json={"action": "complete", "arguments": {"task_id": "brief", "result": " "}}).status_code == 400
    result = json.loads(PixelOfficeTool().execute(action="complete", arguments={"task_id": "brief", "result": "Observed result"}))
    assert result["completed"] == 1
    assert all(a["load"] == 0 for a in result["agents"])
    main(["complete", "--task-id", "brief", "--result", "replacement"])
    assert OfficeManager().list_tasks()[0]["result"] == "Observed result"
    with pytest.raises(ValueError, match="completed"):
        OfficeManager().assign_task("brief", "risk-guard")


def test_multiple_assignments_reassignment_and_partial_completion(tmp_path):
    board = OfficeManager(tmp_path / "board.db")
    board.create_task(task_id="a", title="A", assigned_to="research-analyst")
    board.create_task(task_id="b", title="B", assigned_to="research-analyst")
    board.assign_task("a", "research-analyst")
    assert board.list_agents()[0]["load"] == 2
    board.complete_task("a", "Done A")
    assert board.list_agents()[0]["load"] == 1
    board.assign_task("b", "risk-guard")
    agents = {a["id"]: a for a in board.list_agents()}
    assert agents["research-analyst"]["status"] == "idle"
    assert agents["risk-guard"]["load"] == 1
    before = board.dashboard()
    with pytest.raises(ValueError, match="no matching agent"):
        board.assign_task("b", "missing")
    assert board.dashboard() == before


def test_concurrent_process_like_managers_do_not_lose_updates(tmp_path):
    path = tmp_path / "office.db"
    def write(index):
        OfficeManager(path).create_task(task_id=str(index), title=f"Task {index}")
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write, range(24)))
    board = OfficeManager(path)
    assert len(board.list_tasks()) == 24
    with pytest.raises(ValueError, match="already exists"):
        board.create_task(task_id="0", title="Overwrite")
    assert board.list_tasks()[0]["title"].startswith("Task ")


def test_corrupt_store_is_never_replaced(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not a database")
    with pytest.raises(Exception, match="not a database"):
        OfficeManager(path)
    assert path.read_bytes() == b"not a database"


def test_api_guards_and_invalid_action(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    app = FastAPI()
    def deny():
        raise HTTPException(status_code=403)
    register_pixel_office_routes(app, lambda: None, deny)
    with TestClient(app) as client:
        assert client.get("/api/pixel-office").status_code == 200
        assert client.post("/api/pixel-office/actions", json={"action": "rank", "arguments": {"agent_id": "risk-guard", "rank": "Lead"}}).status_code == 403
    assert OfficeManager().list_agents()[1]["rank"] == "Operator"


@pytest.mark.parametrize("placement", ["before", "after"])
def test_cli_storage_option_and_errors(tmp_path, placement, capsys):
    path = tmp_path / "custom.sqlite3"
    args = ["rank", "--agent-id", "risk-guard", "--rank", "Senior"]
    option = ["--storage", str(path)]
    main(option + args if placement == "before" else args + option)
    assert json.loads(capsys.readouterr().out)["rank"] == "Senior"
    assert OfficeManager(path).list_agents()[1]["rank"] == "Senior"
    with pytest.raises(SystemExit) as failure:
        main(["assign", "--task-id", "missing", *option])
    assert failure.value.code == 2
