import { act, render, renderHook, screen } from "@testing-library/react";
import { LlmUsageCounter } from "../LlmUsageCounter";
import { useLlmUsageCounter } from "@/hooks/useLlmUsageCounter";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => (key === "agent.tokenUsage.live" ? "tokens" : key),
  }),
}));

describe("LlmUsageCounter", () => {
  it("shows the accumulated 300 / 130 totals fed by the hook", () => {
    const { result } = renderHook(() => useLlmUsageCounter());
    act(() => result.current.record("A", 1, { input_tokens: 100, output_tokens: 50, total_tokens: 150 }));
    act(() => result.current.record("A", 2, { input_tokens: 200, output_tokens: 80, total_tokens: 280 }));

    const { container } = render(
      <LlmUsageCounter input={result.current.input} output={result.current.output} />,
    );

    expect(container.textContent).toBe("↑300 ↓130 tokens");
    expect(screen.getByText(/300/)).toBeInTheDocument();
    expect(container.querySelector("p")).toHaveClass("text-xs");
    expect(container.querySelector("p")).toHaveClass("text-muted-foreground");
  });

  it("renders nothing before any usage event arrives", () => {
    const { container } = render(<LlmUsageCounter input={0} output={0} />);
    expect(container.innerHTML).toBe("");
  });

  it("formats large totals through abbreviateNum", () => {
    const { container } = render(<LlmUsageCounter input={15000} output={1300} />);
    expect(container.textContent).toBe("↑15K ↓1,300 tokens");
  });
});
