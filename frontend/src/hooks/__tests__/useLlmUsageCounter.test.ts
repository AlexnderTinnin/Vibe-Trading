import { act, renderHook } from "@testing-library/react";
import { useLlmUsageCounter } from "../useLlmUsageCounter";

// One llm_usage event per iteration, exactly as the backend emits it:
// { **usage_delta, iter, attempt_id }.
const DELTA_1 = { input_tokens: 100, output_tokens: 50, total_tokens: 150 };
const DELTA_2 = { input_tokens: 200, output_tokens: 80, total_tokens: 280 };

describe("useLlmUsageCounter", () => {
  it("accumulates input/output tokens across iterations of one attempt", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, DELTA_1));
    act(() => result.current.record("A", 2, DELTA_2));

    expect(result.current.input).toBe(300);
    expect(result.current.output).toBe(130);
    expect(result.current.calls).toBe(2);
  });

  it("does not double-count a replayed (attemptId, iter) pair", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, DELTA_1));
    act(() => result.current.record("A", 2, DELTA_2));
    act(() => result.current.record("A", 1, DELTA_1));

    expect(result.current.input).toBe(300);
    expect(result.current.output).toBe(130);
    expect(result.current.calls).toBe(2);
  });

  it("resets to zero when the attempt changes, then accumulates the new attempt", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, DELTA_1));
    act(() => result.current.record("A", 2, DELTA_2));
    act(() => result.current.record("B", 1, DELTA_1));

    expect(result.current.input).toBe(100);
    expect(result.current.output).toBe(50);
    expect(result.current.calls).toBe(1);
  });

  it("scopes replay memory to the current attempt", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, DELTA_1));
    act(() => result.current.record("B", 1, DELTA_2));
    // Replayed (B,1): dedup still applies after the A→B reset.
    act(() => result.current.record("B", 1, DELTA_2));

    expect(result.current.input).toBe(200);
    expect(result.current.output).toBe(80);
    expect(result.current.calls).toBe(1);
  });

  it("reset() zeroes totals and replay memory", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, DELTA_1));
    act(() => result.current.reset());

    expect(result.current.input).toBe(0);
    expect(result.current.output).toBe(0);
    expect(result.current.calls).toBe(0);

    act(() => result.current.record("A", 1, DELTA_1));
    expect(result.current.input).toBe(100);
  });

  it("treats missing, null, non-finite, and negative fields as zero — never NaN", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => result.current.record("A", 1, { input_tokens: null, output_tokens: undefined }));
    act(() => result.current.record("A", 2, { input_tokens: Number.NaN, output_tokens: -5 }));
    act(() => result.current.record("A", 3, { input_tokens: "120" }));

    expect(result.current.input).toBe(0);
    expect(result.current.output).toBe(0);
    expect(result.current.calls).toBe(3);
  });

  it("accumulates two records flushed in one batch without re-render", () => {
    const { result } = renderHook(() => useLlmUsageCounter());

    act(() => {
      result.current.record("A", 1, DELTA_1);
      result.current.record("A", 2, DELTA_2);
    });

    expect(result.current.input).toBe(300);
    expect(result.current.output).toBe(130);
  });
});
