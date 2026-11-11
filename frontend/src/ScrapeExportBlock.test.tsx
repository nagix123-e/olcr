import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { ScrapeExportBlock } from "./ScrapeExportBlock";

afterEach(cleanup);

describe("ScrapeExportBlock", () => {
  it("lets the user choose CSV and writes the selected format as a local file", async () => {
    const download = vi.fn().mockResolvedValue(undefined);
    render(<ScrapeExportBlock block={{ type: "scrape_export", run_id: "run/1", output_format: "jsonl", record_count: 12 }} download={download} />);

    expect(screen.getByRole("combobox", { name: "Final output format" })).toHaveValue("jsonl");
    fireEvent.change(screen.getByRole("combobox", { name: "Final output format" }), { target: { value: "csv" } });
    fireEvent.click(screen.getByRole("button", { name: "Save CSV to this computer" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith(
      "/web/scrape/runs/run%2F1/export?format=csv", "olcr-scrape-results.csv"));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved CSV file to this computer");
  });

  it("shows authenticated download failures in the conversation", async () => {
    const download = vi.fn().mockRejectedValue(new Error("HTTP_403: GUI_SESSION_REQUIRED"));
    render(<ScrapeExportBlock block={{ type: "scrape_export", run_id: "run-1", output_format: "csv", record_count: 1 }} download={download} />);

    fireEvent.click(screen.getByRole("button", { name: "Save CSV to this computer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("GUI_SESSION_REQUIRED");
  });
});
