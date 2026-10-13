import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ExtraEtfImport } from "../ExtraEtfImport";
import { RebalanceProposal } from "../RebalanceProposal";
import { api, type PortfolioSnapshot } from "@/lib/api";
import i18n from "@/i18n";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, importExtraEtf: vi.fn(), proposePortfolioRebalance: vi.fn() } };
});
beforeEach(async () => { vi.clearAllMocks(); await i18n.changeLanguage("en"); });

it("imports with the file's mtime and user-confirmed count and freshness", async () => {
  vi.mocked(api.importExtraEtf).mockResolvedValue({ status: "ok", import: { id: "extraetf-a" } });
  const reload = vi.fn().mockResolvedValue(undefined);
  render(<ExtraEtfImport onImported={reload} />);
  const file = new File(["original CSV"], "holdings.csv", { type: "text/csv", lastModified: 1791800000000 });
  Object.defineProperty(file, "text", { value: async () => "original CSV" });
  fireEvent.change(screen.getByLabelText("Investments CSV"), { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText("Account label"), { target: { value: "My export" } });
  fireEvent.change(screen.getByLabelText(/Expected holdings count/), { target: { value: "27" } });
  fireEvent.change(screen.getByLabelText(/Maximum file age/), { target: { value: "3" } });
  fireEvent.submit(screen.getByRole("button", { name: "Import file" }).closest("form")!);
  await waitFor(() => expect(reload).toHaveBeenCalledOnce());
  expect(api.importExtraEtf).toHaveBeenCalledWith({ csv_text: "original CSV", label: "My export", source_mtime: new Date(file.lastModified).toISOString(), expected_positions: 27, max_age_days: 3 });
  expect(screen.getByRole("status")).toHaveTextContent("then save and refresh");
});

it("pins a proposal to the shown snapshot and invalidates it when targets change", async () => {
  const snapshot = { snapshot_id: "shown", display_currency: "EUR", complete: true, combined_holdings: [{ canonical_id: "ISIN:IE00B4L5Y983", symbol: "IE00B4L5Y983", market_value_usd: 100 }] } as PortfolioSnapshot;
  vi.mocked(api.proposePortfolioRebalance).mockResolvedValue({ status: "ok", proposal: { mode: "proposal_only", snapshot_id: "shown", currency: "EUR", rows: [{ canonical_id: "ISIN:IE00B4L5Y983", current_value: 100, target_value: 60, change_value: -40 }] } });
  render(<RebalanceProposal snapshot={snapshot} />);
  fireEvent.click(screen.getByText("Allocation proposal"));
  fireEvent.change(screen.getByLabelText("ISIN:IE00B4L5Y983 target (%)"), { target: { value: "60" } });
  fireEvent.change(screen.getByLabelText("CASH target (%)"), { target: { value: "40" } });
  fireEvent.click(screen.getByRole("button", { name: "Calculate proposal" }));
  await screen.findByText("-€40.00");
  expect(api.proposePortfolioRebalance).toHaveBeenCalledWith("shown", { "ISIN:IE00B4L5Y983": 0.6, CASH: 0.4 });
  fireEvent.change(screen.getByLabelText("CASH target (%)"), { target: { value: "20" } });
  expect(screen.queryByText("-€40.00")).not.toBeInTheDocument();
});
