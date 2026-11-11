import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ScrapeDataTable } from "./ScrapeDataTable";
afterEach(cleanup);
const page = { columns: ["title", "price"], rows: [{ id: 7, values: { title: "Alpha", price: "10" } }], total: 1 };
it("edits completed data and uses the chosen export format", async () => {
  const api = vi.fn(async (_path: string, init?: RequestInit) => init?.method === "POST" ? { updated: true } : page);
  const download = vi.fn().mockResolvedValue(undefined);
  render(<ScrapeDataTable api={api} runId="run" status="COMPLETED" recordCount={1} download={download} />);
  const input = await screen.findByLabelText("Row 7 title");
  fireEvent.change(input, { target: { value: "Edited" } }); fireEvent.blur(input);
  await waitFor(() => expect(api).toHaveBeenCalledWith("/web/scrape/runs/run/table", { method: "POST", body: JSON.stringify({operation:"cell",column:"title",target:"",row_id:7,value:"Edited"}) }));
  fireEvent.click(screen.getByRole("button", {name:"Export XLSX"}));
  expect(download).toHaveBeenCalledWith("/web/scrape/runs/run/export?format=xlsx", "olcr-results.xlsx");
});
it("keeps running and paused collection read-only", async () => {
  const api = vi.fn(async () => page);
  const view = render(<ScrapeDataTable api={api} runId="run" status="RUNNING" recordCount={1} />);
  expect(await screen.findByText("Alpha")).toBeInTheDocument();
  expect(screen.queryByLabelText("Row 7 title")).toBeNull();
  expect(screen.getByRole("button", { name: "Remove duplicate rows" })).toBeDisabled();
  view.rerender(<ScrapeDataTable api={api} runId="run" status="PAUSED" recordCount={1} />);
  expect(screen.queryByLabelText("Row 7 title")).toBeNull();
});
it("searches the full run and resets pagination", async () => {
  const api = vi.fn(async () => ({...page,total:80}));
  render(<ScrapeDataTable api={api} runId="run" status="COMPLETED" recordCount={80} />);
  await screen.findByLabelText("Row 7 title");
  fireEvent.click(screen.getByRole("button", {name:"Next rows"}));
  await waitFor(() => expect(api).toHaveBeenLastCalledWith("/web/scrape/runs/run/table?offset=50&limit=50&search="));
  fireEvent.change(screen.getByLabelText("Search data"), {target:{value:"Beta"}});
  await waitFor(() => expect(api).toHaveBeenLastCalledWith("/web/scrape/runs/run/table?offset=0&limit=50&search=Beta"));
});
