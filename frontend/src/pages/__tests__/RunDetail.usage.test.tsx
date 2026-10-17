import { fireEvent, render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { RunDetail } from "../RunDetail";

const apiMock = vi.hoisted(() => ({
  getRun: vi.fn(),
  getRunCode: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/components/charts/CandlestickChart", () => ({
  CandlestickChart: () => <div data-testid="candlestick-chart" />,
}));
vi.mock("@/components/charts/EquityChart", () => ({
  EquityChart: () => <div data-testid="equity-chart" />,
}));

function renderRunDetail(path = "/runs/usage-run") {
  const router = createMemoryRouter(
    [{ path: "/runs/:runId", element: <RunDetail /> }],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const perIterationNoCache = [
  { iter: 0, input_tokens: 500, output_tokens: 900, total_tokens: 1400 },
  { iter: 1, input_tokens: 414, output_tokens: 2278, total_tokens: 2692 },
  { iter: 2, input_tokens: 320, output_tokens: 2500, total_tokens: 2820 },
];

const usageNoCache = {
  provider: "openai",
  model: "gpt-5.6-terra",
  totals: { input_tokens: 1234, output_tokens: 5678, total_tokens: 6912, calls: 3 },
  per_iteration: perIterationNoCache,
  updated_at: "2026-10-16T00:00:00Z",
};

const usageWithCache = {
  provider: "anthropic",
  model: "claude-sonnet-5",
  totals: {
    input_tokens: 1234,
    output_tokens: 5678,
    total_tokens: 6912,
    calls: 3,
    cache_read_tokens: 300,
    cache_creation_tokens: 40,
  },
  per_iteration: [
    { ...perIterationNoCache[0], cache_read_tokens: 100, cache_creation_tokens: 40 },
    perIterationNoCache[1],
    { ...perIterationNoCache[2], cache_read_tokens: 200 },
  ],
  updated_at: "2026-10-16T00:00:00Z",
};

async function openUsageTab(prompt = "Usage run") {
  await screen.findByText(prompt);
  fireEvent.click(screen.getByRole("tab", { name: "Token Usage" }));
}

describe("RunDetail token-usage tab", () => {
  beforeEach(() => {
    apiMock.getRun.mockReset();
    apiMock.getRunCode.mockReset();
    apiMock.getRunCode.mockResolvedValue({});
  });

  it("shows the reported totals without inventing cache rows", async () => {
    apiMock.getRun.mockResolvedValue({
      status: "success",
      run_id: "usage-run",
      prompt: "Usage run",
      llm_usage: usageNoCache,
    });

    renderRunDetail();
    await openUsageTab();

    const summary = await screen.findByTestId("llm-usage-summary");
    expect(within(summary).getByText("1234")).toBeInTheDocument();
    expect(within(summary).getByText("5678")).toBeInTheDocument();
    expect(within(summary).getByText("6912")).toBeInTheDocument();
    expect(within(summary).getByText("3")).toBeInTheDocument();
    expect(within(summary).getByText("openai")).toBeInTheDocument();
    expect(within(summary).getByText("gpt-5.6-terra")).toBeInTheDocument();
    expect(within(summary).queryByText("Cache read tokens")).not.toBeInTheDocument();
    expect(within(summary).queryByText("Cache creation tokens")).not.toBeInTheDocument();

    const iterations = screen.getByTestId("llm-usage-iterations");
    expect(within(iterations).queryByText("Cache read tokens")).not.toBeInTheDocument();
    expect(within(iterations).getByText("414")).toBeInTheDocument();
  });

  it("renders cache totals and per-iteration cache columns when the provider reported them", async () => {
    apiMock.getRun.mockResolvedValue({
      status: "success",
      run_id: "usage-run",
      prompt: "Cached usage run",
      llm_usage: usageWithCache,
    });

    renderRunDetail();
    await openUsageTab("Cached usage run");

    const summary = await screen.findByTestId("llm-usage-summary");
    expect(within(summary).getByText("Cache read tokens")).toBeInTheDocument();
    expect(within(summary).getByText("300")).toBeInTheDocument();
    expect(within(summary).getByText("Cache creation tokens")).toBeInTheDocument();
    expect(within(summary).getByText("40")).toBeInTheDocument();

    const iterations = screen.getByTestId("llm-usage-iterations");
    const rows = within(iterations).getAllByRole("row");
    expect(within(rows[1]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "0", "500", "900", "1400", "100", "40",
    ]);
    // an iteration that never reported cache gets the not-reported marker,
    // never a fabricated 0
    expect(within(rows[2]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "1", "414", "2278", "2692", "—", "—",
    ]);
    expect(within(rows[3]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "2", "320", "2500", "2820", "200", "—",
    ]);
  });

  it.each([
    { label: "null", llm_usage: null },
    { label: "absent", llm_usage: undefined },
  ])("hides the Token Usage tab when llm_usage is $label", async ({ llm_usage }) => {
    apiMock.getRun.mockResolvedValue({
      status: "success",
      run_id: "usage-run",
      prompt: "Silent run",
      llm_usage,
    });

    renderRunDetail();
    await screen.findByText("Silent run");

    expect(screen.queryByRole("tab", { name: "Token Usage" })).not.toBeInTheDocument();
  });
});
