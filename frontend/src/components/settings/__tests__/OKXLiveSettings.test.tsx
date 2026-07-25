import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OKXLiveSettings } from "../OKXLiveSettings";

const apiMock = vi.hoisted(() => ({
  getOkxLiveConfig: vi.fn(),
  updateOkxLiveConfig: vi.fn(),
  checkOkxLiveConnection: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: apiMock };
});

vi.mock("sonner", () => ({ toast: toastMock }));

function configStatus(overrides = {}) {
  return {
    broker: "okx" as const,
    profile: "live-readonly" as const,
    environment: "live" as const,
    is_demo: false as const,
    configured: true,
    credentials_configured: true,
    api_key_configured: true,
    api_secret_configured: true,
    passphrase_configured: true,
    expected_uid: "live-uid",
    proxy_configured: false,
    sdk_installed: true,
    live_swap_writes_compiled: false,
    missing_fields: [],
    ...overrides,
  };
}

describe("OKX live settings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getOkxLiveConfig.mockResolvedValue(configStatus());
    apiMock.updateOkxLiveConfig.mockResolvedValue(configStatus());
    apiMock.checkOkxLiveConnection.mockResolvedValue({
      broker: "okx",
      ok: true,
      status: "ok",
      message: "OKX live connection and UID verified",
      is_demo: false,
      uid_verified: true,
      total_equity: "9876.54",
      checked_at: "2026-07-25T00:00:00Z",
    });
  });

  it("loads configured live credentials without returning their values", async () => {
    render(<OKXLiveSettings />);

    expect(await screen.findByText("OKX Live Account")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Configured")).toBeInTheDocument());
    expect(screen.getByLabelText("Live API Key")).toHaveValue("");
    expect(screen.getByLabelText("Live API Key")).toBeDisabled();
    expect(screen.getByLabelText("Live Secret")).toBeDisabled();
    expect(screen.getByLabelText("Live Passphrase")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Replace credentials" })).toBeEnabled();
    expect(screen.getByText("Build locked")).toBeInTheDocument();
  });

  it("saves an initial live read-only credential set", async () => {
    apiMock.getOkxLiveConfig.mockResolvedValue(configStatus({
      configured: false,
      credentials_configured: false,
      api_key_configured: false,
      api_secret_configured: false,
      passphrase_configured: false,
      expected_uid: "",
      missing_fields: ["api_key", "api_secret", "passphrase", "expected_uid"],
    }));
    render(<OKXLiveSettings />);

    await screen.findByText("Not configured");
    fireEvent.change(screen.getByLabelText("Live API Key"), { target: { value: "new-live-key" } });
    fireEvent.change(screen.getByLabelText("Live Secret"), { target: { value: "new-live-secret" } });
    fireEvent.change(screen.getByLabelText("Live Passphrase"), { target: { value: "new-live-pass" } });
    fireEvent.change(screen.getByLabelText("OKX Live UID"), { target: { value: "new-live-uid" } });
    fireEvent.click(screen.getByRole("button", { name: "Save live settings" }));

    await waitFor(() => expect(apiMock.updateOkxLiveConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxLiveConfig).toHaveBeenCalledWith({
      api_key: "new-live-key",
      api_secret: "new-live-secret",
      passphrase: "new-live-pass",
      expected_uid: "new-live-uid",
      clear_proxy: false,
    });
  });

  it("replaces all three live credentials as one set", async () => {
    render(<OKXLiveSettings />);

    await screen.findByText("Configured");
    fireEvent.click(screen.getByRole("button", { name: "Replace credentials" }));
    fireEvent.change(screen.getByLabelText("Live API Key"), { target: { value: "replacement-key" } });
    fireEvent.change(screen.getByLabelText("Live Secret"), { target: { value: "replacement-secret" } });
    fireEvent.change(screen.getByLabelText("Live Passphrase"), { target: { value: "replacement-pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Save live settings" }));

    await waitFor(() => expect(apiMock.updateOkxLiveConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxLiveConfig).toHaveBeenCalledWith(expect.objectContaining({
      api_key: "replacement-key",
      api_secret: "replacement-secret",
      passphrase: "replacement-pass",
    }));
  });

  it("retains saved credentials when only the live UID changes", async () => {
    render(<OKXLiveSettings />);

    await screen.findByText("Configured");
    fireEvent.change(screen.getByLabelText("OKX Live UID"), { target: { value: "updated-live-uid" } });
    fireEvent.click(screen.getByRole("button", { name: "Save live settings" }));

    await waitFor(() => expect(apiMock.updateOkxLiveConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxLiveConfig.mock.calls[0][0]).not.toHaveProperty("api_key");
    expect(apiMock.updateOkxLiveConfig.mock.calls[0][0]).not.toHaveProperty("api_secret");
    expect(apiMock.updateOkxLiveConfig.mock.calls[0][0]).not.toHaveProperty("passphrase");
  });

  it("tests only the saved live configuration", async () => {
    render(<OKXLiveSettings />);

    const button = await screen.findByRole("button", { name: "Test live connection" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);

    expect(await screen.findByText("OKX live connection and UID verified")).toBeInTheDocument();
    expect(screen.getByText("Total equity: 9876.54")).toBeInTheDocument();
    expect(apiMock.checkOkxLiveConnection).toHaveBeenCalledTimes(1);
  });
});
