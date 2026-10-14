"""Session search limits sessions rather than an approximate message sample."""

import json
from pathlib import Path

from src.session.search import SessionSearchIndex
from src.tools.session_search_tool import SessionSearchTool


def _populate(index: SessionSearchIndex) -> None:
    for session_id, count in [("long", 25), ("second", 1), ("third", 1)]:
        index.index_session(session_id, session_id, ts=1_700_000_000)
        for _ in range(count):
            index.index_message(session_id, "user", "portfolio volatility")


def test_actual_tool_returns_distinct_sessions_after_long_conversation(
    tmp_path: Path, monkeypatch
) -> None:
    index = SessionSearchIndex(tmp_path / "sessions.db")
    try:
        _populate(index)
        monkeypatch.setattr("src.session.search.get_shared_index", lambda: index)
        result = json.loads(
            SessionSearchTool().execute(query="volatility", max_results=3)
        )
        assert result["status"] == "ok"
        assert {row["session_id"] for row in result["results"]} == {
            "long",
            "second",
            "third",
        }
        assert len(result["results"]) == 3
        assert all(">>>volatility<<<" in row["snippet"] for row in result["results"])
        assert len(index.search("volatility", max_sessions=1)) == 1
    finally:
        index.close()


def test_ranked_cursor_stops_after_requested_distinct_sessions(tmp_path: Path) -> None:
    index = SessionSearchIndex(tmp_path / "sessions.db")
    try:
        for i in range(30):
            index.index_session(str(i), str(i))
            index.index_message(str(i), "user", "volatility")
        real = index._get_conn()
        consumed = []

        class Cursor:
            def fetchone(self):
                return self.cursor.fetchone()

            def __iter__(self):
                for row in self.cursor:
                    consumed.append(row)
                    yield row

            def fetchall(self):
                raise AssertionError("Search must not materialize every message")

        class Connection:
            def execute(self, sql, params):
                wrapper = Cursor()
                wrapper.cursor = real.execute(sql, params)
                return wrapper

        index._conn = Connection()
        matches = index.search("volatility", max_sessions=3)
        index._conn = real
        assert len(matches) == 3
        assert len(consumed) == 3
        assert [match.rank for match in matches] == sorted(
            match.rank for match in matches
        )
    finally:
        if index._conn is not real:
            index._conn = real
        index.close()
