import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "@/i18n";
import type { MandateProposal } from "@/lib/api";
import { MandateProposalCard } from "../MandateProposalCard";

const apiMock = vi.hoisted(() => ({
  commitMandate: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
}));

vi.mock("../AgentAvatar", () => ({
  AgentAvatar: () => <div data-testid="agent-avatar" />,
}));

function makeProposal(instruments: string[] = ["perpetual_swap"]): MandateProposal {
  return {
    type: "mandate.proposal",
    proposal_id: `mp_${"1".repeat(32)}`,
    session_id: "session-1",
    account: {
      broker: "okx",
      type: "margin",
      funded_by: "user",
    },
    profiles: [
      {
        ordinal: 1,
        label: "Balanced",
        universe: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        max_order_usd: 100,
        daily_trade_cap: 3,
        leverage: 3,
        instruments,
      },
    ],
  };
}

describe("MandateProposalCard", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    apiMock.commitMandate.mockReset();
    apiMock.commitMandate.mockResolvedValue({ mandate_id: "mandate-1" });
  });

  it("requires and submits a trimmed account reference for perpetual mandates", async () => {
    const user = userEvent.setup();
    render(
      <MandateProposalCard
        proposal={makeProposal()}
        committed={null}
        onAdjust={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: 'Commit "Balanced"' }));

    const confirm = screen.getByRole("button", { name: "Confirm & commit" });
    expect(confirm).toBeDisabled();
    expect(apiMock.commitMandate).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Dedicated account reference"), "  demo-uid-001  ");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => {
      expect(apiMock.commitMandate).toHaveBeenCalledWith({
        broker: "okx",
        proposal_id: `mp_${"1".repeat(32)}`,
        selected_ordinal: 1,
        adjustments: null,
        consent_ack: true,
        session_id: "session-1",
        account_ref: "demo-uid-001",
      });
    });
  });

  it("does not require an account reference for non-perpetual profiles", async () => {
    const user = userEvent.setup();
    render(
      <MandateProposalCard
        proposal={makeProposal(["equity"])}
        committed={null}
        onAdjust={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: 'Commit "Balanced"' }));

    expect(screen.queryByLabelText("Dedicated account reference")).not.toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Confirm & commit" });
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(apiMock.commitMandate).toHaveBeenCalledTimes(1));
    expect(apiMock.commitMandate.mock.calls[0][0].account_ref).toBeUndefined();
  });
});
