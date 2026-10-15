import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunnerBudgetControls } from "../RunnerBudgetControls";
import { api, type LiveDecisionBudget } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, saveRunnerSettings: vi.fn().mockResolvedValue({}) } };
});

const budget: LiveDecisionBudget = {
  settings: { interval_seconds: 900, daily_at: null, timezone: "America/New_York", max_daily_turns: 26,
    max_daily_tokens: 1000000, max_daily_cost_usd: null, input_usd_per_million: null, output_usd_per_million: null },
  usage: { day: "2026-10-15", turns: 2, tokens: 1234, estimated_cost_usd: null, unaccounted_turns: 0 },
  projected_daily_cost_usd: null,
};

it("shows the spending warning and unknown cost before a start", () => {
  render(<RunnerBudgetControls broker="robinhood" budget={budget} running={false} onRefresh={vi.fn()} />);
  expect(screen.getByText(/even when no trades/)).toBeInTheDocument();
  expect(screen.getByText(/cannot be estimated/)).toBeInTheDocument();
  expect(screen.getByText(/Every 15 minutes/)).toBeInTheDocument();
});

it("saves the operator's cadence without discarding their other limits", async () => {
  const user = userEvent.setup();
  const refresh = vi.fn();
  render(<RunnerBudgetControls broker="robinhood" budget={budget} running={false} onRefresh={refresh} />);
  await user.click(screen.getByText("Model schedule and spending limits"));
  const input = screen.getByLabelText("Decision interval (seconds)");
  await user.clear(input);
  await user.type(input, "1800");
  await user.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(api.saveRunnerSettings).toHaveBeenCalledWith("robinhood", { ...budget.settings, interval_seconds: 1800 }));
  expect(refresh).toHaveBeenCalled();
});

it("requires stopping the runner before editing and displays measured projections", async () => {
  const user = userEvent.setup();
  render(<RunnerBudgetControls broker="robinhood" budget={{ ...budget, projected_daily_cost_usd: 4.2 }} running onRefresh={vi.fn()} />);
  expect(screen.getByText(/about \$4.20/)).toBeInTheDocument();
  await user.click(screen.getByText("Model schedule and spending limits"));
  expect(screen.getByLabelText("Decision interval (seconds)")).toBeDisabled();
  expect(screen.getByText(/Stop the runner before editing/)).toBeInTheDocument();
});
