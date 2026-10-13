"""Shipping rename routes must keep search's conversation labels current."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from src.api.sessions_routes import UpdateSessionRequest, register_sessions_routes
from src.session.events import EventBus
from src.session.search import SessionSearchIndex
from src.session.service import SessionService
from src.session.store import SessionStore
from src.tools.session_search_tool import SessionSearchTool


@pytest.mark.parametrize("automatic", [False, True])
def test_registered_title_route_updates_actual_search(tmp_path, monkeypatch, automatic):
    index = SessionSearchIndex(tmp_path / "search.db")
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: index)
    monkeypatch.setattr("src.session.search.get_shared_index", lambda: index)
    service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    original = "volatility synthetic prompt"
    session = service.create_session(title=original)
    sibling = service.create_session(title="Unrelated conversation")
    # The auto-title route reads a real user message, admitted directly to the
    # store/index to avoid invoking an agent or provider during fixture setup.
    from src.session.models import Message

    service.store.append_message(
        Message(session_id=session.session_id, role="user", content=original)
    )
    index.index_message(session.session_id, "user", original)
    before = index.search("volatility")[0]
    host = SimpleNamespace(
        require_auth=lambda: None,
        require_event_stream_auth=lambda: None,
        _get_session_service=lambda: service,
        _validate_path_param=lambda value, kind: None,
    )
    # This is the route module's documented host-composition boundary. Actual
    # route, persistence, index and search tool implementations remain intact.
    monkeypatch.setitem(sys.modules, "api_server", host)
    app = FastAPI()
    register_sessions_routes(app)
    title = "Renamed synthetic analysis"
    if automatic:
        from src.providers import chat

        class OwnedTitleProvider:
            def chat(self, messages, timeout):
                return SimpleNamespace(content=title)

            def close(self):
                pass

        monkeypatch.setattr(chat, "ChatLLM", OwnedTitleProvider)
        route = next(
            r for r in app.routes if r.path == "/sessions/{session_id}/title/auto"
        )
        result = asyncio.run(route.endpoint(session.session_id))
    else:
        route = next(
            r
            for r in app.routes
            if r.path == "/sessions/{session_id}" and "PATCH" in r.methods
        )
        result = asyncio.run(
            route.endpoint(session.session_id, UpdateSessionRequest(title=title))
        )
    try:
        assert result["status"] == "updated"
        assert service.store.get_session(session.session_id).title == title
        reply = json.loads(SessionSearchTool().execute(query="volatility"))
        assert reply["status"] == "ok"
        match = reply["results"][0]
        assert match["title"] == title
        assert match["session_id"] == session.session_id
        assert match["message_count"] == before.message_count == 1
        assert match["started_at"] == before.started_at
        assert (
            service.store.get_session(sibling.session_id).title
            == "Unrelated conversation"
        )
        assert (
            index._get_conn()
            .execute("SELECT title FROM sessions WHERE id = ?", (sibling.session_id,))
            .fetchone()[0]
            == "Unrelated conversation"
        )
    finally:
        index.close()
