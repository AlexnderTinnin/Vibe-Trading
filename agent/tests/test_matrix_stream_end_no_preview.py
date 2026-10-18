"""Regression: a Matrix streaming reply must not be dropped when no preview edit ever succeeded."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("nio")

from src.channels.matrix import MatrixChannel, _StreamBuf


class _FakeResponse:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


def _make_channel() -> MatrixChannel:
    return MatrixChannel({}, bus=None)


class TestStreamEndWithoutPreview:
    """If every periodic preview edit failed (buf.event_id stayed None), the
    accumulated text must still reach the room as a fresh message, never be
    silently dropped.
    """

    def test_missing_event_id_still_sends_the_accumulated_reply(self) -> None:
        channel = _make_channel()
        chat_id = "!room:example.org"
        channel._stream_bufs[chat_id] = _StreamBuf(text="Hello, world!", event_id=None)

        sent: list[tuple[str, dict]] = []

        async def fake_send_room_content(room_id: str, content: dict) -> _FakeResponse:
            sent.append((room_id, content))
            return _FakeResponse("evt-new")

        channel._send_room_content = fake_send_room_content

        asyncio.run(channel.send_delta(chat_id, "", {"_stream_end": True}))

        assert len(sent) == 1
        room_id, content = sent[0]
        assert room_id == chat_id
        assert content["body"] == "Hello, world!"
        assert "m.relates_to" not in content
        assert chat_id not in channel._stream_bufs

    def test_existing_event_id_still_sends_an_edit(self) -> None:
        channel = _make_channel()
        chat_id = "!room:example.org"
        channel._stream_bufs[chat_id] = _StreamBuf(text="Hello, world!", event_id="evt-preview")

        sent: list[tuple[str, dict]] = []

        async def fake_send_room_content(room_id: str, content: dict) -> _FakeResponse:
            sent.append((room_id, content))
            return _FakeResponse("evt-preview")

        channel._send_room_content = fake_send_room_content

        asyncio.run(channel.send_delta(chat_id, "", {"_stream_end": True}))

        assert len(sent) == 1
        content = sent[0][1]
        assert content["m.relates_to"]["event_id"] == "evt-preview"

    def test_empty_text_with_no_preview_sends_nothing(self) -> None:
        channel = _make_channel()
        chat_id = "!room:example.org"
        channel._stream_bufs[chat_id] = _StreamBuf(text="", event_id=None)

        sent: list[tuple[str, dict]] = []

        async def fake_send_room_content(room_id: str, content: dict) -> _FakeResponse:
            sent.append((room_id, content))
            return _FakeResponse("evt-new")

        channel._send_room_content = fake_send_room_content

        asyncio.run(channel.send_delta(chat_id, "", {"_stream_end": True}))

        assert not sent


def test_failed_preview_then_final_send_uses_real_payload_builder():
    """Exercise buffering, preview failure and final thread delivery together."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def scenario():
        channel = _make_channel()
        channel.client = SimpleNamespace(
            room_send=AsyncMock(side_effect=[RuntimeError("temporary failure"), _FakeResponse("final")]),
            room_typing=AsyncMock(),
        )
        metadata = {"thread_root_event_id": "root", "thread_reply_to_event_id": "reply"}
        await channel.send_delta("room", "**Answer**", metadata)
        assert channel._stream_bufs["room"].event_id is None
        await channel.send_delta("room", "", {**metadata, "_stream_end": True})
        sent = channel.client.room_send.call_args.kwargs
        assert sent["content"]["body"] == "**Answer**"
        assert sent["content"]["formatted_body"].strip() == "<p><strong>Answer</strong></p>"
        assert sent["content"]["m.relates_to"]["rel_type"] == "m.thread"
        assert sent["content"]["m.relates_to"]["event_id"] == "root"
        assert "m.new_content" not in sent["content"]
        assert sent["ignore_unverified_devices"] is True
        assert "room" not in channel._stream_bufs

    asyncio.run(scenario())
