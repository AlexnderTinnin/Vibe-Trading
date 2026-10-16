import {
  applySwarmEvent,
  buildSwarmStatusFromToolResultPreview,
} from "../swarmStatus";
import type { SwarmRunStatus } from "@/types/agent";

function baseStatus(): SwarmRunStatus {
  return {
    runId: "run-usage",
    preset: "demo_team",
    status: "running",
    currentLayer: 0,
    totalLayers: 2,
    startedAt: 0,
    agents: [
      { agentId: "analyst", taskId: "t1", status: "running" },
      { agentId: "risk", taskId: "t2", status: "running" },
    ],
  };
}

function completedEvent(
  agentId: string,
  taskId: string,
  data: Record<string, unknown>,
) {
  return {
    type: "task_completed",
    agent_id: agentId,
    task_id: taskId,
    timestamp: "2026-07-29T12:01:00.000Z",
    data,
  };
}

describe("swarmStatus task token telemetry", () => {
  it("stores per-task tokens and derives the run totals on task_completed", () => {
    const next = applySwarmEvent(baseStatus(), completedEvent("analyst", "t1", {
      iterations: 4,
      input_tokens: 4200,
      output_tokens: 1300,
      cache_read_tokens: 900,
      cache_creation_tokens: 120,
      token_source: "provider",
    }));

    const agent = next.agents.find((a) => a.agentId === "analyst");
    expect(agent?.input_tokens).toBe(4200);
    expect(agent?.output_tokens).toBe(1300);
    expect(agent?.cache_read_tokens).toBe(900);
    expect(agent?.cache_creation_tokens).toBe(120);
    expect(next.total_input_tokens).toBe(4200);
    expect(next.total_output_tokens).toBe(1300);
    expect(next.token_source).toBe("provider");
  });

  it("does not double the run totals when the same event is replayed", () => {
    const event = completedEvent("analyst", "t1", {
      input_tokens: 4200,
      output_tokens: 1300,
      token_source: "provider",
    });
    const once = applySwarmEvent(baseStatus(), event);
    const twice = applySwarmEvent(once, event);

    expect(twice.total_input_tokens).toBe(4200);
    expect(twice.total_output_tokens).toBe(1300);
    expect(twice.token_source).toBe("provider");
  });

  it("sums totals across agents as each task completes", () => {
    let status = applySwarmEvent(baseStatus(), completedEvent("analyst", "t1", {
      input_tokens: 4200,
      output_tokens: 1300,
      token_source: "provider",
    }));
    status = applySwarmEvent(status, completedEvent("risk", "t2", {
      input_tokens: 800,
      output_tokens: 200,
      token_source: "estimated",
    }));

    expect(status.total_input_tokens).toBe(5000);
    expect(status.total_output_tokens).toBe(1500);
    expect(status.token_source).toBe("estimated");
  });

  it("keeps a real label when a replayed event carries a null token_source", () => {
    let status = applySwarmEvent(baseStatus(), completedEvent("analyst", "t1", {
      input_tokens: 4200,
      output_tokens: 1300,
      token_source: "provider",
    }));
    status = applySwarmEvent(status, completedEvent("risk", "t2", {
      input_tokens: 800,
      output_tokens: 200,
      token_source: null,
    }));

    expect(status.token_source).toBe("provider");
    expect(status.total_input_tokens).toBe(5000);
  });

  it("leaves run totals undefined — never 0 — when no event reports tokens", () => {
    const status = applySwarmEvent(baseStatus(), completedEvent("analyst", "t1", {
      iterations: 2,
      summary: "done",
    }));

    expect(status.total_input_tokens).toBeUndefined();
    expect(status.total_output_tokens).toBeUndefined();
    expect(status.token_source).toBeUndefined();
    expect(status.agents[0].status).toBe("done");
  });

  it("a tokenless worker_completed does not wipe existing task totals", () => {
    let status = applySwarmEvent(baseStatus(), completedEvent("analyst", "t1", {
      input_tokens: 4200,
      output_tokens: 1300,
      token_source: "provider",
    }));
    status = applySwarmEvent(status, {
      type: "worker_completed",
      agent_id: "analyst",
      task_id: "t1",
      timestamp: "2026-07-29T12:02:00.000Z",
      data: { iterations: 5 },
    });

    expect(status.total_input_tokens).toBe(4200);
    expect(status.total_output_tokens).toBe(1300);
    expect(status.token_source).toBe("provider");
  });
});

describe("swarmStatus tool-result preview token_usage", () => {
  function previewWith(tokenUsage: unknown): string {
    return JSON.stringify({
      status: "completed",
      run_id: "run-9",
      preset: "demo_team",
      final_report: "",
      tasks: [],
      token_usage: tokenUsage,
    });
  }

  it("parses run totals and token_source from the run_swarm result preview", () => {
    const status = buildSwarmStatusFromToolResultPreview(
      previewWith({
        total_input_tokens: 5000,
        total_output_tokens: 1500,
        token_source: "estimated",
      }),
    );

    expect(status?.runId).toBe("run-9");
    expect(status?.total_input_tokens).toBe(5000);
    expect(status?.total_output_tokens).toBe(1500);
    expect(status?.token_source).toBe("estimated");
  });

  it("treats a null token_source in the preview as unlabeled, not provider", () => {
    const status = buildSwarmStatusFromToolResultPreview(
      previewWith({ total_input_tokens: 10, total_output_tokens: 2, token_source: null }),
    );

    expect(status?.total_input_tokens).toBe(10);
    expect(status?.token_source).toBeUndefined();
  });

  it("keeps the pre-existing parse behaviour for results without token_usage", () => {
    const status = buildSwarmStatusFromToolResultPreview(
      previewWith(undefined),
    );

    expect(status?.runId).toBe("run-9");
    expect(status?.status).toBe("completed");
    expect(status?.total_input_tokens).toBeUndefined();
    expect(status?.total_output_tokens).toBeUndefined();
    expect(status?.token_source).toBeUndefined();
  });

  it("the truncated-JSON fallback path still recovers identity fields", () => {
    const truncated = '{"status": "completed", "run_id": "run-9", "preset": "demo_team", "token_u';
    const status = buildSwarmStatusFromToolResultPreview(truncated);

    expect(status?.runId).toBe("run-9");
    expect(status?.preset).toBe("demo_team");
    expect(status?.status).toBe("completed");
    expect(status?.total_input_tokens).toBeUndefined();
  });
});
