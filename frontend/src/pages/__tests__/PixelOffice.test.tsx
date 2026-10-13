import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PixelOffice } from "../PixelOffice";
import { api, type OfficeSnapshot } from "@/lib/api";
import i18n from "@/i18n";

vi.mock("@/lib/api", () => ({ api: { getPixelOffice: vi.fn(), updatePixelOffice: vi.fn() } }));
const initial: OfficeSnapshot = { mode: "manual_planning", agents: [{ id: "research", name: "Research Analyst", role: "research", rank: "Operator", color: "#c59259", load: 0 }], tasks: [] };
beforeEach(async () => {
  vi.resetAllMocks();
  await i18n.changeLanguage("en");
  vi.mocked(api.getPixelOffice).mockResolvedValue(initial);
});
describe("shared Pixel Office", () => {
  it("uses persisted tasks, creates on the API and preserves failed completion input", async () => {
    localStorage.setItem("vibe-pixel-office-tasks", JSON.stringify([{ title: "old local-only task" }]));
    render(<PixelOffice />);
    await screen.findByText("No tasks yet.");
    expect(screen.queryByText("old local-only task")).not.toBeInTheDocument();
    const created: OfficeSnapshot = { ...initial, tasks: [{ id: "a", title: "Read filing", description: "Observed sources only", assigned_to: "research", status: "assigned", priority: "normal", result: "" }] };
    vi.mocked(api.updatePixelOffice).mockResolvedValueOnce(created);
    fireEvent.change(screen.getByLabelText("Task title"), { target: { value: "Read filing" } });
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "Observed sources only" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    await screen.findByText("Read filing");
    expect(api.updatePixelOffice).toHaveBeenCalledWith("create", expect.objectContaining({ title: "Read filing", assigned_to: "research" }));
    expect(screen.getByRole("button", { name: "Record completion" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Completion result"), { target: { value: "Reviewed the reported cash flows" } });
    vi.mocked(api.updatePixelOffice).mockRejectedValueOnce(new Error("Write failed"));
    fireEvent.click(screen.getByRole("button", { name: "Record completion" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Completion result")).toHaveValue("Reviewed the reported cash flows");
    expect(screen.queryByText("Completed")).not.toBeInTheDocument();
    vi.mocked(api.updatePixelOffice).mockResolvedValueOnce({ ...created, tasks: [{ ...created.tasks[0], status: "completed", result: "Reviewed the reported cash flows" }] });
    fireEvent.click(screen.getByRole("button", { name: "Record completion" }));
    await waitFor(() => expect(screen.queryByLabelText("Completion result")).not.toBeInTheDocument());
    expect(screen.getByText("Reviewed the reported cash flows")).toBeInTheDocument();
  });

  it("does not invent an office snapshot on read failure and supports retry", async () => {
    vi.mocked(api.getPixelOffice).mockRejectedValueOnce(new Error("Unavailable"));
    render(<PixelOffice />);
    await screen.findByRole("alert");
    expect(screen.queryByText("Research Analyst")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("No tasks yet.");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
