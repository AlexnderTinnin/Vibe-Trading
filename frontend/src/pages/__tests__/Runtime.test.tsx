import { act, fireEvent, render, screen } from "@testing-library/react";
import { Runtime } from "../Runtime";
import type { LiveStatus, OkxQuantStatus } from "@/lib/api";

const apiMock = vi.hoisted(() => ({
  getLiveStatus: vi.fn(),
  getOkxQuantStatus: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
}));

function makeStatus(overrides: Partial<LiveStatus> = {}): LiveStatus {
  return {
    global_halted: false,
    brokers: [
      {
        auth: {
          broker: "paper",
          oauth_token_present: true,
          is_live_broker: true,
        },
        runner: {
          broker: "paper",
          alive: true,
          last_tick: null,
          last_tick_age_seconds: 5,
        },
        mandate: {
          broker: "paper",
          account_ref: "acct-1",
          created_at: "2026-06-12T00:00:00Z",
          expires_at: "2999-01-01T00:00:00Z",
          expires_in_seconds: 3600,
          expired: false,
          limits: {
            max_order_notional_usd: 750,
            max_total_exposure_usd: 2000,
            max_leverage: 1,
            max_trades_per_day: 4,
            allowed_instruments: ["equity"],
            account_funding_usd: 10000,
          },
        },
        halted: false,
      },
      {
        auth: {
          broker: "sandbox",
          oauth_token_present: false,
          is_live_broker: true,
        },
        runner: {
          broker: "sandbox",
          alive: false,
          last_tick: null,
          last_tick_age_seconds: null,
        },
        mandate: null,
        halted: false,
      },
    ],
    ...overrides,
  };
}

function makeQuantStatus(overrides: Partial<OkxQuantStatus> = {}): OkxQuantStatus {
  return {
    broker: "okx",
    environment: "demo",
    runner: {
      running: false,
      mode: "shadow",
      tick_interval_seconds: 60,
      latest: { outcome: "completed", tick_at: new Date().toISOString() },
    },
    breaker: { tripped: false },
    promotion: {
      broker: "okx",
      qualified: false,
      manually_approved: false,
      execution_enabled: false,
      reasons: ["requires 30 elapsed Demo days"],
      qualification: {
        broker: "okx",
        eligible: false,
        elapsed_days: 4.5,
        active_days: 5,
        tick_count: 240,
        passed_drills: ["network_timeout"],
        missing_drills: ["process_restart"],
      },
      approval: null,
    },
    live_swap_writes_compiled: false,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("Runtime page", () => {
  beforeEach(() => {
    apiMock.getLiveStatus.mockReset();
    apiMock.getOkxQuantStatus.mockReset();
    apiMock.getOkxQuantStatus.mockResolvedValue(makeQuantStatus());
  });

  it("renders broker auth, runner, mandate, and risk state from live status", async () => {
    apiMock.getLiveStatus.mockResolvedValue(makeStatus());

    render(<Runtime />);

    expect(await screen.findByText("Live / Paper Runtime Status")).toBeInTheDocument();
    expect(screen.getByText("Clear")).toBeInTheDocument();
    expect(screen.getByText("paper")).toBeInTheDocument();
    expect(screen.getByText("auth present")).toBeInTheDocument();
    expect(screen.getByText("runner alive")).toBeInTheDocument();
    expect(screen.getByText("runtime active")).toBeInTheDocument();
    expect(screen.getByText("acct-1")).toBeInTheDocument();
    expect(screen.getByText(/\$750\/order/)).toBeInTheDocument();
    expect(screen.getByText("sandbox")).toBeInTheDocument();
    expect(screen.getByText("auth missing")).toBeInTheDocument();
    expect(screen.getByText("dormant")).toBeInTheDocument();
    expect(screen.getByText("OKX BTC / ETH Perpetual")).toBeInTheDocument();
    expect(screen.getByText("Live disabled")).toBeInTheDocument();
    expect(screen.getByText("4.5 / 30")).toBeInTheDocument();
  });

  it("fails closed when live status is unavailable", async () => {
    apiMock.getLiveStatus.mockRejectedValue(new Error("backend offline"));

    render(<Runtime />);

    expect(await screen.findByText("Runtime status unavailable")).toBeInTheDocument();
    expect(screen.getByText("backend offline")).toBeInTheDocument();
    expect(screen.getByText(/Treat connector runtime as unavailable/)).toBeInTheDocument();
  });

  it("refreshes by reading live status again", async () => {
    apiMock.getLiveStatus.mockResolvedValue(makeStatus());

    render(<Runtime />);
    await screen.findByText("paper");

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(apiMock.getLiveStatus).toHaveBeenCalledTimes(2);
    expect(apiMock.getOkxQuantStatus).toHaveBeenCalledTimes(2);
  });

  it("keeps the newest live status when an older request resolves later", async () => {
    const first = deferred<LiveStatus>();
    const second = deferred<LiveStatus>();
    apiMock.getLiveStatus
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(<Runtime />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await act(async () => {
      second.resolve(makeStatus({ global_halted: true, brokers: [] }));
      await second.promise;
    });
    expect(await screen.findByText("Halted")).toBeInTheDocument();

    await act(async () => {
      first.resolve(makeStatus());
      await first.promise;
    });

    expect(screen.getByText("Halted")).toBeInTheDocument();
    expect(screen.queryByText("paper")).not.toBeInTheDocument();
  });

  it("aborts an in-flight status request on unmount", () => {
    const pending = deferred<LiveStatus>();
    apiMock.getLiveStatus.mockReturnValue(pending.promise);

    const { unmount } = render(<Runtime />);
    const signal = apiMock.getLiveStatus.mock.calls[0][0] as AbortSignal;
    const quantSignal = apiMock.getOkxQuantStatus.mock.calls[0][0] as AbortSignal;

    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal.aborted).toBe(false);
    expect(quantSignal).toBe(signal);

    unmount();

    expect(signal.aborted).toBe(true);
  });

  it("keeps broker status visible when the OKX quant snapshot is unavailable", async () => {
    apiMock.getLiveStatus.mockResolvedValue(makeStatus());
    apiMock.getOkxQuantStatus.mockRejectedValue(new Error("quant endpoint offline"));

    render(<Runtime />);

    expect(await screen.findByText("paper")).toBeInTheDocument();
    expect(screen.getByText("quant endpoint offline")).toBeInTheDocument();
    expect(screen.queryByText("Runtime status unavailable")).not.toBeInTheDocument();
  });

  it("renders sub-minute mandate expiry as seconds", async () => {
    const baseStatus = makeStatus();
    const expiresAt = new Date(Date.now() + 45_000).toISOString();
    apiMock.getLiveStatus.mockResolvedValue(makeStatus({
      brokers: [
        {
          ...baseStatus.brokers[0],
          mandate: {
            ...baseStatus.brokers[0].mandate!,
            expires_at: expiresAt,
          },
        },
      ],
    }));

    render(<Runtime />);

    expect(await screen.findByText("45s")).toBeInTheDocument();
  });
});
