import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunData } from "../api";

async function loadApiModule() {
  vi.resetModules();
  return import("../api");
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function stubFetchWith(payload: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.stubGlobal("localStorage", {
    getItem: vi.fn(() => ""),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("GET /runs/{id} llm_usage contract", () => {
  it("parses llm_usage with cache keys into LlmUsage-shaped values", async () => {
    stubFetchWith({
      status: "success",
      run_id: "run-with-cache",
      elapsed_seconds: 12.5,
      llm_usage: {
        provider: "deepseek",
        model: "deepseek-v4-pro",
        totals: {
          input_tokens: 4821,
          output_tokens: 930,
          total_tokens: 5751,
          calls: 3,
          cache_read_tokens: 3120,
          cache_creation_tokens: 640,
        },
        per_iteration: [
          { iter: 1, input_tokens: 2000, output_tokens: 300, total_tokens: 2300 },
          { iter: 2, input_tokens: 1821, output_tokens: 430, total_tokens: 2251, cache_read_tokens: 3120, cache_creation_tokens: 640 },
          { iter: 3, input_tokens: 1000, output_tokens: 200, total_tokens: 1200, cache_read_tokens: 0 },
        ],
        updated_at: "2026-10-16T08:12:34Z",
      },
    });
    const { api } = await loadApiModule();

    const run: RunData = await api.getRun("run-with-cache");

    expect(run.llm_usage?.provider).toBe("deepseek");
    expect(run.llm_usage?.model).toBe("deepseek-v4-pro");
    expect(run.llm_usage?.totals.input_tokens).toBe(4821);
    expect(run.llm_usage?.totals.output_tokens).toBe(930);
    expect(run.llm_usage?.totals.total_tokens).toBe(5751);
    expect(run.llm_usage?.totals.calls).toBe(3);
    expect(run.llm_usage?.totals.cache_read_tokens).toBe(3120);
    expect(run.llm_usage?.totals.cache_creation_tokens).toBe(640);
    expect(run.llm_usage?.per_iteration).toHaveLength(3);
    expect(run.llm_usage?.per_iteration[1].cache_read_tokens).toBe(3120);
    expect(run.llm_usage?.updated_at).toBe("2026-10-16T08:12:34Z");
  });

  it("does not fabricate cache keys when the provider reported none", async () => {
    stubFetchWith({
      status: "success",
      run_id: "run-no-cache",
      elapsed_seconds: 4,
      llm_usage: {
        provider: "openai",
        model: "gpt-5.6",
        totals: { input_tokens: 100, output_tokens: 50, total_tokens: 150, calls: 1 },
        per_iteration: [
          { iter: 1, input_tokens: 100, output_tokens: 50, total_tokens: 150 },
        ],
        updated_at: "2026-10-16T08:00:00Z",
      },
    });
    const { api } = await loadApiModule();

    const run: RunData = await api.getRun("run-no-cache");

    expect(run.llm_usage?.totals.input_tokens).toBe(100);
    expect(run.llm_usage?.totals.cache_read_tokens).toBeUndefined();
    expect(run.llm_usage?.totals.cache_creation_tokens).toBeUndefined();
    expect(Object.keys(run.llm_usage!.totals)).not.toContain("cache_read_tokens");
    expect(Object.keys(run.llm_usage!.totals)).not.toContain("cache_creation_tokens");
    const firstCall = run.llm_usage!.per_iteration[0];
    expect(firstCall.cache_read_tokens).toBeUndefined();
    expect(Object.keys(firstCall)).not.toContain("cache_read_tokens");
    expect(Object.keys(firstCall)).not.toContain("cache_creation_tokens");
  });

  it("parses llm_usage: null without throwing", async () => {
    stubFetchWith({
      status: "success",
      run_id: "run-null-usage",
      elapsed_seconds: 1,
      llm_usage: null,
    });
    const { api } = await loadApiModule();

    const run: RunData = await api.getRun("run-null-usage");

    expect(run.llm_usage).toBeNull();
  });

  it("parses a response with no llm_usage key as undefined", async () => {
    stubFetchWith({
      status: "success",
      run_id: "run-absent-usage",
      elapsed_seconds: 1,
    });
    const { api } = await loadApiModule();

    const run: RunData = await api.getRun("run-absent-usage");

    expect(run.llm_usage).toBeUndefined();
  });
});
