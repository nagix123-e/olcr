import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { ScrapeStudio } from "./ScrapeStudio";

afterEach(cleanup);

describe("ScrapeStudio managed browser flow", () => {
  it("hydrates the managed browser opened from a chat scrape request", async () => {
    const api = vi.fn(async (path: string) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "chat-session", url: "https://example.test/catalog", last_activity: 1 }] };
      if (path === "/web/browser/profiles") return { profiles: [] };
      if (path === "/web/recipes") return { recipes: [] };
      if (path.endsWith("/snapshot")) return { url: "https://example.test/catalog", title: "Catalog", clean_markdown: "Visible catalog records" };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} initialSessionId="chat-session" initialUrl="https://example.test/catalog" initialInstruction="Collect names and prices" embedded />);
    expect(await screen.findByText("Visible catalog records")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Web scraping" })).toBeInTheDocument();
    expect(document.querySelector(".chat-scrape-studio")).toBeInTheDocument();
    expect(screen.queryByLabelText("Visible page elements")).not.toBeInTheDocument();
    expect(document.querySelector(".chat-scrape-studio")).not.toHaveClass("scrape-advanced-open");
    fireEvent.click(screen.getByRole("button", { name: "Controls" }));
    expect(screen.getByLabelText("Describe what to collect")).toHaveValue("Collect names and prices");
    expect(screen.getByRole("button", { name: "Close session" })).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/chat-session/snapshot");
    expect(api).not.toHaveBeenCalledWith("/web/browser/sessions/chat-session/picker", expect.anything());
  });

  it("restores the run started by a chat scrape request", async () => {
    const api = vi.fn(async (path: string) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "chat-session", url: "https://example.test/catalog", last_activity: 1 }] };
      if (path === "/web/browser/profiles") return { profiles: [] };
      if (path === "/web/recipes") return { recipes: [{ id: "chat-recipe", name: "Catalog", status: "VALIDATED", fields: { title: { selector: "h2" } } }] };
      if (path === "/web/scrape/runs/chat-run") return { id: "chat-run", recipe_id: "chat-recipe", status: "RUNNING", page_cursor: 1, pages_completed: 1, record_count: 3, output_format: "csv" };
      if (path.endsWith("/snapshot")) return { url: "https://example.test/catalog", title: "Catalog", clean_markdown: "Catalog in progress" };
      if (path.endsWith("/picker")) return { candidates: [] };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} initialSessionId="chat-session" initialUrl="https://example.test/catalog" initialInstruction="Collect titles" initialRunId="chat-run" embedded />);
    expect(await screen.findByText("Catalog in progress")).toBeInTheDocument();
    expect(await screen.findByText(/Started deterministic run for Catalog|Scrape run restored from chat/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(api).toHaveBeenCalledWith("/web/scrape/runs/chat-run");
  });

  it("opens a named headed login profile with the persistent-session request", async () => {
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions" && init?.method === "POST") {
        return { session_id: "profile-session", profile_id: "example-account", interactive_login: true };
      }
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "profile-session", url: "", last_activity: 1, profile_id: "example-account", interactive_login: true }] };
      if (path === "/web/browser/profiles") return { profiles: [{ profile_id: "example-account", in_use: true }] };
      if (path === "/web/recipes") return { recipes: [] };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    fireEvent.change(screen.getByLabelText("Login profile ID"), { target: { value: "example-account" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open headed login browser" })); });
    expect(api).toHaveBeenCalledWith("/web/browser/sessions", {
      method: "POST", body: JSON.stringify({ profile_id: "example-account", interactive_login: true }),
    });
    expect(await screen.findByText(/OLCR does not read password fields/)).toBeInTheDocument();
  });

  it("creates a preview-first Recipe draft from a natural-language request", async () => {
    let opened = false;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions" && init?.method === "POST") { opened = true; return { session_id: "s1" }; }
      if (path === "/web/browser/sessions") return { sessions: opened ? [{ session_id: "s1", url: "", last_activity: 1 }] : [] };
      if (path === "/web/recipes") return { recipes: [] };
      if (path.endsWith("/navigate")) return { status: "AVAILABLE", url: "https://example.com/list", title: "Directory", clean_markdown: "Directory content" };
      if (path === "/web/recipes/draft-from-text") return { id: "r1", name: "Directory", status: "DRAFT" };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open browser" })); });
    fireEvent.change(screen.getByLabelText("Public page URL"), { target: { value: "https://example.com/list" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Navigate" })); });
    fireEvent.change(await screen.findByLabelText("Describe what to collect"), { target: { value: "Collect organization names and profile links" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Propose Recipe draft" })); });
    expect(await screen.findByText(/Draft Recipe proposed: Directory/)).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/recipes/draft-from-text", {
      method: "POST", body: JSON.stringify({ session_id: "s1", instruction: "Collect organization names and profile links" }),
    });
  });

  it("reacquires visible DOM candidates after a recipe selector stops matching", async () => {
    let savedSelector = "table tr";
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "s1", url: "https://example.com/list", last_activity: 1 }] };
      if (path === "/web/recipes") return { recipes: [{ id: "r1", name: "Directory", status: "DRAFT", record_selector: savedSelector }] };
      if (path.endsWith("/validate")) throw new Error("HTTP_422: RECIPE_RECORD_SELECTOR_MATCHED_NO_ROWS");
      if (path.endsWith("/recovery-alternatives")) return { candidates: [
        { candidate_id: "r0", selector: "table tbody tr", label: "Acme", record_count: 4, field_match_counts: { name: 4 } },
        { candidate_id: "r1", selector: ".directory-card", label: "Record", record_count: 0, field_match_counts: { name: 0 } },
      ] };
      if (path.endsWith("/record-selector") && init?.method === "PATCH") {
        savedSelector = JSON.parse(String(init.body)).record_selector;
        return { id: "r1", name: "Directory", status: "DRAFT", record_selector: savedSelector };
      }
      if (path.endsWith("/snapshot")) return { url: "https://example.com/list", clean_markdown: "Updated list" };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    const validate = await screen.findByRole("button", { name: "Preview & validate" });
    await act(async () => { fireEvent.click(validate); });
    expect(await screen.findByText(/Compared 2 bounded row selector candidate/)).toBeInTheDocument();
    expect(screen.getByText("Updated list")).toBeInTheDocument();
    expect(screen.getByText(/table tbody tr/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Use this selector" })[0]).toBeEnabled();
    expect(screen.getAllByRole("button", { name: "Use this selector" })[1]).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Use this selector" })[0]); });
    expect(api).toHaveBeenCalledWith("/web/recipes/r1/record-selector", {
      method: "PATCH", body: JSON.stringify({ record_selector: "table tbody tr" }),
    });
    expect(await screen.findByText(/Recipe is DRAFT/)).toBeInTheDocument();
  });

  it("requires Recipe revalidation before resuming a run after runtime selector drift", async () => {
    let recipeStatus = "VALIDATED";
    const run = {
      id: "run-recovery", recipe_id: "recipe-recovery", status: "FAILED", page_cursor: 0,
      pages_completed: 0, record_count: 0, error: "RECIPE_SELECTOR_RECOVERY_REQUIRED",
      stop_reason: "SELECTOR_RECOVERY",
      recovery: { page_number: 1, reason: "NO_ROWS", original_selector: "table tr", recipe_updated_at: 1, unmatched_fields: [], candidates: [
        { candidate_id: "r0", selector: "table.people tr", record_count: 3, field_match_counts: { name: 3 } },
      ] },
    };
    let recipeUpdatedAt = 1;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "s1", url: "https://example.com/people", last_activity: 1 }] };
      if (path === "/web/recipes") return { recipes: [{ id: "recipe-recovery", name: "People", status: recipeStatus, record_selector: "table tr", updated_at: recipeUpdatedAt }] };
      if (path.endsWith("/runs") && init?.method === "POST") return run;
      if (path.endsWith("/record-selector") && init?.method === "PATCH") {
        recipeStatus = "DRAFT";
        recipeUpdatedAt = 2;
        return { id: "recipe-recovery", name: "People", status: "DRAFT", record_selector: "table.people tr" };
      }
      if (path.endsWith("/validate")) {
        recipeStatus = "VALIDATED";
        recipeUpdatedAt = 3;
        return { status: "VALIDATED", record_count: 3, rows: [{ name: "Acme" }] };
      }
      if (path === "/web/scrape/runs/run-recovery/resume") return { ...run, status: "QUEUED", error: null, recovery: {} };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    const start = await screen.findByRole("button", { name: "Run Recipe" });
    await act(async () => { fireEvent.click(start); });
    expect(await screen.findByRole("group", { name: "Run recovery review" })).toBeInTheDocument();
    expect(screen.getByText(/failed page was not checkpointed/)).toBeInTheDocument();
    const resume = screen.getByRole("button", { name: "Resume" });
    expect(resume).toBeDisabled();

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Use this selector" })); });
    expect(await screen.findByText(/Recipe is DRAFT/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Preview & validate" })); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Resume" })); });
    expect(api).toHaveBeenCalledWith("/web/scrape/runs/run-recovery/resume", { method: "POST" });
  });

  it("offers the existing browser accessibility snapshot when runtime selector recovery has no usable rows", async () => {
    const run = {
      id: "run-accessibility-recovery", recipe_id: "recipe-accessibility-recovery", status: "FAILED", page_cursor: 0,
      pages_completed: 0, record_count: 0, error: "RECIPE_SELECTOR_RECOVERY_REQUIRED",
      stop_reason: "SELECTOR_RECOVERY",
      recovery: { page_number: 1, reason: "NO_ROWS", original_selector: "table tr", recipe_updated_at: 1, unmatched_fields: [], candidates: [] },
    };
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [] };
      if (path === "/web/recipes") return { recipes: [{ id: "recipe-accessibility-recovery", name: "People", status: "VALIDATED", record_selector: "table tr" }] };
      if (path.endsWith("/runs") && init?.method === "POST") return run;
      if (path === "/web/scrape/runs/run-accessibility-recovery/recovery-snapshot" && init?.method === "POST") return {
        page_number: 1, url: "https://example.com/people", provider: "playwright_accessibility",
        clean_markdown: "Accessible page controls and content", persisted: false,
      };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    const runRecipe = await screen.findByRole("button", { name: "Run Recipe" });
    await act(async () => { fireEvent.click(runRecipe); });
    expect(await screen.findByRole("group", { name: "Run recovery review" })).toBeInTheDocument();

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Reopen failed page for review" })); });

    expect(api).toHaveBeenCalledWith("/web/scrape/runs/run-accessibility-recovery/recovery-snapshot", { method: "POST" });
    expect(await screen.findByText("Accessible page controls and content")).toBeInTheDocument();
    expect(screen.getByText(/transient accessibility snapshot.*untrusted page content/)).toBeInTheDocument();
    expect(await screen.findByText(/Reopened failed Recipe page 1 in a temporary managed session/)).toBeInTheDocument();
  });

  it("saves human-edited recovery selectors through Recipe PUT as a draft and requires validation before resume", async () => {
    let recipeStatus = "VALIDATED";
    let recipeUpdatedAt = 1;
    let savedBody: any = null;
    const run = {
      id: "run-manual-recovery", recipe_id: "recipe-manual-recovery", status: "FAILED", page_cursor: 0,
      pages_completed: 0, record_count: 0, error: "RECIPE_SELECTOR_RECOVERY_REQUIRED",
      stop_reason: "SELECTOR_RECOVERY",
      recovery: { page_number: 1, reason: "NO_ROWS", original_selector: "table tr", recipe_updated_at: 1, unmatched_fields: ["name"], candidates: [] },
    };
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "s1", url: "https://example.com/people", last_activity: 1 }] };
      if (path === "/web/recipes") return { recipes: [{ id: "recipe-manual-recovery", name: "People", status: recipeStatus, record_selector: recipeStatus === "DRAFT" ? ".people" : "table tr", updated_at: recipeUpdatedAt }] };
      if (path.endsWith("/runs") && init?.method === "POST") return run;
      if (path === "/web/recipes/recipe-manual-recovery" && !init?.method) return {
        id: "recipe-manual-recovery", name: "People", status: "VALIDATED", start: { url: "https://example.com/people" },
        record_selector: "table tr", fields: { name: { selector: "td.name", extract: "text" }, profile: { selector: "a.profile", extract: "href" } },
        navigation: { type: "next", selector: "a.next" }, detail: { link_field: "profile", record_selector: "body", fields: { city: { selector: ".city", extract: "text" } } },
        wait_strategy: "page_load", deduplicate_keys: ["name"], max_pages: 8, max_records: 50, max_duration_seconds: 120, output_format: "jsonl",
      };
      if (path === "/web/recipes/recipe-manual-recovery" && init?.method === "PUT") {
        savedBody = JSON.parse(String(init.body));
        recipeStatus = "DRAFT"; recipeUpdatedAt = 2;
        return { id: "recipe-manual-recovery", name: "People", status: "DRAFT", record_selector: savedBody.record_selector };
      }
      if (path.endsWith("/validate")) {
        recipeStatus = "VALIDATED"; recipeUpdatedAt = 3;
        return { status: "VALIDATED", record_count: 2, rows: [{ name: "Acme" }] };
      }
      if (path === "/web/scrape/runs/run-manual-recovery/resume") return { ...run, status: "QUEUED", recovery: null };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<ScrapeStudio api={api} />);
    const runRecipeButton = await screen.findByRole("button", { name: "Run Recipe" });
    await act(async () => { fireEvent.click(runRecipeButton); });
    expect(await screen.findByRole("group", { name: "Run recovery review" })).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Edit Recipe fields" })); });
    expect(await screen.findByRole("region", { name: "Edit Recipe fields" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Recovery record selector"), { target: { value: ".people" } });
    fireEvent.change(screen.getByLabelText("Recovery field selector name"), { target: { value: ".person-name" } });
    fireEvent.change(screen.getByLabelText("Recovery detail field selector city"), { target: { value: ".person-city" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save as draft" })); });

    expect(savedBody).toMatchObject({
      start_url: "https://example.com/people", record_selector: ".people",
      fields: { name: { selector: ".person-name", extract: "text" }, profile: { selector: "a.profile", extract: "href" } },
      navigation: { type: "next", selector: "a.next" },
      detail: { link_field: "profile", record_selector: "body", fields: { city: { selector: ".person-city", extract: "text" } } },
      wait_strategy: "page_load", deduplicate_keys: ["name"], max_pages: 8, max_records: 50, max_duration_seconds: 120, output_format: "jsonl",
    });
    expect(await screen.findByText(/Saved People as DRAFT/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Preview & validate" })); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Resume" })); });
    expect(api).toHaveBeenCalledWith("/web/scrape/runs/run-manual-recovery/resume", { method: "POST" });
  });

  it("opens a session, navigates, displays the snapshot, and closes it", async () => {
    const session = { session_id: "session-1", url: "", last_activity: 1 };
    let isOpen = false;
    let savedRecipe: any = null;
    const scrapeRun = { id: "run-1", status: "COMPLETED", page_cursor: 2, pages_completed: 2, record_count: 2, output_format: "csv", stop_reason: "MAX_PAGES" };
    const download = vi.fn(async () => {});
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions" && init?.method === "POST") {
        isOpen = true; return { status: "READY", session_id: session.session_id };
      }
      if (path === "/web/browser/sessions") return { sessions: isOpen ? [session] : [] };
      if (path === "/web/recipes" && init?.method === "POST") {
        savedRecipe = { id: "recipe-1", ...JSON.parse(String(init.body)), status: "DRAFT", record_selector: "table > tbody > tr" };
        return savedRecipe;
      }
      if (path.endsWith("/validate")) { if (savedRecipe) savedRecipe.status = "VALIDATED"; return { status: "VALIDATED", record_count: 2, rows: [{ organization: "Acme" }] }; }
      if (path.endsWith("/runs") && init?.method === "POST") return scrapeRun;
      if (path === "/web/scrape/runs/run-1") return scrapeRun;
      if (path === "/web/recipes") return { recipes: savedRecipe ? [savedRecipe] : [] };
      if (path.endsWith("/navigate")) return { status: "AVAILABLE", title: "Example", url: "https://example.com/", clean_markdown: "Main page text", warnings: [] };
      if (path.endsWith("/snapshot")) return { status: "AVAILABLE", clean_markdown: "Refreshed page" };
      if (path.endsWith("/back")) return { status: "AVAILABLE", title: "Previous", url: "https://example.com/previous", clean_markdown: "Previous page text" };
      if (path.endsWith("/reload")) return { status: "AVAILABLE", title: "Example", url: "https://example.com/", clean_markdown: "Reloaded page text" };
      if (path.endsWith("/network")) return { status: "AVAILABLE", requests: [{ index: 1, method: "GET", url: "https://example.com/api/items", status_code: 200 }] };
      if (path.endsWith("/picker")) {
        const kind = JSON.parse(String(init?.body)).kind;
        const row = { kind: "row", tag: "tr", role: "row", text: "Acme · 42", attributes: {}, bbox: { x: 1, y: 2, width: 300, height: 28 }, dom_path: "table > tbody > tr:nth-of-type(2)", selector_candidates: ["table > tbody > tr:nth-of-type(2)"] };
        const field = { kind: "field", tag: "td", role: "cell", text: "Acme", attributes: {}, bbox: { x: 2, y: 3, width: 100, height: 24 }, dom_path: "table > tbody > tr:nth-of-type(2) > td:nth-of-type(1)", selector_candidates: ["table > tbody > tr:nth-of-type(2) > td:nth-of-type(1)"], relative_selector: "td:nth-of-type(1)" };
        return { status: "AVAILABLE", candidates: kind === "field" ? [field] : [row] };
      }
      if (path === "/web/browser/sessions/session-1" && init?.method === "DELETE") {
        isOpen = false; return { closed: true };
      }
      throw new Error(`Unexpected API call ${path}`);
    });

    render(<ScrapeStudio api={api} download={download}/>);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/web/browser/sessions"));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open browser" })); });
    await waitFor(() => expect(screen.getByText(/managed browser is ready/i)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Public page URL"), { target: { value: "https://example.com/" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Navigate" })); });
    expect(await screen.findByText("Main page text")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Reload page" })); });
    expect(await screen.findByText("Reloaded page text")).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/session-1/reload", { method: "POST" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect fetch/XHR metadata" })); });
    expect(await screen.findByText("https://example.com/api/items")).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/session-1/network");
    expect(screen.getByText("Example")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Go back" })); });
    expect(await screen.findByText("Previous page text")).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/session-1/back", { method: "POST" });
    fireEvent.change(screen.getByLabelText("Public page URL"), { target: { value: "https://example.com/" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Navigate" })); });
    expect(await screen.findByText("Main page text")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect page" })); });
    fireEvent.click(await screen.findByRole("button", { name: /Acme · 42/ }));
    expect(screen.getByLabelText("Selected element evidence")).toHaveTextContent("Selector candidates");
    fireEvent.change(screen.getByLabelText("Element type"), { target: { value: "field" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect page" })); });
    fireEvent.click(await screen.findByRole("button", { name: /cell · Acme/ }));
    fireEvent.change(screen.getByLabelText("Output field name"), { target: { value: "organization" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add field mapping" })); });
    expect(screen.getByLabelText("Recipe output field mappings")).toHaveTextContent("td:nth-of-type(1)");
    fireEvent.change(screen.getByLabelText("Element type"), { target: { value: "row" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect page" })); });
    fireEvent.click(await screen.findByRole("button", { name: /Acme · 42/ }));
    fireEvent.change(screen.getByLabelText("Element type"), { target: { value: "field" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect page" })); });
    fireEvent.click(await screen.findByRole("button", { name: /cell · Acme/ }));
    fireEvent.change(screen.getByLabelText("Output field name"), { target: { value: "profile" } });
    fireEvent.change(screen.getByLabelText("Extract"), { target: { value: "href" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add field mapping" })); });
    fireEvent.change(screen.getByLabelText("Element type"), { target: { value: "row" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect page" })); });
    fireEvent.click(await screen.findByRole("button", { name: /Acme · 42/ }));
    fireEvent.click(screen.getByLabelText("Visit each row's detail page"));
    fireEvent.change(screen.getByLabelText("Mapped detail link field"), { target: { value: "profile" } });
    fireEvent.change(screen.getByLabelText("Detail output field name"), { target: { value: "city" } });
    fireEvent.change(screen.getByLabelText("Detail field CSS selector"), { target: { value: ".city" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add detail field" })); });
    fireEvent.change(screen.getByLabelText("Navigation"), { target: { value: "next" } });
    fireEvent.change(screen.getByLabelText("Next link CSS selector"), { target: { value: "a.next-page" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save draft Recipe" })); });
    expect(await screen.findByText(/Draft Recipe saved: Example with 2 field mapping/i)).toBeInTheDocument();
    const saveCall = api.mock.calls.find(([path, init]) => path === "/web/recipes" && init?.method === "POST");
    expect(JSON.parse(String(saveCall?.[1]?.body))).toMatchObject({
      start_url: "https://example.com/", record_selector: "table > tbody > tr", fields: { organization: { selector: "td:nth-of-type(1)", extract: "text" } }, max_pages: 2,
      navigation: { type: "next", selector: "a.next-page" },
      detail: { link_field: "profile", record_selector: "body", fields: { city: { selector: ".city", extract: "text" } } },
    });
    fireEvent.change(screen.getByLabelText("Navigation"), { target: { value: "load_more" } });
    fireEvent.change(screen.getByLabelText("Load more button CSS selector"), { target: { value: "button.more" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save draft Recipe" })); });
    await waitFor(() => expect(api.mock.calls.filter(([path, init]) => path === "/web/recipes" && init?.method === "POST")).toHaveLength(2));
    const loadMoreCall = api.mock.calls.filter(([path, init]) => path === "/web/recipes" && init?.method === "POST")[1];
    expect(JSON.parse(String(loadMoreCall?.[1]?.body))).toMatchObject({ navigation: { type: "load_more", selector: "button.more" } });
    await act(async () => { fireEvent.click(await screen.findByRole("button", { name: "Preview & validate" })); });
    expect(await screen.findByLabelText("Recipe preview")).toHaveTextContent('"organization": "Acme"');
    expect(screen.getByText(/validated Example/i)).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Run Recipe" })); });
    expect(await screen.findByLabelText("Scrape run status")).toHaveTextContent("2 unique record(s)");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Export" })); });
    expect(download).toHaveBeenCalledWith("/web/scrape/runs/run-1/export?format=csv", "olcr-run-1.csv");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Close session" })); });
    await waitFor(() => expect(screen.getByText("Browser session closed.")).toBeInTheDocument());
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/session-1", { method: "DELETE" });
  });

  it("reads only a user-selected JSON network response and labels the preview transient", async () => {
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "s1", url: "https://example.com/list", last_activity: 1 }] };
      if (path.endsWith("/navigate")) return { status: "AVAILABLE", url: "https://example.com/list?session_token=secret#fragment", title: "List", clean_markdown: "List page" };
      if (path.endsWith("/network")) return { status: "AVAILABLE", requests: [{ index: 2, method: "GET", url: "https://example.com/api/items", status_code: 200 }] };
      if (path.endsWith("/network/2/json")) return { status: "AVAILABLE", request_index: 2, url: "https://example.com/api/items", status_code: 200, content_type: "application/json", preview: { items: [{ name: "Ada", token: "[redacted]" }] }, truncated: true, persisted: false };
      if (path === "/web/recipes") return { recipes: [] };
      throw new Error(`Unexpected API call ${path} ${init?.method || ""}`);
    });
    render(<ScrapeStudio api={api} />);
    await screen.findByRole("button", { name: "Close session" });
    fireEvent.change(screen.getByLabelText("Public page URL"), { target: { value: "https://example.com/list" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Navigate" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Navigate" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect fetch/XHR metadata" })); });
    expect(await screen.findByText("https://example.com/api/items")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Review JSON response" })); });
    expect(await screen.findByRole("group", { name: "Reviewed JSON response" })).toBeInTheDocument();
    expect(screen.getByText(/transient preview \(redacted or truncated\)/)).toBeInTheDocument();
    expect(screen.getByText(/\[redacted\]/)).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/s1/network/2/json");
  });

  it("creates a JSON Recipe from explicit JSON Pointer mappings without persisting the response body", async () => {
    let saved: any = null;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/web/browser/sessions") return { sessions: [{ session_id: "s1", url: "https://example.com/list", last_activity: 1 }] };
      if (path.endsWith("/navigate")) return { status: "AVAILABLE", url: "https://example.com/list", title: "List", clean_markdown: "List page" };
      if (path.endsWith("/network")) return { status: "AVAILABLE", requests: [{ index: 2, method: "GET", url: "https://example.com/api/items?cursor=secret", status_code: 200 }] };
      if (path.endsWith("/network/2/json")) return { status: "AVAILABLE", request_index: 2, url: "https://example.com/api/items", status_code: 200, content_type: "application/json", preview: { items: [{ name: "Ada" }] }, truncated: false, persisted: false };
      if (path === "/web/recipes" && init?.method === "POST") {
        saved = { id: "json-r1", ...JSON.parse(String(init.body)), status: "DRAFT" };
        return saved;
      }
      if (path === "/web/recipes") return { recipes: saved ? [saved] : [] };
      throw new Error(`Unexpected API call ${path} ${init?.method || ""}`);
    });
    render(<ScrapeStudio api={api} />);
    await screen.findByRole("button", { name: "Close session" });
    fireEvent.change(screen.getByLabelText("Public page URL"), { target: { value: "https://example.com/list" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Navigate" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Navigate" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Inspect fetch/XHR metadata" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Review JSON response" })); });
    fireEvent.change(screen.getByLabelText("Records JSON Pointer"), { target: { value: "/items" } });
    fireEvent.change(screen.getByLabelText("JSON Recipe field name"), { target: { value: "name" } });
    fireEvent.change(screen.getByLabelText("Field JSON Pointer"), { target: { value: "/name" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add JSON field" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save JSON Recipe draft" })); });
    expect(await screen.findByText(/Draft JSON Recipe saved/)).toBeInTheDocument();
    expect(saved.source).toBe("network_json");
    expect(saved.start_url).toBe("https://example.com/list");
    expect(saved.network_json).toEqual({ endpoint_path: "/api/items", records_pointer: "/items", fields: { name: { pointer: "/name" } } });
    expect(JSON.stringify(saved)).not.toContain("cursor=secret");
    expect(JSON.stringify(saved)).not.toContain('"preview"');
  });
});

it("previews automatic extraction before validating and starting collection", async () => {
  const api = vi.fn(async (path: string) => {
    if (path === "/web/browser/sessions") return {sessions:[{session_id:"quick",url:"https://example.test/",last_activity:1}]};
    if (path === "/web/browser/profiles") return {profiles:[]};
    if (path === "/web/recipes") return {recipes:[]};
    if (path.endsWith("/snapshot")) return {clean_markdown:"Contacts"};
    if (path.endsWith("/picker")) return {candidates:[]};
    if (path.endsWith("/screenshot")) throw new Error("no screenshot in test");
    if (path === "/web/recipes/auto") return {recipe:{id:"quick-recipe",name:"Emails"},rows:[{email:"one@example.com"}],record_count:1};
    if (path.endsWith("/validate")) return {status:"VALIDATED"};
    if (path.endsWith("/runs")) return {id:"quick-run",recipe_id:"quick-recipe",status:"RUNNING",record_count:0,page_cursor:0,pages_completed:0};
    if (path.includes("/table?")) return {columns:[],rows:[],total:0};
    throw new Error(`Unexpected ${path}`);
  });
  render(<ScrapeStudio api={api} initialSessionId="quick" initialUrl="https://example.test/" embedded />);
  await screen.findByText("Contacts");
  fireEvent.change(screen.getByLabelText("Extraction tool"), {target:{value:"emails"}});
  expect(screen.getByRole("button",{name:"Start previewed collection"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"Preview automatic extraction"}));
  expect(await screen.findByText(/Found 1 records/)).toBeInTheDocument();
  expect(api.mock.calls.some(([path])=>path.endsWith("/runs"))).toBe(false);
  fireEvent.click(screen.getByRole("button",{name:"Start previewed collection"}));
  await waitFor(()=>expect(api.mock.calls.some(([path])=>path.endsWith("/validate"))).toBe(true));
  await waitFor(()=>expect(api.mock.calls.some(([path])=>path.endsWith("/runs"))).toBe(true));
});
