import { render, screen } from "@testing-library/react";
import { SwarmStatusCard } from "../SwarmStatusCard";
import type { SwarmRunStatus } from "@/types/agent";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, string | number>) => {
      if (key === "swarmStatus.tokens") {
        return `↑${String(options?.input ?? "")} ↓${String(options?.output ?? "")} tokens`;
      }
      if (key === "swarmStatus.estimated") return "estimated";
      if (key === "swarmStatus.tokenSourceUnlabeled") return "source unlabeled";
      return key;
    },
  }),
}));

function statusWith(overrides: Partial<SwarmRunStatus>): SwarmRunStatus {
  return {
    runId: "run-usage",
    preset: "demo_team",
    status: "completed",
    currentLayer: 0,
    totalLayers: 0,
    startedAt: 0,
    agents: [
      { agentId: "analyst", taskId: "t1", status: "done", input_tokens: 4200, output_tokens: 1300 },
    ],
    ...overrides,
  };
}

describe("SwarmStatusCard token usage line", () => {
  it("distinguishes a reported zero from an unreported direction", () => {
    const { container } = render(
      <SwarmStatusCard status={statusWith({
        total_input_tokens: 42,
        total_output_tokens: 0,
        token_source: "provider",
        agents: [
          { agentId: "analyst", taskId: "t1", status: "done", input_tokens: 42, output_tokens: 0 },
          { agentId: "risk", taskId: "t2", status: "failed", input_tokens: 12 },
        ],
      })} />,
    );
    expect(container.textContent).toContain("↑42 ↓0 tokens");
    const rows = container.querySelector('[role="rowgroup"]');
    expect(rows?.textContent).toContain("↑42 ↓0");
    expect(rows?.textContent).toContain("↑12 ↓—");
  });

  it("shows exact totals with a provider source and no caveat badge", () => {
    const { container } = render(
      <SwarmStatusCard
        status={statusWith({
          total_input_tokens: 4200,
          total_output_tokens: 1300,
          token_source: "provider",
        })}
      />,
    );

    expect(container.innerHTML).toContain("4200");
    expect(container.innerHTML).toContain("1300");
    expect(container.textContent).toContain("↑");
    expect(container.textContent).toContain("↓");
    expect(screen.queryByText("estimated")).toBeNull();
    expect(screen.queryByText("source unlabeled")).toBeNull();
  });

  it("shows a visible estimated badge when the source is estimated", () => {
    render(
      <SwarmStatusCard
        status={statusWith({
          total_input_tokens: 4200,
          total_output_tokens: 1300,
          token_source: "estimated",
        })}
      />,
    );

    expect(screen.getByText("estimated")).toBeInTheDocument();
    expect(screen.queryByText("source unlabeled")).toBeNull();
  });

  it("shows the honest unlabeled badge — not provider, not estimated — when the source is missing", () => {
    render(
      <SwarmStatusCard
        status={statusWith({
          total_input_tokens: 4200,
          total_output_tokens: 1300,
        })}
      />,
    );

    expect(screen.getByText("source unlabeled")).toBeInTheDocument();
    expect(screen.queryByText("estimated")).toBeNull();
  });

  it("treats an explicit null source as unlabeled", () => {
    render(
      <SwarmStatusCard
        status={statusWith({
          total_input_tokens: 4200,
          total_output_tokens: 1300,
          token_source: null,
        })}
      />,
    );

    expect(screen.getByText("source unlabeled")).toBeInTheDocument();
  });

  it("renders no token line and no badge for zero totals", () => {
    const { container } = render(
      <SwarmStatusCard
        status={statusWith({
          agents: [{ agentId: "analyst", taskId: "t1", status: "done" }],
          total_input_tokens: 0,
          total_output_tokens: 0,
        })}
      />,
    );

    expect(container.textContent).not.toContain("↑");
    expect(screen.queryByText("source unlabeled")).toBeNull();
  });

  it("formats the run line through abbreviateNum", () => {
    const { container } = render(
      <SwarmStatusCard
        status={statusWith({
          total_input_tokens: 15000,
          total_output_tokens: 1300,
          token_source: "provider",
        })}
      />,
    );

    expect(container.textContent).toContain("↑15K");
    expect(container.textContent).toContain("↓1,300");
  });

  it("shows per-agent tokens inside the dashboard rows", () => {
    const { container } = render(<SwarmStatusCard status={statusWith({})} />);

    const rowgroup = container.querySelector('[role="rowgroup"]');
    expect(rowgroup?.textContent).toContain("↑4,200 ↓1,300");
  });
});
