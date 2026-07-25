import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OKXPaperSettings } from "../OKXPaperSettings";

const apiMock = vi.hoisted(() => ({
  getOkxPaperConfig: vi.fn(),
  updateOkxPaperConfig: vi.fn(),
  checkOkxPaperConnection: vi.fn(),
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
    profile: "paper",
    environment: "demo" as const,
    is_demo: true,
    configured: true,
    credentials_configured: true,
    api_key_configured: true,
    api_secret_configured: true,
    passphrase_configured: true,
    expected_uid: "demo-uid",
    allowed_swap_instruments: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    max_swap_leverage: 8,
    proxy_configured: false,
    sdk_installed: true,
    runner_running: false,
    missing_fields: [],
    ...overrides,
  };
}

describe("OKX Demo settings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getOkxPaperConfig.mockResolvedValue(configStatus());
    apiMock.updateOkxPaperConfig.mockResolvedValue(configStatus());
    apiMock.checkOkxPaperConnection.mockResolvedValue({
      broker: "okx",
      ok: true,
      status: "ok",
      message: "OKX Demo connection and UID verified",
      is_demo: true,
      uid_verified: true,
      total_equity: "1234.56",
      checked_at: "2026-07-25T00:00:00Z",
    });
  });

  it("loads configured state without putting saved secrets in the inputs", async () => {
    render(<OKXPaperSettings />);

    expect(await screen.findByText("OKX Demo Trading")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Configured")).toBeInTheDocument());
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("Secret")).toHaveValue("");
    expect(screen.getByLabelText("Passphrase")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute(
      "placeholder",
      "Configured",
    );
    expect(screen.getByLabelText("API Key")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Replace credentials" })).toBeEnabled();
    expect(screen.getByDisplayValue("demo-uid")).toBeInTheDocument();
  });

  it("saves all three credentials and the constrained swap scope", async () => {
    apiMock.getOkxPaperConfig.mockResolvedValue(configStatus({
      configured: false,
      credentials_configured: false,
      api_key_configured: false,
      api_secret_configured: false,
      passphrase_configured: false,
      expected_uid: "",
      max_swap_leverage: 10,
      missing_fields: ["api_key", "api_secret", "passphrase", "expected_uid"],
    }));
    render(<OKXPaperSettings />);

    await screen.findByText("Not configured");
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "new-demo-key" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "new-demo-secret" } });
    fireEvent.change(screen.getByLabelText("Passphrase"), { target: { value: "new-demo-pass" } });
    fireEvent.change(screen.getByLabelText("OKX Demo UID"), { target: { value: "new-demo-uid" } });
    fireEvent.click(screen.getByLabelText("ETH-USDT-SWAP"));
    fireEvent.change(screen.getByRole("slider", { name: "Maximum leverage" }), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save OKX Demo settings" }));

    await waitFor(() => expect(apiMock.updateOkxPaperConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxPaperConfig).toHaveBeenCalledWith({
      api_key: "new-demo-key",
      api_secret: "new-demo-secret",
      passphrase: "new-demo-pass",
      expected_uid: "new-demo-uid",
      allowed_swap_instruments: ["BTC-USDT-SWAP"],
      max_swap_leverage: 5,
      clear_proxy: false,
    });
  });

  it("keeps saved credentials when all three secret fields stay blank", async () => {
    render(<OKXPaperSettings />);

    await screen.findByText("Configured");
    fireEvent.change(screen.getByLabelText("OKX Demo UID"), { target: { value: "updated-uid" } });
    fireEvent.click(screen.getByRole("button", { name: "Save OKX Demo settings" }));

    await waitFor(() => expect(apiMock.updateOkxPaperConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxPaperConfig.mock.calls[0][0]).not.toHaveProperty("api_key");
    expect(apiMock.updateOkxPaperConfig.mock.calls[0][0]).not.toHaveProperty("api_secret");
    expect(apiMock.updateOkxPaperConfig.mock.calls[0][0]).not.toHaveProperty("passphrase");
  });

  it("rejects a partial credential replacement before calling the API", async () => {
    render(<OKXPaperSettings />);

    await screen.findByText("Configured");
    fireEvent.click(screen.getByRole("button", { name: "Replace credentials" }));
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "only-one-field" } });
    fireEvent.click(screen.getByRole("button", { name: "Save OKX Demo settings" }));

    expect(screen.getByLabelText("Secret")).toBeRequired();
    expect(screen.getByLabelText("Passphrase")).toBeRequired();
    expect(apiMock.updateOkxPaperConfig).not.toHaveBeenCalled();
  });

  it("replaces the saved credentials only after all three new values are entered", async () => {
    render(<OKXPaperSettings />);

    await screen.findByText("Configured");
    fireEvent.click(screen.getByRole("button", { name: "Replace credentials" }));
    expect(screen.getByLabelText("API Key")).toBeEnabled();
    expect(screen.getByLabelText("API Key")).toBeRequired();
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "replacement-key" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "replacement-secret" } });
    fireEvent.change(screen.getByLabelText("Passphrase"), { target: { value: "replacement-pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Save OKX Demo settings" }));

    await waitFor(() => expect(apiMock.updateOkxPaperConfig).toHaveBeenCalledTimes(1));
    expect(apiMock.updateOkxPaperConfig).toHaveBeenCalledWith(expect.objectContaining({
      api_key: "replacement-key",
      api_secret: "replacement-secret",
      passphrase: "replacement-pass",
    }));
  });

  it("tests only the saved configuration and displays the verified equity", async () => {
    render(<OKXPaperSettings />);

    const button = await screen.findByRole("button", { name: "Test connection" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);

    expect(await screen.findByText("OKX Demo connection and UID verified")).toBeInTheDocument();
    expect(screen.getByText("Total equity: 1234.56")).toBeInTheDocument();
    expect(apiMock.checkOkxPaperConnection).toHaveBeenCalledTimes(1);
  });

  it("requires fresh Demo credentials when a live profile is detected", async () => {
    apiMock.getOkxPaperConfig.mockResolvedValue(configStatus({
      profile: "live",
      environment: "live",
      is_demo: false,
      configured: false,
      missing_fields: ["demo_profile"],
    }));
    render(<OKXPaperSettings />);

    expect(await screen.findByText("Live profile detected")).toBeInTheDocument();
    expect(screen.getByLabelText("API Key")).toBeRequired();
    expect(screen.getByLabelText("Secret")).toBeRequired();
    expect(screen.getByLabelText("Passphrase")).toBeRequired();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
  });
});
