import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { RoutinePanel } from "./RoutinePanel";

afterEach(cleanup);

describe("RoutinePanel", () => {
  it("compiles matching confirmed traces into a visible non-executable candidate", async () => {
    let compiled = false;
    let reviewDecision: string | null = null;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/routines/candidates/compile") {
        compiled = true;
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({ trace_ids: ["trace-1", "trace-2"] });
        return { status: "CANDIDATE", procedure: { name: "Candidate open disclosure" } };
      }
      if (path === "/routines/candidates/candidate-1/review") {
        reviewDecision = JSON.parse(String(init?.body)).decision;
        return { status: "CANDIDATE", validated: false, review: { decision: reviewDecision } };
      }
      if (path === "/routines/match") {
        expect(JSON.parse(String(init?.body))).toEqual({ question: "Open the Details section" });
        return { status: "CANDIDATE_MATCHED", matches: [{ procedure_id: "candidate-1", element_id: "ax-9" }] };
      }
      if (path === "/routines/candidates") return {
        traces: compiled ? [] : [
          { id: "trace-1", action: "open_disclosure", goal_signature: "same-goal", captured_at: 100 },
          { id: "trace-2", action: "open_disclosure", goal_signature: "same-goal", captured_at: 101 },
          { id: "trace-other", action: "open_find", goal_signature: "other-goal", captured_at: 102 },
        ],
        candidates: compiled ? [{ id: "candidate-1", name: "Candidate open disclosure", validated: false, run_stats: { runs: 1, verified_successes: 1, failures: 0 }, source_trace_ids: ["trace-1", "trace-2"],
          review: reviewDecision ? { decision: reviewDecision, reviewed_at: 100 } : null,
          action: { kind: "open_disclosure" }, context: { source: "AX", application_bundle_id: "com.example.demo" },
          actions: [{ mechanism: "accessibility" }], preconditions: [{ expanded: false }], expected_effects: [{ expanded: true }] }] : [],
      };
      throw new Error(`Unexpected API call ${path}`);
    });
    render(<RoutinePanel api={api} />);
    const checkboxes = await screen.findAllByRole("checkbox");
    await act(async () => {
      fireEvent.click(checkboxes[0]);
      fireEvent.click(checkboxes[1]);
    });
    expect(screen.getByRole("button", { name: "Compile candidate" })).toBeEnabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Compile candidate" })); });
    expect(await screen.findByText("Draft candidate created: Candidate open disclosure.")).toBeInTheDocument();
    expect(screen.getByText("Candidate open disclosure")).toBeInTheDocument();
    expect(screen.getByText("Awaiting review · not executable")).toBeInTheDocument();
    expect(screen.getByText(/Expected effect:.*expanded/)).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Approve review" })); });
    expect(await screen.findByText("Review approval recorded; candidate remains non-executable.")).toBeInTheDocument();
    expect(screen.getByText("Approved · not executable")).toBeInTheDocument();
    expect(screen.getByText("Verified uses: 1 · recorded failures: 0")).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/routines/candidates/candidate-1/review", expect.objectContaining({ method: "POST" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Goal to match against World State" }),
      { target: { value: "Open the Details section" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Check current World State" })); });
    expect(await screen.findByText(/Candidate matched \(candidate-1\) to ax-9/)).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/routines/match", expect.objectContaining({ method: "POST" }));
    expect(api).toHaveBeenCalledWith("/routines/candidates/compile", expect.any(Object));
  });

  it("only allows traces from the same action and goal to be combined", async () => {
    const api = vi.fn(async () => ({ traces: [
      { id: "trace-1", action: "open_disclosure", goal_signature: "goal-a", captured_at: 100 },
      { id: "trace-2", action: "open_find", goal_signature: "goal-b", captured_at: 101 },
    ], candidates: [] }));
    render(<RoutinePanel api={api} />);
    await screen.findByText("open disclosure");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    expect(checkboxes[1]).toBeDisabled();
    expect(screen.getByRole("button", { name: "Compile candidate" })).toBeDisabled();
  });

  it("shows suspect candidates as disabled and requires fresh traces for replacement", async () => {
    const api = vi.fn(async () => ({ traces: [], candidates: [{
      id: "suspect-1", name: "Candidate open find", status: "SUSPECT", validated: false,
      run_stats: { runs: 3, verified_successes: 1, failures: 2 }, source_trace_ids: ["trace-a", "trace-b"],
      review: { decision: "APPROVED", reviewed_at: 100 }, action: { kind: "open_find" },
      context: { source: "AX", application_bundle_id: "com.example.demo" },
      preconditions: [{ role: "AXMenuItem" }], expected_effects: [{ focused: true }],
      actions: [{ mechanism: "keyboard_shortcut" }],
    }] }));
    render(<RoutinePanel api={api} />);
    expect(await screen.findByText("Suspect · automatic matching disabled")).toBeInTheDocument();
    expect(screen.getByText("Verified uses: 1 · recorded failures: 2")).toBeInTheDocument();
    expect(screen.getByText("Compile a replacement from new confirmed traces to review this routine again.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve review" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("ignores malformed records from the persisted routine inventory", async () => {
    const api = vi.fn(async () => ({ traces: [
      { id: "missing-action", goal_signature: "goal-a", captured_at: 100 },
      { id: "non-finite-time", action: "open_find", goal_signature: "goal-a", captured_at: Number.NaN },
    ], candidates: [{ id: "bad-candidate" }] }));
    render(<RoutinePanel api={api} />);
    expect(await screen.findByText("No confirmed traces have been captured yet.")).toBeInTheDocument();
    expect(screen.getByText("No routine candidates have been compiled.")).toBeInTheDocument();
  });
});
