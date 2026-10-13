"""Final-answer grounding orchestration, extracted from AgentLoop (#1624).

The ledger owns evidence and release policy. This module only coordinates
its existing repair/recovery/revision calls, trace events and stream release.
The loop receives an explicit decision about whether to continue iterating.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.agent.loop_streaming import TurnStream
from src.agent.trace import TraceWriter

if TYPE_CHECKING:
    from src.agent.loop import AgentLoop


@dataclass(frozen=True)
class GroundingDecision:
    """Updated correction state and the original loop's continue decision."""

    content: str
    revisions: int
    text_only: bool
    pending_draft: str
    retry: bool = False


def validate_and_release(
    loop: AgentLoop, *, final_content: str, grounding_revisions: int,
    grounding_correction_text_only: bool, pending_grounding_draft: str,
    forced_grounding_release: bool, syntax_fallback_emitted: bool,
    stream: TurnStream, trace: TraceWriter, react_trace: list, messages: list,
    iteration: int, current_iter: int, max_revisions: int,
) -> GroundingDecision:
    """Validate one final draft and preserve the bounded correction lifecycle.

    Args:
        loop: Current run owner, including ledger, events and fallback status.
        final_content: Candidate draft, or the last draft at the correction cap.
        grounding_revisions: Number of correction-path rejected drafts.
        grounding_correction_text_only: Whether this is a text-only correction.
        pending_grounding_draft: Last rejected draft retained for a forced release.
        forced_grounding_release: An unoffered tool call exhausted correction.
        syntax_fallback_emitted: A tool-syntax fallback already reached the stream.
        stream: This iteration's stream buffers and emission state.
        trace: Persisted run trace.
        react_trace: Returned compact trace, appended in place.
        messages: Conversation messages, appended in place.
        iteration: Current turn-relative iteration.
        current_iter: Session-wide iteration number.
        max_revisions: Existing correction budget supplied by the loop.

    Returns:
        Updated correction state; retry requests the loop's next iteration.
    """
    if loop._grounding is not None:
        validation = (
            loop._grounding.revalidate(final_content)
            if forced_grounding_release
            else loop._grounding.validate_final_answer(final_content)
        )
        if not validation.valid:
            # A draft whose only defect is a missing provenance
            # word (source / currency / symbol suffix) gets the
            # word appended, not another multi-minute model round.
            repaired = loop._grounding.repair_provenance(
                final_content, validation
            )
            if repaired is not None:
                # Non-recording: no model round produced this
                # text, and ``validation_count`` is the
                # rejected-draft number the user is shown.
                recheck = loop._grounding.revalidate(repaired)
                if recheck.valid:
                    trace.write(
                        {
                            "type": "answer_repaired",
                            "iter": current_iter,
                            "issues": validation.issues,
                        }
                    )
                    react_trace.append(
                        {"type": "answer_repaired", "issues": validation.issues}
                    )
                    final_content = repaired
                    validation = recheck
        if validation.valid:
            # The figures block is the model's declaration to
            # the gate, not answer text.
            final_content = validation.released_text
        if not validation.valid:
            if not forced_grounding_release:
                trace.write_text_entry(
                    {
                        "type": "answer_rejected",
                        "iter": current_iter,
                        "issues": validation.issues,
                    },
                    field="content",
                    value=final_content,
                    offload_kind=f"answer-rejected-{current_iter}",
                )
                if not stream.buffer_text_output and stream.streamed_chars:
                    # The stream showed this draft up to its first unchecked
                    # number; the next draft or the released answer replaces it.
                    loop._emit(
                        "stream_reset",
                        {"iter": current_iter, "reason": "grounding_rejected"},
                    )
                react_trace.append(
                    {
                        "type": "answer_rejected",
                        "issues": validation.issues,
                    }
                )
                messages.append(
                    {"role": "assistant", "content": final_content}
                )
            recovery = loop._grounding.recovery_action(validation)
            if not forced_grounding_release and recovery is not None and iteration < loop.max_iterations:
                # Explicit bounded recovery is the one case where
                # the next turn is allowed to research again.
                grounding_correction_text_only = False
                loop._grounding.record_recovery(recovery)
                trace.write(
                    {
                        "type": "grounding_recovery",
                        "iter": current_iter,
                        "action": recovery,
                    }
                )
                react_trace.append(
                    {"type": "grounding_recovery", "action": recovery}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"<system>{loop._grounding.recovery_prompt(recovery, validation)}</system>",
                    }
                )
                loop._emit(
                    "grounding_status",
                    {
                        "stage": "revising",
                        "round": loop._grounding.validation_count,
                        "issues": len(validation.issues),
                    },
                )
                final_content = ""
                return GroundingDecision(final_content, grounding_revisions, grounding_correction_text_only, pending_grounding_draft, retry=True)
            if not forced_grounding_release:
                messages.append(
                    {
                        "role": "user",
                        "content": f"<system>{loop._grounding.correction_prompt(validation)}</system>",
                    }
                )
            rejected_draft = final_content
            pending_grounding_draft = rejected_draft
            final_content = ""
            # The budget counts drafts rejected on this
            # correction path; the last one is released with
            # its figures cut. A draft that triggered bounded
            # recovery is not counted (it re-fetches evidence
            # rather than rewording), so a run that had to
            # resolve its symbol first still gets a corrected
            # draft.
            if not forced_grounding_release:
                grounding_revisions += 1
            if (
                not forced_grounding_release
                and iteration < loop.max_iterations
                and grounding_revisions < max_revisions
            ):
                grounding_correction_text_only = True
                loop._emit(
                    "grounding_status",
                    {
                        "stage": "revising",
                        "round": loop._grounding.validation_count,
                        "issues": len(validation.issues),
                    },
                )
                return GroundingDecision(final_content, grounding_revisions, grounding_correction_text_only, pending_grounding_draft, retry=True)
            # Out of revisions. The last draft is still the
            # analysis the user waited minutes for; release it
            # with the rejected figures cut out and re-checked
            # by the same gate. The canned refusal is only for
            # what cannot be cut: an identity finding, a run
            # that never observed a price, or a cut that still
            # fails validation.
            rejected_drafts = loop._grounding.validation_count
            released = loop._grounding.redacted_release(
                rejected_draft, validation
            )
            if released is not None:
                trace.write(
                    {
                        "type": "answer_released_redacted",
                        "iter": current_iter,
                        "issues": validation.issues,
                    }
                )
                react_trace.append(
                    {
                        "type": "answer_released_redacted",
                        "issues": validation.issues,
                    }
                )
                final_content = released
                loop._emit(
                    "grounding_status",
                    {
                        "stage": "released_redacted",
                        "removed": loop._grounding.figures_removed,
                    },
                )
                loop._released_fallback_reason = (
                    "final answer released with unverified figures "
                    f"redacted after {rejected_drafts} rejected drafts"
                )
            else:
                final_content = loop._grounding.safe_fallback()
                # Captured HERE, beside the redacted branch's
                # own count. Left unset, the reason was built
                # lazily at the end of the run from a
                # ``validation_count`` the release path's
                # rechecks had already moved.
                loop._released_fallback_reason = (
                    "final answer degraded to the deterministic "
                    f"fallback after {rejected_drafts} rejected "
                    "drafts could not be corrected within the "
                    "iteration budget"
                )
            loop._released_fallback = True
            loop._emit(
                "text_delta",
                {"delta": final_content, "iter": current_iter},
            )
        elif stream.buffer_text_output and not syntax_fallback_emitted:
            loop._emit(
                "text_delta",
                {"delta": final_content, "iter": current_iter},
            )
        elif not loop._released_fallback and not syntax_fallback_emitted:
            # Flush a held-back last line that never became a
            # figures fence; a stripped block leaves nothing.
            shown = "".join(stream.thinking_chunks)[:stream.streamed_chars]
            if not final_content.startswith(shown):
                # Stripping the block also trims the blank lines
                # the stream already showed before its fence.
                shown = shown.rstrip()
            if final_content.startswith(shown) and len(final_content) > len(shown):
                loop._emit(
                    "text_delta",
                    {"delta": final_content[len(shown):], "iter": current_iter},
                )
    return GroundingDecision(final_content, grounding_revisions, grounding_correction_text_only, pending_grounding_draft)
