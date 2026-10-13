"""Per-response stream buffering and progress callbacks for AgentLoop.

A fresh instance is created for each model iteration. Its state deliberately
survives the one provider retry exactly as the former closure state did.
"""
from __future__ import annotations

import time as _time
from typing import Any, Callable

from src.agent.grounding import GroundingLedger


class TurnStream:
    """Hold unchecked measurements and figures fences; throttle progress events."""

    def __init__(
        self, *, grounding: GroundingLedger | None, emit: Callable[[str, dict[str, Any]], None],
        iteration: int, heartbeat_interval: Callable[[], float], reasoning_interval: Callable[[], float],
    ) -> None:
        self.grounding = grounding
        self.emit = emit
        self.iteration = iteration
        self.heartbeat_interval = heartbeat_interval
        self.reasoning_interval = reasoning_interval
        self.thinking_chunks: list[str] = []
        self.reasoning_chars = 0
        self.reasoning_tail = ""
        self.last_reasoning_emit: float | None = None
        self.buffer_text_output = bool(grounding and grounding.should_buffer_output)
        self.streamed_chars = 0
        self.stream_total = 0
        self.stream_frozen = False
        self.last_waiting_emit: float | None = None

    def on_text(self, delta: str) -> None:
        self.thinking_chunks.append(delta)
        self.stream_total += len(delta)
        if self.buffer_text_output or self.stream_frozen:
            return
        # Neither the figures block nor an unchecked measurement streams
        # (see streamable_length). Only a fence character or a digit
        # can hold text back, so any other chunk is emitted as it
        # arrives instead of re-parsing the whole answer.
        if self.streamed_chars + len(delta) == self.stream_total and not any(
            char in "`~" or char.isdigit() for char in delta
        ):
            self.emit("text_delta", {"delta": delta, "iter": self.iteration})
            self.streamed_chars = self.stream_total
            return
        text = "".join(self.thinking_chunks)
        safe = (
            self.grounding.streamable_length(text)
            if self.grounding is not None
            else len(text)
        )
        if self.grounding is not None and safe < len(text):
            # Held at a measurement: nothing after it streams this turn.
            self.stream_frozen = self.grounding.measurement_start(text) == safe
        if safe > self.streamed_chars:
            self.emit(
                "text_delta",
                {"delta": text[self.streamed_chars:safe], "iter": self.iteration},
            )
            self.streamed_chars = safe

    def on_waiting(self) -> None:
        now = _time.monotonic()
        if self.last_waiting_emit is None or now - self.last_waiting_emit >= self.heartbeat_interval():
            self.last_waiting_emit = now
            self.emit("llm_waiting", {"iter": self.iteration})

    def on_reasoning(self, delta: str) -> None:
        # Throttled: long reasoning streams produce hundreds of
        # chunks; emitting each one floods the SSE replay buffer
        # and evicts tool_call/text_delta events. The first chunk
        # of each iteration always emits immediately so the UI
        # flips to "Reasoning…" without delay.
        self.reasoning_chars += len(delta)
        # Rolling tail rides the already-throttled emit so the UI
        # can whisper the current thought; a bounded window keeps
        # replay-buffer pressure flat regardless of trace length.
        self.reasoning_tail = (self.reasoning_tail + delta)[-600:]
        now = _time.monotonic()
        if (
            self.last_reasoning_emit is not None
            and now - self.last_reasoning_emit < self.reasoning_interval()
        ):
            return
        self.last_reasoning_emit = now
        reasoning_event = {
            "iter": self.iteration,
            "chars": self.reasoning_chars,
        }
        if not self.buffer_text_output:
            reasoning_event["tail"] = self.reasoning_tail
        self.emit("reasoning_delta", reasoning_event)

    def reset_for_retry(self) -> None:
        """Discard the failed text attempt with the existing retry semantics."""
        self.thinking_chunks.clear()
        self.streamed_chars = 0
        self.stream_total = 0
        self.stream_frozen = False
        self.reasoning_chars = 0
        self.last_reasoning_emit = None
