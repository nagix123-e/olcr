import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
vi.mock("./api", () => ({ api: vi.fn(async () => ({})) }));
import { api } from "./api";
import { AccessibilityPanel } from "./AccessibilityPanel";

describe("AccessibilityPanel", () => {
  let originalDescriptor: PropertyDescriptor | undefined;
  afterEach(() => {
    cleanup();
    if (originalDescriptor) Object.defineProperty(window, "__TAURI__", originalDescriptor);
    else delete (window as Window & { __TAURI__?: unknown }).__TAURI__;
    originalDescriptor = undefined;
    vi.mocked(api).mockClear();
  });

  it("keeps OS permissions and chat-directed targeting in the policy view", async () => {
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return false;
      if (command === "pointer_event_permission_status") return false;
      if (command === "screen_capture_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "list_running_accessibility_applications") return [];
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel view="policy" />);

    expect(await screen.findByRole("button", { name: "Request Accessibility Permission" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request Screen Recording Permission" })).toBeInTheDocument();
    expect(screen.getByText("Choose the app in chat")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Observe focused window" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh unified World State" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start watching selected app" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("requests Screen Recording from Settings and exposes refresh without tying it to capture start", async () => {
    let granted = false;
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status" || command === "pointer_event_permission_status") return false;
      if (command === "screen_capture_permission_status") return granted;
      if (command === "request_screen_capture_permission") { granted = true; return true; }
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel view="policy" />);
    const request = await screen.findByRole("button", { name: "Request Screen Recording Permission" });
    expect(invoke).not.toHaveBeenCalledWith("request_screen_capture_permission");
    fireEvent.click(request);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("request_screen_capture_permission"));
    expect(await screen.findByText("Screen Recording permission granted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request Screen Recording Permission" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh screen capture status" })).toBeInTheDocument();
  });

  it("keeps observation and World State tools in the separate diagnostics view", async () => {
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel view="diagnostics" />);

    expect(await screen.findByRole("button", { name: "Observe focused window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh unified World State" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request Accessibility Permission" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request Screen Recording Permission" })).not.toBeInTheDocument();
    expect(screen.queryByText("Computer Use app scope")).not.toBeInTheDocument();
  });

  it("checks permission without prompting and reads the focused window only after user actions", async () => {
    const observation = {
      permissionGranted: true, observedAt: 1, sessionId: "macos-ax-123", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123,
      windowTitle: "Example Window", truncated: false, uncertainty: [],
      elements: [{ id: "ax-0", role: "AXButton", label: "Continue", frame: null, enabled: true, focused: false, selected: null, depth: 1 }],
    };
    let trusted = false;
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return trusted;
      if (command === "pointer_event_permission_status") return false;
      if (command === "screen_capture_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "request_accessibility_permission") { trusted = true; return true; }
      if (command === "observe_focused_accessibility") return observation;
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    const panel = render(<AccessibilityPanel view="policy" />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("accessibility_permission_status"));
    expect(invoke).not.toHaveBeenCalledWith("request_accessibility_permission");
    expect(invoke).not.toHaveBeenCalledWith("observe_focused_accessibility");

    fireEvent.click(screen.getByRole("button", { name: "Request Accessibility Permission" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("request_accessibility_permission"));
    panel.rerender(<AccessibilityPanel view="diagnostics" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Observe focused window" })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Observe focused window" }));
    await waitFor(() => expect(screen.getByLabelText("Focused window observation")).toHaveTextContent("Example Window"));
    expect(screen.getByLabelText("Focused window observation")).toHaveTextContent("AXButton Continue");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/desktop/world-state", expect.objectContaining({ method: "PUT" })));
  });

  it("observes only an explicitly selected running app and checks its PID and bundle identity", async () => {
    const observation = { observationId: "ax-textedit", permissionGranted: true, observedAt: 2,
      sessionId: "macos-ax-445", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 445,
      applicationBundleId: "com.apple.TextEdit", windowTitle: "Untitled", elements: [], truncated: false, uncertainty: [] };
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "list_running_accessibility_applications") return [{ applicationPid: 445, applicationName: "TextEdit", applicationBundleId: "com.apple.TextEdit" }];
      if (command === "observe_accessibility_application") return observation;
      throw new Error(`Unexpected command ${command}`);
    });
    const onTargetApplicationChange = vi.fn();
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel onTargetApplicationChange={onTargetApplicationChange} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh app list" })).toBeInTheDocument());
    expect(invoke).not.toHaveBeenCalledWith("list_running_accessibility_applications");
    fireEvent.click(screen.getByRole("button", { name: "Refresh app list" }));
    await waitFor(() => expect(screen.getByRole("option", { name: /TextEdit · com\.apple\.TextEdit · PID 445/ })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Observe selected app" })).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox", { name: "Target app" }), { target: { value: "445" } });
    expect(onTargetApplicationChange).toHaveBeenCalledWith({ applicationPid: 445, applicationBundleId: "com.apple.TextEdit" });
    fireEvent.click(screen.getByRole("button", { name: "Observe selected app" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("observe_accessibility_application", { applicationPid: 445 }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/desktop/world-state", expect.objectContaining({
      method: "PUT", body: expect.stringContaining('"applicationBundleId":"com.apple.TextEdit"'),
    })));
    expect(screen.getByLabelText("Focused window observation")).toHaveTextContent("Untitled");
  });

  it("keeps app targeting chat-driven instead of requiring per-app grants", async () => {
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return false;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "list_running_accessibility_applications") return [
        { applicationPid: 445, applicationName: "TextEdit", applicationBundleId: "com.apple.TextEdit" },
      ];
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel view="policy" />);
    expect(screen.getByText("Choose the app in chat")).toBeInTheDocument();
    expect(screen.getByText(/name the app in your message/i)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Allow .* for Computer Use/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Application bundle ID")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add bundle ID to scope" })).not.toBeInTheDocument();
  });

  it("subscribes to AX invalidation only after an explicit start and lets the user stop it", async () => {
    let listener: ((event: { payload: { source: string; notification: string; applicationPid: number; sessionId: string; observedAt: number } }) => void) | undefined;
    let active = false;
    const listen = vi.fn(async (_name: string, callback: typeof listener) => { listener = callback; return () => { listener = undefined; }; });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active, applicationPid: null };
      if (command === "list_running_accessibility_applications") return [{ applicationPid: 123, applicationName: "Example", applicationBundleId: "example.app" }];
      if (command === "observe_accessibility_application") return { permissionGranted: true, observedAt: 1, sessionId: "macos-ax-123", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123, applicationBundleId: "example.app", windowTitle: "Example", elements: [], truncated: false, uncertainty: [] };
      if (command === "signal_perception") return { state: "ACTIVE", reason: "accessibility_change", targetFps: 8, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 1, burstAvailable: true };
      if (command === "start_accessibility_monitoring") { active = true; return { active, applicationPid: 123 }; }
      if (command === "stop_accessibility_monitoring") { active = false; return { active, applicationPid: null }; }
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen } } });

    render(<AccessibilityPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Start watching selected app" })).toBeInTheDocument());
    expect(listen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh app list" }));
    await screen.findByRole("option", { name: /Example · example\.app · PID 123/ });
    fireEvent.change(screen.getByRole("combobox", { name: "Target app" }), { target: { value: "123" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Start watching selected app" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Start watching selected app" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop watching selected app" })).toBeInTheDocument());
    expect(invoke).toHaveBeenCalledWith("start_accessibility_monitoring", { applicationPid: 123 });
    listener?.({ payload: { source: "AX", notification: "AXFocusedUIElementChanged", applicationPid: 123, sessionId: "macos-ax-123", observedAt: 2 } });
    await waitFor(() => expect(screen.getByText(/AX change received/)).toBeInTheDocument());
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "ax_change" });
    fireEvent.click(screen.getByRole("button", { name: "Stop watching selected app" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start watching selected app" })).toBeInTheDocument());
    expect(invoke).toHaveBeenCalledWith("stop_accessibility_monitoring");
  });

  it("routes an explicitly selected AX disclosure through the router and verifies a fresh state", async () => {
    const before = { observationId: "ax-before", permissionGranted: true, observedAt: 100, sessionId: "macos-ax-123",
      coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123, windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "ax-2", role: "AXDisclosureTriangle", label: "Details", frame: { x: 5, y: 10, width: 20, height: 20 }, enabled: true,
        focused: false, selected: null, expanded: false, supportedActions: ["AXPress"], depth: 1 }] };
    const after = { ...before, observationId: "ax-after", observedAt: 101,
      elements: [{ ...before.elements[0], expanded: true }] };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") return { plan_id: "plan-1", selected_element: { id: "ax-2" }, action_route: {
        status: "PASS", mechanism: "accessibility", verification_required: true,
        observation_id: "ax-before", session_id: "macos-ax-123",
      } };
      if (path === "/computer/actions/open-disclosure/verify") return { verified: true, status: "PASS" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "press_accessibility_disclosure") return { status: "PASS", verified: true, executionAttempted: true, observation: after };
      if (command === "signal_perception") return { controllerId: "controller-1", revision: 1, state: "ACTIVE", reason: "action_trigger", targetFps: 8, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 1, burstAvailable: false };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with Accessibility" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify", expect.objectContaining({ method: "POST" })));
    expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/plan", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"project_id":"project-1"'),
    }));
    expect(invoke).toHaveBeenCalledWith("press_accessibility_disclosure", {
      observationId: "ax-before", sessionId: "macos-ax-123", applicationPid: 123, elementId: "ax-2", label: "Details",
    });
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "action_trigger" });
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "expected_effect" });
    expect(await screen.findByLabelText("Focused window observation")).toHaveTextContent("AXDisclosureTriangle Details");
    expect(screen.queryByRole("button", { name: "Open with Accessibility" })).not.toBeInTheDocument();
  });

  it("executes an approved routine plan only through native dispatch and fresh API verification", async () => {
    const before = { observationId: "routine-before", permissionGranted: true, observedAt: 100,
      sessionId: "macos-ax-routine", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 321,
      applicationBundleId: "example.target", windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "routine-target", role: "AXDisclosureTriangle", label: "Details",
        frame: { x: 5, y: 10, width: 20, height: 20 }, enabled: true, focused: false,
        selected: null, expanded: false, supportedActions: ["AXPress"], depth: 1 }] };
    const after = { ...before, observationId: "routine-after", observedAt: 101,
      elements: [{ ...before.elements[0], expanded: true }] };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") return {
        plan_id: "routine-plan", routine_candidate_id: "routine-candidate",
        decision: { selection_source: "APPROVED_VERIFIED_ROUTINE" },
        selected_element: { id: "routine-target" },
        action_route: { status: "PASS", mechanism: "accessibility", verification_required: true,
          observation_id: "routine-before", session_id: "macos-ax-routine" },
      };
      if (path === "/computer/actions/open-disclosure/verify") return { verified: true, status: "PASS" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "press_accessibility_disclosure") return { status: "PASS", verified: true, executionAttempted: true, observation: after };
      if (command === "signal_perception") return { state: "ACTIVE", reason: "action_trigger" };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with Accessibility" }));

    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify",
      expect.objectContaining({ method: "POST", body: expect.stringContaining('"plan_id":"routine-plan"') })));
    expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/plan", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"question":"Open Details"'),
    }));
    expect(invoke).toHaveBeenCalledWith("press_accessibility_disclosure", {
      observationId: "routine-before", sessionId: "macos-ax-routine", applicationPid: 321,
      elementId: "routine-target", label: "Details",
    });
    const verificationCall = vi.mocked(api).mock.calls.find(call => call[0] === "/computer/actions/open-disclosure/verify");
    expect(JSON.parse(String(verificationCall?.[1]?.body))).toMatchObject({
      plan_id: "routine-plan", observation: { ax: { observationId: "routine-after" } },
      execution_receipt: { source: "native_accessibility", action_kind: "open_disclosure",
        status: "PASS", execution_attempted: true },
    });
    expect(await screen.findByLabelText("Focused window observation")).toHaveTextContent("AXDisclosureTriangle Details");
    expect(screen.queryByText(/not verified|not confirmed/i)).not.toBeInTheDocument();
  });

  it("reports a native disclosure attempt whose fresh postcondition was not confirmed", async () => {
    const before = { observationId: "ax-before-failed", permissionGranted: true, observedAt: 100,
      sessionId: "macos-ax-123", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123,
      applicationBundleId: "example.target", windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "ax-2", role: "AXDisclosureTriangle", label: "Details",
        frame: { x: 5, y: 10, width: 20, height: 20 }, enabled: true, focused: false,
        selected: null, expanded: false, supportedActions: ["AXPress"], depth: 1 }] };
    const after = { ...before, observationId: "ax-after-failed", observedAt: 101 };
    let planCalls = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") {
        planCalls += 1;
        return { plan_id: planCalls === 1 ? "plan-failed" : "plan-recovery", selected_element: { id: "ax-2" }, action_route: {
          status: "PASS", mechanism: "accessibility", verification_required: true,
          observation_id: planCalls === 1 ? "ax-before-failed" : "ax-after-failed", session_id: "macos-ax-123",
        }, ...(planCalls === 1 ? { routine_candidate_id: "candidate-failed" } : {}) };
      }
      if (path === "/computer/actions/open-disclosure/verify") return { status: "FAILED", verified: false,
        verification_state: "NOT_CONFIRMED", recovery_offer: true, recovery_question: "Open Details" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "press_accessibility_disclosure") return { status: "FAILED", verified: false,
        executionAttempted: true, observation: after };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with Accessibility" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify",
      expect.objectContaining({ method: "POST" })));
    const failedVerify = vi.mocked(api).mock.calls.find(call => call[0] === "/computer/actions/open-disclosure/verify");
    expect(JSON.parse(String(failedVerify?.[1]?.body))).toMatchObject({
      plan_id: "plan-failed",
      execution_receipt: { source: "native_accessibility", action_kind: "open_disclosure",
        status: "FAILED", execution_attempted: true },
    });
    const persistedFailure = vi.mocked(api).mock.calls.filter(call => call[0] === "/desktop/world-state").at(-1);
    expect(String(persistedFailure?.[1]?.body)).toContain('"observationId":"ax-after-failed"');
    expect(await screen.findByText("Expected effect was not confirmed. Review the available replan before choosing whether to execute it.")).toBeInTheDocument();
    const replan = await screen.findByRole("button", { name: "Replan from latest observation" });
    expect(invoke.mock.calls.filter(call => call[0] === "press_accessibility_disclosure")).toHaveLength(1);
    fireEvent.click(replan);
    expect(await screen.findByRole("button", { name: "Execute replanned action" })).toBeInTheDocument();
    expect(invoke.mock.calls.filter(call => call[0] === "press_accessibility_disclosure")).toHaveLength(1);
    const recoveryPlanCall = vi.mocked(api).mock.calls.filter(call => call[0] === "/computer/actions/open-disclosure/plan").at(-1);
    expect(JSON.parse(String(recoveryPlanCall?.[1]?.body))).toMatchObject({ bypass_routine_cache: true });
  });

  it("asks for a fresh observation after an inconclusive result without offering a replan", async () => {
    const before = { observationId: "ax-before-inconclusive", permissionGranted: true, observedAt: 100,
      sessionId: "macos-ax-123", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123,
      applicationBundleId: "example.target", windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "ax-2", role: "AXDisclosureTriangle", label: "Details",
        frame: { x: 5, y: 10, width: 20, height: 20 }, enabled: true, focused: false,
        selected: null, expanded: false, supportedActions: ["AXPress"], depth: 1 }] };
    const after = { ...before, observationId: "ax-after-inconclusive", observedAt: 101 };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") return { plan_id: "plan-inconclusive",
        selected_element: { id: "ax-2" }, action_route: { status: "PASS", mechanism: "accessibility",
          verification_required: true, observation_id: "ax-before-inconclusive", session_id: "macos-ax-123" } };
      if (path === "/computer/actions/open-disclosure/verify") return { status: "FAILED", verified: false,
        verification_state: "INCONCLUSIVE", recovery_offer: false, reason: "OBSERVATION_STALE" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status" || command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "press_accessibility_disclosure") return { status: "FAILED", verified: false,
        executionAttempted: true, observation: after };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with Accessibility" }));
    expect(await screen.findByText("Post-action observation is inconclusive. Observe the focused window again before retrying.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replan from latest observation" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Observe focused window" })).toBeInTheDocument();
  });

  it("uses the deterministic pointer fallback only when the router selects it", async () => {
    const before = { observationId: "pointer-before", permissionGranted: true, observedAt: 100, sessionId: "macos-ax-123",
      coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123, applicationBundleId: "example.target",
      windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "ax-2", role: "AXDisclosureTriangle", label: "Details", frame: { x: 5, y: 10, width: 20, height: 20 }, enabled: true,
        focused: false, selected: null, expanded: false, supportedActions: [], depth: 1 }] };
    const after = { ...before, observationId: "pointer-after", observedAt: 101,
      elements: [{ ...before.elements[0], expanded: true }] };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") return { plan_id: "pointer-plan", selected_element: { id: "ax-2" }, action_route: {
        status: "PASS", mechanism: "deterministic_input", verification_required: true,
        observation_id: "pointer-before", session_id: "macos-ax-123",
      } };
      if (path === "/computer/actions/open-disclosure/verify") return { verified: true, status: "PASS" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "click_accessibility_disclosure") return { status: "PASS", verified: true, executionAttempted: true, observation: after };
      if (command === "signal_perception") return { controllerId: "controller-1", revision: 1, state: "ACTIVE", reason: "action_trigger", targetFps: 8, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 1, burstAvailable: false };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with pointer" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify", expect.objectContaining({ method: "POST" })));
    expect(invoke).toHaveBeenCalledWith("click_accessibility_disclosure", {
      observationId: "pointer-before", sessionId: "macos-ax-123", applicationPid: 123, elementId: "ax-2", label: "Details",
    });
    expect(invoke).not.toHaveBeenCalledWith("press_accessibility_disclosure", expect.anything());
  });

  it("uses a fresh tracked point only for a frame-less closed AX disclosure and verifies the same-app transition", async () => {
    const now = Date.now() / 1000;
    const targetApplication = { applicationPid: 123, applicationBundleId: "example.target" };
    const before = { observationId: "vision-before", permissionGranted: true, observedAt: now,
      sessionId: "macos-ax-vision", coordinateSpace: "ax_global_screen_coordinates", ...targetApplication,
      windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "ax-vision", role: "AXDisclosureTriangle", label: "Details", frame: null, enabled: true,
        focused: false, selected: null, expanded: false, supportedActions: [], depth: 1 }] };
    const after = { ...before, observationId: "vision-after", observedAt: now + 0.2,
      elements: [{ ...before.elements[0], expanded: true }] };
    const grounded = { status: "GROUNDED" as const,
      candidate: { id: "candidate-1", label: "Details", bbox: [0.1, 0.2, 0.2, 0.3], confidence: 0.9 },
      provenance: { capture_session_id: "capture-vision", frame_sequence: 12, tracked_frame_sequence: 13,
        observed_at: now, image_sha256: "a".repeat(64), width: 640, height: 480,
        target_application: targetApplication,
        display_geometry: { displayId: 2, originX: 0, originY: 0, widthPoints: 1440, heightPoints: 900 } } };
    const alignment = { status: "READY", captureSessionId: "capture-vision", targetApplication,
      seedSequence: 12, verifiedSequence: 13, verifiedAt: now, displayId: 2,
      trackingSource: "OPENCV_LUCAS_KANADE", trackingConfidence: 0.88,
      targetWindowContainsPoint: true, mappedGlobalPoint: [240, 180] };
    const capture = { status: { running: true, captureSessionId: "capture-vision", targetApplication },
      targetApplication, grounding: null, groundTarget: vi.fn(async () => grounded),
      validatePointerAlignment: vi.fn(async () => alignment) };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/open-disclosure/plan") return { plan_id: "vision-plan", selected_element: { id: "ax-vision" }, action_route: {
        status: "PASS", mechanism: "vision_pointer", verification_required: true,
        observation_id: "vision-before", session_id: "macos-ax-vision",
      } };
      if (path === "/computer/actions/open-disclosure/verify") return { verified: true, status: "PASS" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return true;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "click_vision_grounded_disclosure") return { alignment,
        action: { status: "PASS", verified: true, executionAttempted: true, observation: after } };
      if (command === "signal_perception") return { controllerId: "controller-1", revision: 1, state: "ACTIVE",
        reason: "action_trigger", targetFps: 8, captureFpsCeiling: 2, achievableFps: 2,
        computeLevelTarget: 1, burstAvailable: false };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" capture={capture as any} />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Ground and open with tracked pointer" }));

    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify", expect.objectContaining({ method: "POST" })));
    expect(capture.groundTarget).toHaveBeenCalledWith("Details");
    expect(capture.validatePointerAlignment).toHaveBeenCalledWith(grounded);
    expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/plan", expect.objectContaining({
      body: expect.stringContaining('"vision_pointer"'),
    }));
    expect(invoke).toHaveBeenCalledWith("click_vision_grounded_disclosure", {
      captureSessionId: "capture-vision", seedSequence: 12, seedBboxNormalized: [0.1, 0.2, 0.2, 0.3],
      candidateConfidence: 0.9, targetApplication,
      observationId: "vision-before", sessionId: "macos-ax-vision", applicationPid: 123,
      elementId: "ax-vision", label: "Details", candidateLabel: "Details",
    });
    expect(invoke).not.toHaveBeenCalledWith("click_accessibility_disclosure", expect.anything());
    expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"observationId":"vision-after"'),
    }));
  });

  it("executes an approved Find routine through its shortcut and verifies fresh search-field focus", async () => {
    const before = { observationId: "menu-before", permissionGranted: true, observedAt: 100, sessionId: "macos-ax-123",
      coordinateSpace: "ax_global_screen_coordinates", applicationPid: 123, windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "find-1", role: "AXMenuItem", subrole: null, label: "Find…", frame: null, enabled: true,
        focused: false, selected: null, expanded: null, supportedActions: [], keyboardShortcut: {
          keyCode: 3, modifiers: 0, key: "f", provenance: "application_menu",
        }, depth: 1 }] };
    const after = { ...before, observationId: "menu-after", observedAt: 101,
      elements: [{ ...before.elements[0], id: "search-1", role: "AXTextField", subrole: "AXSearchField",
        label: "Search", focused: true, keyboardShortcut: null }] };
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/shortcut/open-find/plan") return {
        plan_id: "shortcut-routine-plan", routine_candidate_id: "routine-find-1",
        decision: { selection_source: "APPROVED_VERIFIED_ROUTINE" }, selected_element: { id: "find-1" },
        action_route: { status: "PASS", mechanism: "keyboard_shortcut", verification_required: true,
          observation_id: "menu-before", session_id: "macos-ax-123" },
      };
      if (path === "/computer/actions/shortcut/open-find/verify") return { verified: true, status: "PASS" };
      return {};
    });
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "activate_accessibility_find_shortcut") return { status: "PASS", verified: true, executionAttempted: true, shortcut: before.elements[0].keyboardShortcut, observation: after };
      if (command === "signal_perception") return { controllerId: "controller-1", revision: 1, state: "ACTIVE", reason: "action_trigger", targetFps: 8, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 1, burstAvailable: false };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with ⌘F" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/verify", expect.objectContaining({ method: "POST" })));
    expect(invoke).toHaveBeenCalledWith("activate_accessibility_find_shortcut", {
      observationId: "menu-before", sessionId: "macos-ax-123", applicationPid: 123, elementId: "find-1", label: "Find…",
    });
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "action_trigger" });
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "expected_effect" });
    expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/plan", expect.objectContaining({ method: "POST" }));
    const verificationCall = vi.mocked(api).mock.calls.find(call => call[0] === "/computer/actions/shortcut/open-find/verify");
    expect(JSON.parse(String(verificationCall?.[1]?.body))).toMatchObject({
      plan_id: "shortcut-routine-plan", observation: { ax: { observationId: "menu-after" } },
      execution_receipt: { source: "native_accessibility", action_kind: "open_find",
        status: "PASS", execution_attempted: true },
    });
  });

  it("requires a separate Execute choice after replanning a failed Find shortcut", async () => {
    const before = { observationId: "find-before-failed", permissionGranted: true, observedAt: 100,
      sessionId: "macos-ax-find-recovery", coordinateSpace: "ax_global_screen_coordinates", applicationPid: 456,
      applicationBundleId: "example.find-target", windowTitle: "Example", truncated: false, uncertainty: [],
      elements: [{ id: "find-menu", role: "AXMenuItem", subrole: null, label: "Find…", frame: null, enabled: true,
        focused: false, selected: null, expanded: null, supportedActions: [], keyboardShortcut: {
          keyCode: 3, modifiers: 0, key: "f", provenance: "application_menu",
        }, depth: 1 }] };
    const afterFailure = { ...before, observationId: "find-after-failed", observedAt: 101 };
    const afterSecondFailure = { ...before, observationId: "find-after-second-failed", observedAt: 102 };
    const afterRecovery = { ...before, observationId: "find-after-recovery", observedAt: 102,
      elements: [{ ...before.elements[0], id: "find-search", role: "AXTextField", subrole: "AXSearchField",
        label: "Search", focused: true, keyboardShortcut: null }] };
    let planCalls = 0;
    let verifyCalls = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/actions/shortcut/open-find/plan") {
        planCalls += 1;
        const recoveryPlan = planCalls > 1;
        const planId = planCalls === 1 ? "find-failed-plan" : `find-recovery-plan-${planCalls - 1}`;
        return { plan_id: planId,
          selected_element: { id: "find-menu" }, action_route: {
            status: "PASS", mechanism: "keyboard_shortcut", verification_required: true,
            observation_id: planCalls === 1 ? "find-before-failed"
              : planCalls === 2 ? "find-after-failed" : "find-after-second-failed",
            session_id: "macos-ax-find-recovery",
          }, ...(!recoveryPlan ? { routine_candidate_id: "find-candidate-failed" } : {}) };
      }
      if (path === "/computer/actions/shortcut/open-find/verify") {
        verifyCalls += 1;
        return verifyCalls < 3
          ? { status: "FAILED", verified: false, verification_state: "NOT_CONFIRMED",
              recovery_offer: true, recovery_question: "Open the Find search field" }
          : { status: "PASS", verified: true, verification_state: "CONFIRMED" };
      }
      return {};
    });
    let dispatches = 0;
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return true;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      if (command === "observe_focused_accessibility") return before;
      if (command === "activate_accessibility_find_shortcut") {
        dispatches += 1;
        return dispatches < 3
          ? { status: "FAILED", verified: false, executionAttempted: true,
              observation: dispatches === 1 ? afterFailure : afterSecondFailure }
          : { status: "PASS", verified: true, executionAttempted: true, observation: afterRecovery };
      }
      if (command === "signal_perception") return { state: "ACTIVE", reason: "action_trigger" };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true,
      value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel projectId="project-1" conversationId="conversation-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Observe focused window" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open with ⌘F" }));

    expect(await screen.findByRole("button", { name: "Replan from latest observation" })).toBeInTheDocument();
    expect(dispatches).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Replan from latest observation" }));
    const execute = await screen.findByRole("button", { name: "Execute replanned action" });
    expect(dispatches).toBe(1);
    expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/plan", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"observationId":"find-after-failed"'),
    }));
    const recoveryPlanCall = vi.mocked(api).mock.calls.filter(call => call[0] === "/computer/actions/shortcut/open-find/plan").at(-1);
    expect(JSON.parse(String(recoveryPlanCall?.[1]?.body))).toMatchObject({ bypass_routine_cache: true });

    fireEvent.click(execute);
    await waitFor(() => expect(dispatches).toBe(2));
    expect(await screen.findByRole("button", { name: "Replan from latest observation" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Replan from latest observation" }));
    const secondExecute = await screen.findByRole("button", { name: "Execute replanned action" });
    const secondRecoveryPlanCall = vi.mocked(api).mock.calls
      .filter(call => call[0] === "/computer/actions/shortcut/open-find/plan").at(-1);
    expect(JSON.parse(String(secondRecoveryPlanCall?.[1]?.body))).toMatchObject({
      observation: { ax: { observationId: "find-after-second-failed" } }, bypass_routine_cache: true,
    });
    expect(dispatches).toBe(2);

    fireEvent.click(secondExecute);
    await waitFor(() => expect(dispatches).toBe(3));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/verify",
      expect.objectContaining({ method: "POST", body: expect.stringContaining('"plan_id":"find-recovery-plan-2"') })));
    const recoveryVerify = vi.mocked(api).mock.calls.filter(call => call[0] === "/computer/actions/shortcut/open-find/verify").at(-1);
    expect(JSON.parse(String(recoveryVerify?.[1]?.body))).toMatchObject({
      plan_id: "find-recovery-plan-2", observation: { ax: { observationId: "find-after-recovery" } },
    });
    expect(await screen.findByLabelText("Focused window observation")).toHaveTextContent("AXTextField Search");
  });

  it("refreshes the unified desktop and managed-browser World State only after a user action", async () => {
    const desktopRecord = { session_id: "macos-ax-123", updated_at: 1, expires_at: 601, state: {
      source: "AX", active_window: { title: "Saved window", application_pid: 123 },
      ui_elements: [{ id: "ax-0", role: "AXButton", label: "Continue", frame: null, enabled: true, focused: false, selected: null, depth: 1 }],
      scene_epoch: 3, coordinate_space: "ax_global_screen_coordinates", perception_state: "HIGH",
      perception_policy: { reason: "pixel_change", target_fps: 20, capture_fps_ceiling: 2, achievable_fps: 2, compute_level_target: 2 },
    } };
    const restored = { schema_version: 1, updated_at: 2,
      desktop: { status: "AVAILABLE", record: desktopRecord },
      browser_sessions: [{ session_id: "playwright-123", url: "https://example.test/results",
        last_activity: 2, status: "READY", source: "managed_playwright_session" }] };
    vi.mocked(api).mockResolvedValueOnce(restored as any);
    vi.mocked(api).mockResolvedValueOnce({ schema_version: 1, updated_at: 3,
      desktop: { status: "EMPTY", record: null }, browser_sessions: [] } as any);
    const invoke = vi.fn(async (command: string) => {
      if (command === "accessibility_permission_status") return false;
      if (command === "pointer_event_permission_status") return false;
      if (command === "accessibility_monitor_status") return { active: false, applicationPid: null };
      throw new Error(`Unexpected command ${command}`);
    });
    originalDescriptor = Object.getOwnPropertyDescriptor(window, "__TAURI__");
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });

    render(<AccessibilityPanel />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("accessibility_monitor_status"));
    expect(api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh unified World State" }));
    await waitFor(() => expect(screen.getByLabelText("Unified world state")).toHaveTextContent("Saved window"));
    expect(screen.getByLabelText("Unified world state")).toHaveTextContent("Advisory perception policy: HIGH · target 20 fps / capture ceiling 2 fps");
    expect(screen.getByLabelText("Unified world state")).toHaveTextContent("Managed Playwright sessions: 1");
    expect(screen.getByLabelText("Unified world state")).toHaveTextContent("https://example.test/results");
    expect(api).toHaveBeenCalledWith("/world-state");

    fireEvent.click(screen.getByRole("button", { name: "Refresh unified World State" }));
    await waitFor(() => expect(screen.getByLabelText("Unified world state")).toHaveTextContent("No unexpired desktop state."));
    expect(screen.getByLabelText("Unified world state")).toHaveTextContent("Managed Playwright sessions: 0");
  });
});
