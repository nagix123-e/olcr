import { beforeEach, describe, expect, it, vi } from "vitest";
import { executeChatComputerUseAction, parseChatComputerUseAction, parseChatComputerUseStartAction } from "./chatComputerUseAction";

vi.mock("./perception", () => ({ persistPerceptionPolicy: vi.fn().mockResolvedValue(true) }));

const target = { applicationPid: 42, applicationBundleId: "com.example.demo" };
const before = {
  source: "AX", permissionGranted: true, observationId: "before-1", observedAt: 100,
  sessionId: "ax-session-1", applicationPid: target.applicationPid,
  applicationBundleId: target.applicationBundleId, elements: [],
};
const after = { ...before, observationId: "after-1", observedAt: 101 };

const makeHarness = (routeMechanism = "accessibility", actionKind = "open_disclosure") => {
  let goalPlanCalls = 0;
  const invoke = vi.fn(async (command: string) => {
    if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
    if (command === "observe_accessibility_application") return before;
    if (command === "pointer_event_permission_status") return false;
    if (command === "press_accessibility_disclosure" || command === "click_accessibility_disclosure"
        || command === "activate_accessibility_find_shortcut" || command === "press_accessibility_button"
        || command === "press_accessibility_tab" || command === "press_accessibility_key") {
      return { status: "PASS", verified: command !== "press_accessibility_key", executionAttempted: true,
        key: "Space", observation: after };
    }
    if (command === "scroll_accessibility_area") {
      return { status: "PASS", executionAttempted: true, direction: "down", observation: after };
    }
    if (command === "enter_accessibility_text") {
      return { status: "PASS", verified: true, executionAttempted: true, valueMatched: true, observation: after };
    }
    return { controllerId: "apc-1", revision: 1, state: "ACTIVE", reason: "test", targetFps: 5,
      captureFpsCeiling: 15, achievableFps: 5, computeLevelTarget: 1, burstAvailable: true };
  });
  const api = vi.fn(async (path: string, _init?: RequestInit) => {
    if (path.endsWith("/plan")) {
      const body = JSON.parse(String(_init?.body || "{}"));
      if (actionKind === "press_button" && !body.target_label) {
        goalPlanCalls += 1;
        if (goalPlanCalls > 1) return { status: "COMPLETE", verified: true,
          completion_text: "Game started", message: "Goal complete: Game started", conversation_id: "conversation-1" };
      }
      return { status: "PASS", plan_id: `plan-${goalPlanCalls || 1}`,
      selected_element: { id: actionKind === "open_find" ? "ax-menu-find-1"
        : actionKind === "press_button" ? "ax-button-start" : actionKind === "type_text" ? "ax-text-search"
        : actionKind === "press_tab" ? "ax-button-continue" : actionKind === "press_key" ? "ax-window-game"
          : actionKind === "scroll_wheel" ? "ax-area-results" : "ax-disclosure-1",
        label: actionKind === "open_find" ? "Find" : actionKind === "press_button" ? "Start" : actionKind === "type_text" ? "Search"
          : actionKind === "press_tab" ? "Continue" : actionKind === "press_key" ? "Game"
            : actionKind === "scroll_wheel" ? "Results" : "General",
        ...(actionKind === "type_text" ? { role: "AXTextField" } : {}),
        ...(actionKind === "scroll_wheel" ? { role: "AXScrollArea", scrollbar_id: "ax-scrollbar-results" } : {}) },
      ...(actionKind === "scroll_wheel" ? { scroll_value: 0.4 } : {}),
      ...(actionKind === "press_tab" ? { current_focus_element: { id: "ax-text-name", label: "Name" } } : {}),
      action_route: { status: "PASS", verification_required: true, observation_id: before.observationId,
        session_id: before.sessionId, mechanism: routeMechanism } };
    }
    if (path.endsWith("/verify")) return { status: "PASS", verified: true,
      message: actionKind === "open_find" ? "Opened Find." : actionKind === "press_key"
        ? "Pressed Space; the requested text appeared." : actionKind === "press_tab"
        ? "Moved focus to Continue." : actionKind === "press_button"
        ? "Pressed Start; the requested text appeared." : actionKind === "type_text"
          ? "Entered text in Search; the field value was verified." : actionKind === "scroll_wheel"
            ? "Scrolled Results down and verified the changed position." : "Opened General.", conversation_id: "conversation-1" };
    return {};
  });
  return { bridge: { core: { invoke: invoke as any } }, api, invoke };
};

describe("chat Computer Use disclosure action", () => {
  beforeEach(() => vi.clearAllMocks());

  it("resolves only explicit labelled disclosure requests", () => {
    expect(parseChatComputerUseAction('Open the disclosure "General"')).toEqual({
      kind: "open_disclosure", question: 'Open the disclosure "General"',
    });
    expect(parseChatComputerUseAction("Open the General settings section")).toEqual({
      kind: "open_disclosure", question: "Open the General settings section",
    });
    expect(parseChatComputerUseAction("「一般」セクションを開いて")).toEqual({
      kind: "open_disclosure", question: "「一般」セクションを開いて",
    });
    expect(parseChatComputerUseAction("一般設定のセクションを開いて")).toEqual({
      kind: "open_disclosure", question: "一般設定のセクションを開いて",
    });
    expect(parseChatComputerUseAction("scrape https://example.test/General")).toBeNull();
    expect(parseChatComputerUseAction("run E2E and open the disclosure \"General\"")).toBeNull();
    expect(parseChatComputerUseAction("Open Settings")).toEqual({ kind: "goal", question: "Open Settings" });
    expect(parseChatComputerUseAction("Start the game")).toEqual({ kind: "goal", question: "Start the game" });
    expect(parseChatComputerUseAction("ゲームを開始して")).toEqual({ kind: "goal", question: "ゲームを開始して" });
    expect(parseChatComputerUseAction("Click the button labeled ‘Start tracking demo’ and verify that the screen shows ‘Started — tracking verified’.")).toEqual({
      kind: "press_button",
      question: "Click the button labeled ‘Start tracking demo’ and verify that the screen shows ‘Started — tracking verified’.",
      targetLabel: "Start tracking demo",
      expectedText: "Started — tracking verified",
    });
    expect(parseChatComputerUseAction("open the Find search field")).toEqual({
      kind: "open_find", question: "open the Find search field",
    });
    expect(parseChatComputerUseAction("検索欄を開いて")).toEqual({
      kind: "open_find", question: "検索欄を開いて",
    });
    expect(parseChatComputerUseAction('Type "red fox" into the "Search" field.')).toEqual({
      kind: "type_text", question: 'Type "red fox" into the "Search" field.',
      targetLabel: "Search", inputText: "red fox",
    });
    expect(parseChatComputerUseAction('Type "OLCR-E2E-INPUT-9c50" into "E2E note”.')).toEqual({
      kind: "type_text", question: 'Type "OLCR-E2E-INPUT-9c50" into "E2E note”.',
      targetLabel: "E2E note", inputText: "OLCR-E2E-INPUT-9c50",
    });
    expect(parseChatComputerUseAction("「青い鳥」を「検索」欄に入力して")).toEqual({
      kind: "type_text", question: "「青い鳥」を「検索」欄に入力して",
      targetLabel: "検索", inputText: "青い鳥",
    });
    expect(parseChatComputerUseAction("「検索」欄に「青い鳥」と入力して")).toEqual({
      kind: "type_text", question: "「検索」欄に「青い鳥」と入力して",
      targetLabel: "検索", inputText: "青い鳥",
    });
    expect(parseChatComputerUseAction("「E2E note」欄に「OLCR-E2E-INPUT-9c50」と入力して。")).toEqual({
      kind: "type_text", question: "「E2E note」欄に「OLCR-E2E-INPUT-9c50」と入力して。",
      targetLabel: "E2E note", inputText: "OLCR-E2E-INPUT-9c50",
    });
    expect(parseChatComputerUseAction('Fill the "Search" field with "red fox".')).toEqual({
      kind: "type_text", question: 'Fill the "Search" field with "red fox".',
      targetLabel: "Search", inputText: "red fox",
    });
    expect(parseChatComputerUseAction('Press Space and verify "Started".')).toEqual({
      kind: "press_key", question: 'Press Space and verify "Started".', key: "Space", expectedText: "Started",
    });
    expect(parseChatComputerUseAction('ArrowRightキーを押して「次の画面」が表示されたことを確認して')).toEqual({
      kind: "press_key", question: 'ArrowRightキーを押して「次の画面」が表示されたことを確認して',
      key: "ArrowRight", expectedText: "次の画面",
    });
    expect(parseChatComputerUseAction('press Delete and verify "Deleted"')).toBeNull();
    expect(parseChatComputerUseAction('Press Tab to move focus to "Continue" and verify it receives focus.')).toEqual({
      kind: "press_tab", question: 'Press Tab to move focus to "Continue" and verify it receives focus.',
      targetLabel: "Continue",
    });
    expect(parseChatComputerUseAction("Tabキーを押して「続行」にフォーカスが移ったことを確認して")).toEqual({
      kind: "press_tab", question: "Tabキーを押して「続行」にフォーカスが移ったことを確認して",
      targetLabel: "続行",
    });
    expect(parseChatComputerUseAction('Scroll down in the "Results" area.')).toEqual({
      kind: "scroll_wheel", question: 'Scroll down in the "Results" area.',
      targetLabel: "Results", direction: "down",
    });
    expect(parseChatComputerUseAction("「結果」領域を上にスクロールして")).toEqual({
      kind: "scroll_wheel", question: "「結果」領域を上にスクロールして",
      targetLabel: "結果", direction: "up",
    });
    expect(parseChatComputerUseAction('Type "red fox" into Search.')).toBeNull();
    expect(parseChatComputerUseAction("Enter my password in the Password field")).toBeNull();
    expect(parseChatComputerUseAction('click "Start" button and verify "Started"')).toEqual({
      kind: "press_button", question: 'click "Start" button and verify "Started"',
      targetLabel: "Start", expectedText: "Started",
    });
    expect(parseChatComputerUseAction('Click the button labeled “Start tracking demo” and verify that the visible status changes to “Started — tracking verified”.')).toEqual({
      kind: "press_button",
      question: 'Click the button labeled “Start tracking demo” and verify that the visible status changes to “Started — tracking verified”.',
      targetLabel: "Start tracking demo", expectedText: "Started — tracking verified",
    });
    expect(parseChatComputerUseAction("Click the button labeled ‘Start tracking demo’ and verify that the visible status changes to ‘Started — tracking verified’.")).toEqual({
      kind: "press_button",
      question: "Click the button labeled ‘Start tracking demo’ and verify that the visible status changes to ‘Started — tracking verified’.",
      targetLabel: "Start tracking demo", expectedText: "Started — tracking verified",
    });
    expect(parseChatComputerUseAction("「開始」ボタンをクリックして「ゲーム開始」を確認して")).toEqual({
      kind: "press_button", question: "「開始」ボタンをクリックして「ゲーム開始」を確認して",
      targetLabel: "開始", expectedText: "ゲーム開始",
    });
    expect(parseChatComputerUseAction("Start the game and verify ‘Game started’.")).toEqual({
      kind: "press_button", question: "Start the game and verify ‘Game started’.",
      expectedText: "Game started",
    });
    expect(parseChatComputerUseAction("ゲームを開始して「ゲーム開始」が表示されることを確認して")).toEqual({
      kind: "press_button", question: "ゲームを開始して「ゲーム開始」が表示されることを確認して",
      expectedText: "ゲーム開始",
    });
    expect(parseChatComputerUseAction("「削除」ボタンを押して" )).toBeNull();
    expect(parseChatComputerUseAction('click "Open" and verify "Dialog open"; then click "Continue" and verify "Complete"')).toEqual({
      kind: "sequence", question: 'click "Open" and verify "Dialog open"; then click "Continue" and verify "Complete"',
      steps: [
        { kind: "press_button", question: 'click "Open" and verify "Dialog open"', targetLabel: "Open", expectedText: "Dialog open" },
        { kind: "press_button", question: 'click "Continue" and verify "Complete"', targetLabel: "Continue", expectedText: "Complete" },
      ],
    });
    expect(parseChatComputerUseAction("「開く」をクリックして「画面を開いた」を確認し、次に「続ける」を押して「完了」を確認する")).toMatchObject({
      kind: "sequence", steps: [
        { kind: "press_button", targetLabel: "開く", expectedText: "画面を開いた" },
        { kind: "press_button", targetLabel: "続ける", expectedText: "完了" },
      ],
    });
    expect(parseChatComputerUseAction('click "The then button" and verify "Step one"; then click "Continue" and verify "Done"')).toMatchObject({
      kind: "sequence", steps: [{ targetLabel: "The then button" }, { targetLabel: "Continue" }],
    });
    expect(parseChatComputerUseAction('click "A" and verify "A"; click "B"')).toBeNull();
    expect(parseChatComputerUseAction('click "A" and verify "A"; click "B" and verify "B"; click "C" and verify "C"; click "D" and verify "D"; click "E" and verify "E"')).toBeNull();
  });

  it("splits an authorized native session start from its same-message action", () => {
    expect(parseChatComputerUseStartAction("Start a real GUI E2E with Computer Use for Calculator, then Start the game and verify ‘Game started’."))
      .toEqual({ target: "Calculator", action: { kind: "press_button",
        question: "Start the game and verify ‘Game started’.", expectedText: "Game started" } });
    expect(parseChatComputerUseStartAction("computer use ONの状態で実E2Eを開始して、ゲームを開始して「ゲーム開始」が表示されることを確認して"))
      .toEqual({ target: null, action: { kind: "press_button",
        question: "ゲームを開始して「ゲーム開始」が表示されることを確認して", expectedText: "ゲーム開始" } });
    expect(parseChatComputerUseStartAction("Start Playwright E2E at http://127.0.0.1:5173, then start the game"))
      .toBeNull();
    expect(parseChatComputerUseStartAction('Start a real GUI E2E with Computer Use for Calculator, then click "Open" and verify "Opened"; then click "Continue" and verify "Done".'))
      .toMatchObject({ target: "Calculator", action: { kind: "sequence", steps: [
        { targetLabel: "Open", expectedText: "Opened" }, { targetLabel: "Continue", expectedText: "Done" },
      ] } });
    expect(parseChatComputerUseStartAction('Start a real GUI E2E with Computer Use for Calculator, then click "Open" and verify "Opened". The control moves during capture; use grounded visual tracking when AXPress is unavailable.'))
      .toEqual({ target: "Calculator", action: { kind: "press_button",
        question: 'click "Open" and verify "Opened".', targetLabel: "Open", expectedText: "Opened" } });
  });

  it("uses the selected active AX target and requires verified post-action state", async () => {
    const { bridge, api, invoke } = makeHarness();
    const outcome = await executeChatComputerUseAction({ action: { kind: "open_disclosure", question: 'Open the disclosure "General"' },
      enabled: true, projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(outcome).toEqual({ message: "Opened General.", conversationId: "conversation-1" });
    expect(invoke).toHaveBeenCalledWith("press_accessibility_disclosure", {
      observationId: before.observationId, sessionId: before.sessionId, applicationPid: target.applicationPid,
      elementId: "ax-disclosure-1", label: "General",
    });
    expect(api).toHaveBeenCalledWith("/computer/actions/open-disclosure/verify", expect.objectContaining({ method: "POST" }));
    expect(api).toHaveBeenCalledWith("/desktop/world-state", expect.objectContaining({ method: "PUT" }));
    expect(api.mock.calls.filter(call => call[0] === "/desktop/world-state")).toHaveLength(2);
  });

  it("routes Find from chat through the menu-grounded shortcut and fresh AX verification", async () => {
    const { bridge, api, invoke } = makeHarness("keyboard_shortcut", "open_find");
    const outcome = await executeChatComputerUseAction({ action: { kind: "open_find", question: "open Find" },
      enabled: true, projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(outcome).toEqual({ message: "Opened Find.", conversationId: "conversation-1" });
    expect(invoke).toHaveBeenCalledWith("observe_accessibility_application", { applicationPid: target.applicationPid, includeMenuBar: true });
    expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/plan", expect.anything());
    expect(invoke).toHaveBeenCalledWith("activate_accessibility_find_shortcut", {
      observationId: before.observationId, sessionId: before.sessionId, applicationPid: target.applicationPid,
      elementId: "ax-menu-find-1", label: "Find",
    });
    expect(api).toHaveBeenCalledWith("/computer/actions/shortcut/open-find/verify", expect.objectContaining({ method: "POST" }));
  });

  it("presses only the exact AX button label and requires the requested new text", async () => {
    const { bridge, api, invoke } = makeHarness("accessibility", "press_button");
    const action = { kind: "press_button" as const, question: 'click "Start" button and verify "Started"',
      targetLabel: "Start", expectedText: "Started" };
    const outcome = await executeChatComputerUseAction({ action, enabled: true, projectId: "project-1",
      conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(outcome).toEqual({ message: "Pressed Start; the requested text appeared.", conversationId: "conversation-1" });
    expect(api).toHaveBeenCalledWith("/computer/actions/button/plan", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"target_label":"Start"'),
    }));
    expect(invoke).toHaveBeenCalledWith("observe_accessibility_application", { applicationPid: target.applicationPid, includeMenuBar: false });
    expect(invoke).toHaveBeenCalledWith("press_accessibility_button", {
      observationId: before.observationId, sessionId: before.sessionId, applicationPid: target.applicationPid,
      elementId: "ax-button-start", label: "Start",
    });
    expect(api).toHaveBeenCalledWith("/computer/actions/button/verify", expect.objectContaining({ method: "POST" }));
  });

  it("routes explicitly quoted text to the exact chat-planned AX field without echoing it", async () => {
    const { bridge, api, invoke } = makeHarness("accessibility", "type_text");
    const action = parseChatComputerUseAction('Type "red fox" into the "Search" field.');
    expect(action).toEqual({ kind: "type_text", question: 'Type "red fox" into the "Search" field.',
      targetLabel: "Search", inputText: "red fox" });
    const result = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(result).toEqual({ message: "Entered text in Search; the field value was verified.",
      conversationId: "conversation-1" });
    const planCall = api.mock.calls.find(call => call[0] === "/computer/actions/text/plan");
    expect(JSON.parse(String(planCall?.[1]?.body))).toMatchObject({
      target_label: "Search", input_text: "red fox",
    });
    expect(invoke).toHaveBeenCalledWith("enter_accessibility_text", {
      observationId: before.observationId, sessionId: before.sessionId,
      applicationPid: target.applicationPid, elementId: "ax-text-search", label: "Search",
      inputText: "red fox",
    });
    const verifyCall = api.mock.calls.find(call => call[0] === "/computer/actions/text/verify");
    const receipt = JSON.parse(String(verifyCall?.[1]?.body)).execution_receipt;
    expect(receipt).toMatchObject({ action_kind: "type_text", value_matched: true,
      verified: true, element_id: "ax-text-search" });
    expect(JSON.stringify(receipt)).not.toContain("red fox");
  });

  it("presses Tab for a planned focus target and verifies the fresh AX observation", async () => {
    const { bridge, api, invoke } = makeHarness("keyboard_shortcut", "press_tab");
    const action = parseChatComputerUseAction('Press Tab to move focus to "Continue" and verify it receives focus.');
    expect(action).toEqual({ kind: "press_tab",
      question: 'Press Tab to move focus to "Continue" and verify it receives focus.', targetLabel: "Continue" });
    const result = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api,
      recordUserMessage: false });
    expect(result.message).toBe("Moved focus to Continue.");
    expect(invoke).toHaveBeenCalledWith("press_accessibility_tab", expect.objectContaining({
      currentFocusElementId: "ax-text-name", targetElementId: "ax-button-continue", targetLabel: "Continue",
    }));
    const verification = api.mock.calls.find(([path]) => String(path).endsWith("/verify"));
    expect(verification?.[0]).toBe("/computer/actions/keyboard/tab/verify");
    expect(JSON.parse(String(verification?.[1]?.body)).execution_receipt.action_kind).toBe("press_tab");
  });

  it("dispatches an allowlisted key through the planned focused control and verifies its postcondition", async () => {
    const { bridge, api, invoke } = makeHarness("deterministic_input", "press_key");
    const action = parseChatComputerUseAction('Press Space and verify "Started".');
    expect(action).toEqual({ kind: "press_key", question: 'Press Space and verify "Started".',
      key: "Space", expectedText: "Started" });
    const result = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api });
    expect(result.message).toBe("Pressed Space; the requested text appeared.");
    expect(invoke).toHaveBeenCalledWith("press_accessibility_key", {
      observationId: before.observationId, sessionId: before.sessionId,
      applicationPid: target.applicationPid, elementId: "ax-window-game", label: "Game", key: "Space",
    });
    const planCall = api.mock.calls.find(call => call[0] === "/computer/actions/keyboard/key/plan");
    expect(JSON.parse(String(planCall?.[1]?.body))).toMatchObject({ key: "Space", expected_text: "Started" });
    const verifyCall = api.mock.calls.find(call => call[0] === "/computer/actions/keyboard/key/verify");
    expect(JSON.parse(String(verifyCall?.[1]?.body)).execution_receipt).toMatchObject({
      action_kind: "press_key", action: "CGEventKeyboard", key: "Space", element_id: "ax-window-game",
    });
  });

  it("scrolls only the chat-planned AX area and submits a bound wheel receipt for verification", async () => {
    const { bridge, api, invoke } = makeHarness("deterministic_input", "scroll_wheel");
    const action = parseChatComputerUseAction('Scroll down in the "Results" area.');
    const result = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(result.message).toBe("Scrolled Results down and verified the changed position.");
    expect(api).toHaveBeenCalledWith("/computer/actions/scroll/plan", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"target_label":"Results"'),
    }));
    expect(invoke).toHaveBeenCalledWith("scroll_accessibility_area", expect.objectContaining({
      observationId: before.observationId, sessionId: before.sessionId,
      applicationPid: target.applicationPid, areaElementId: "ax-area-results",
      areaLabel: "Results", scrollbarElementId: "ax-scrollbar-results",
      beforeScrollValue: 0.4, direction: "down",
    }));
    const verifyCall = api.mock.calls.find(([path]) => path === "/computer/actions/scroll/verify");
    expect(JSON.parse(String(verifyCall?.[1]?.body)).execution_receipt).toMatchObject({
      action_kind: "scroll_wheel", action: "CGEventScrollWheel", direction: "down",
      element_id: "ax-area-results", scrollbar_element_id: "ax-scrollbar-results",
    });
  });

  it("lets the semantic planner resolve the button from the outcome when the user gives no label", async () => {
    const { bridge, api, invoke } = makeHarness("accessibility", "press_button");
    const action = parseChatComputerUseAction("Start the game and verify ‘Game started’.");
    expect(action).toEqual({ kind: "press_button", question: "Start the game and verify ‘Game started’.",
      expectedText: "Game started" });
    const outcome = await executeChatComputerUseAction({ action: action!, enabled: true, projectId: "project-1",
      conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(outcome).toEqual({ message: "Pressed Start; the requested text appeared.", conversationId: "conversation-1" });
    const planCall = api.mock.calls.find(call => call[0] === "/computer/actions/button/plan");
    expect(planCall).toBeDefined();
    const body = JSON.parse(String(planCall?.[1]?.body));
    expect(body).not.toHaveProperty("target_label");
    expect(body.expected_text).toBe("Game started");
    expect(invoke).toHaveBeenCalledWith("press_accessibility_button", expect.objectContaining({
      elementId: "ax-button-start", label: "Start",
    }));
  });

  it("returns a planner clarification for an outcome-only request without reporting a route mismatch", async () => {
    const action = parseChatComputerUseAction('Start the game and verify "Game started"');
    expect(action).toMatchObject({ kind: "press_button", expectedText: "Game started" });
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
      if (command === "observe_accessibility_application") return before;
      if (command === "pointer_event_permission_status") return false;
      throw new Error(`unexpected action dispatch: ${command}`);
    });
    const api = vi.fn(async (path: string) => path.endsWith("/plan")
      ? { status: "CLARIFY", clarification: "Which visible action should I take?", conversation_id: "conversation-1" }
      : {});

    const outcome = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target,
      bridge: { core: { invoke: invoke as any } }, api });

    expect(outcome).toEqual({ message: "Which visible action should I take?",
      conversationId: "conversation-1", clarification: true });
    expect(invoke).not.toHaveBeenCalledWith("press_accessibility_button", expect.anything());
  });

  it("accepts verified completion for an outcome-only request when the expected text is already visible", async () => {
    const action = parseChatComputerUseAction('Start the game and verify "Game started"');
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
      if (command === "observe_accessibility_application") return before;
      if (command === "pointer_event_permission_status") return false;
      throw new Error(`unexpected action dispatch: ${command}`);
    });
    const api = vi.fn(async (path: string) => path.endsWith("/plan")
      ? { status: "COMPLETE", verified: true, completion_text: "Game started",
        message: "The game is already started.", conversation_id: "conversation-1" }
      : {});

    const outcome = await executeChatComputerUseAction({ action: action!, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target,
      bridge: { core: { invoke: invoke as any } }, api });

    expect(outcome).toEqual({ message: "The game is already started.",
      conversationId: "conversation-1", goalComplete: true });
    expect(invoke).not.toHaveBeenCalledWith("press_accessibility_button", expect.anything());
  });

  it("replans from fresh evidence and stops when the backend proves a plain goal complete", async () => {
    const { bridge, api, invoke } = makeHarness("accessibility", "press_button");
    const action = parseChatComputerUseAction("Start the game");
    expect(action).toEqual({ kind: "goal", question: "Start the game" });
    const outcome = await executeChatComputerUseAction({ action: action!, enabled: true, projectId: "project-1",
      conversationId: "conversation-1", selectedTarget: target, bridge, api });

    expect(outcome).toEqual({ message: "Pressed Start; the requested text appeared.\nGoal complete: Game started",
      conversationId: "conversation-1" });
    const planCalls = api.mock.calls.filter(call => call[0] === "/computer/actions/button/plan");
    expect(planCalls).toHaveLength(2);
    const planCall = planCalls[0];
    const body = JSON.parse(String(planCall?.[1]?.body));
    expect(body.question).toBe("Start the game");
    expect(body).not.toHaveProperty("target_label");
    expect(body).not.toHaveProperty("expected_text");
    expect(JSON.parse(String(planCalls[1]?.[1]?.body))).toMatchObject({
      goal_step_index: 1, verified_effects: ["Pressed Start; the requested text appeared."],
    });
    expect(invoke).toHaveBeenCalledWith("press_accessibility_button", expect.objectContaining({
      elementId: "ax-button-start", label: "Start",
    }));
  });

  it("can reuse the initiating chat message without recording the action clause twice", async () => {
    const { bridge, api } = makeHarness("accessibility", "press_button");
    await executeChatComputerUseAction({ action: { kind: "goal", question: "Start the game" }, enabled: true,
      projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api,
      recordUserMessage: false });
    const planCall = api.mock.calls.find(call => call[0] === "/computer/actions/button/plan");
    expect(JSON.parse(String(planCall?.[1]?.body))).toMatchObject({ record_user_message: false });
  });

  it("replans each native goal step from a new AX observation and caps the run at verified actions", async () => {
    let observationNumber = 0;
    let dispatchNumber = 0;
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
      if (command === "observe_accessibility_application") {
        observationNumber += 1;
        return { ...before, observationId: `before-${observationNumber}` };
      }
      if (command === "pointer_event_permission_status") return false;
      if (command === "press_accessibility_button") {
        dispatchNumber += 1;
        return { status: "PASS", executionAttempted: true,
          observation: { ...after, observationId: `after-${dispatchNumber}` } };
      }
      return {};
    });
    let planNumber = 0;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith("/plan")) {
        const body = JSON.parse(String(init?.body || "{}"));
        planNumber += 1;
        if (planNumber === 3) return { status: "COMPLETE", verified: true,
          completion_text: "Game ready", message: "Goal complete: Game ready", conversation_id: "conversation-1" };
        return { status: "PASS", plan_id: `plan-${planNumber}`,
          selected_element: { id: `button-${planNumber}`, label: planNumber === 1 ? "Continue" : "Start" },
          action_route: { status: "PASS", verification_required: true,
            observation_id: body.observation.ax.observationId, session_id: before.sessionId,
            mechanism: "accessibility" } };
      }
      if (path.endsWith("/verify")) {
        return { status: "PASS", verified: true, message: `Step ${dispatchNumber} verified`,
          conversation_id: "conversation-1" };
      }
      return {};
    });
    const onStepProgress = vi.fn();
    const outcome = await executeChatComputerUseAction({ action: { kind: "goal", question: "Open the game and start a new round" },
      enabled: true, projectId: "project-1", conversationId: "conversation-1", selectedTarget: target,
      bridge: { core: { invoke: invoke as any } }, api, onStepProgress });

    expect(outcome).toEqual({ message: "Step 1 verified\nStep 2 verified\nGoal complete: Game ready",
      conversationId: "conversation-1" });
    expect(dispatchNumber).toBe(2);
    const plans = api.mock.calls.filter(([path]) => path.endsWith("/plan"));
    expect(plans.map(([, init]) => JSON.parse(String(init?.body)).goal_step_index)).toEqual([0, 1, 2]);
    expect(JSON.parse(String(plans[1][1]?.body)).verified_effects).toEqual(["Step 1 verified"]);
    expect(JSON.parse(String(plans[2][1]?.body)).verified_effects).toEqual(["Step 1 verified", "Step 2 verified"]);
    expect(onStepProgress).toHaveBeenCalledTimes(3);
  });

  it("re-observes and verifies every sequence step before proceeding", async () => {
    let current = { ...before };
    let pressCount = 0;
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
      if (command === "observe_accessibility_application") return current;
      if (command === "pointer_event_permission_status") return true;
      if (command === "press_accessibility_button") {
        pressCount += 1;
        current = { ...after, observationId: `after-${pressCount}`, observedAt: 101 + pressCount };
        return { status: "PASS", executionAttempted: true, observation: current };
      }
      return { controllerId: "apc-1", revision: pressCount, state: "ACTIVE", reason: "test", targetFps: 5,
        captureFpsCeiling: 15, achievableFps: 5, computeLevelTarget: 1, burstAvailable: true };
    });
    let planCount = 0;
    let verifyCount = 0;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith("/plan")) {
        planCount += 1;
        const body = JSON.parse(String(init?.body));
        const label = body.target_label;
        return { status: "PASS", plan_id: `plan-${planCount}`,
          selected_element: { id: `ax-${label}`, label },
          action_route: { status: "PASS", verification_required: true,
            observation_id: current.observationId, session_id: current.sessionId, mechanism: "accessibility" } };
      }
      if (path.endsWith("/verify")) {
        verifyCount += 1;
        return { status: "PASS", verified: true, message: `Verified step ${verifyCount}`, conversation_id: "conversation-next" };
      }
      return {};
    });
    const action = parseChatComputerUseAction('click "Open" and verify "Opened"; then click "Continue" and verify "Complete"');
    expect(action?.kind).toBe("sequence");
    const onStepProgress = vi.fn();
    const outcome = await executeChatComputerUseAction({ action: action!, enabled: true, projectId: "project-1",
      conversationId: "conversation-1", selectedTarget: target, bridge: { core: { invoke: invoke as any } }, api,
      onStepProgress });

    expect(outcome).toEqual({ message: "Verified step 1\nVerified step 2", conversationId: "conversation-next" });
    expect(pressCount).toBe(2);
    expect(planCount).toBe(2);
    expect(verifyCount).toBe(2);
    expect(onStepProgress.mock.calls).toEqual([[1, 2], [2, 2]]);
    const plans = api.mock.calls.filter(call => call[0].endsWith("/plan"));
    expect(JSON.parse(String(plans[0]?.[1]?.body)).observation.ax.observationId).toBe("before-1");
    expect(JSON.parse(String(plans[1]?.[1]?.body)).observation.ax.observationId).toBe("after-1");
  });

  it("stops the sequence at the first unverified effect", async () => {
    const { bridge, invoke } = makeHarness("accessibility", "press_button");
    let verifyCount = 0;
    const api = vi.fn(async (path: string) => {
      if (path.endsWith("/plan")) return { status: "PASS", plan_id: "plan-1",
        selected_element: { id: "ax-button-start", label: "Start" },
        action_route: { status: "PASS", verification_required: true, observation_id: before.observationId,
          session_id: before.sessionId, mechanism: "accessibility" } };
      if (path.endsWith("/verify")) {
        verifyCount += 1;
        return verifyCount === 1
          ? { status: "PASS", verified: true, message: "First effect verified." }
          : { status: "FAILED", verified: false, reason: "EXPECTED_TEXT_NOT_FOUND" };
      }
      return {};
    });
    const action = { kind: "sequence" as const, question: "three explicit actions", steps: [1, 2, 3].map(index => ({
      kind: "press_button" as const, question: `click Start ${index}`, targetLabel: "Start", expectedText: `Step ${index}`,
    })) };

    await expect(executeChatComputerUseAction({ action, enabled: true, projectId: "project-1",
      conversationId: "conversation-1", selectedTarget: target, bridge, api }))
      .rejects.toThrow("COMPUTER_USE_SEQUENCE_STEP_FAILED:2/3:verified_steps=1:COMPUTER_USE_EFFECT_NOT_VERIFIED:EXPECTED_TEXT_NOT_FOUND");
    expect(verifyCount).toBe(2);
    expect(invoke.mock.calls.filter(call => call[0] === "press_accessibility_button")).toHaveLength(2);
  });

  it("escalates only an AX button without AXPress to UI-Venus plus tracked pointer input", async () => {
    const alignment = { status: "READY", captureSessionId: "capture-1", targetApplication: target,
      seedSequence: 8, verifiedSequence: 9, verifiedAt: 100, displayId: 1,
      trackingSource: "OPENCV_LUCAS_KANADE", trackingConfidence: 0.9,
      targetWindowContainsPoint: true, mappedGlobalPoint: [120, 80] };
    const grounding = { candidate: { label: "Start", bbox: [0.05, 0.05, 0.15, 0.15], confidence: 0.9 },
      provenance: { capture_session_id: "capture-1", frame_sequence: 8, observed_at: 100,
        image_sha256: "a".repeat(64), width: 1440, height: 900,
        target_application: target, display_geometry: { displayId: 1, originX: 0, originY: 0,
          widthPoints: 1440, heightPoints: 900 }, tracked_frame_sequence: 9 }, alignment };
    const refreshedBefore = { ...before, observationId: "before-after-grounding", observedAt: 101 };
    let observationCount = 0;
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return { running: true, captureSessionId: "capture-1", targetApplication: target };
      if (command === "observe_accessibility_application") {
        observationCount += 1;
        return observationCount === 1 ? before : refreshedBefore;
      }
      if (command === "pointer_event_permission_status") return true;
      if (command === "click_vision_grounded_button") return { alignment,
        action: { status: "PASS", executionAttempted: true, action: "VISION_GROUNDED_POINTER", observation: after } };
      return { controllerId: "apc-1", revision: 1, state: "ACTIVE", reason: "test", targetFps: 5,
        captureFpsCeiling: 15, achievableFps: 5, computeLevelTarget: 1, burstAvailable: true };
    });
    let planCount = 0;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith("/plan")) {
        planCount += 1;
        const body = JSON.parse(String(init?.body));
        if (planCount === 1) return { status: "VISION_GROUNDING_REQUIRED",
          selected_element: { id: "ax-button-start", label: "Start" }, expected_text_contains: "Started" };
        expect(body.observation.ax.observationId).toBe("before-after-grounding");
        expect(body.observation.vision_pointer.candidate.label).toBe("Start");
        return { status: "PASS", plan_id: "visual-plan-1",
          selected_element: { id: "ax-button-start", label: "Start" },
          action_route: { status: "PASS", verification_required: true, observation_id: refreshedBefore.observationId,
            session_id: refreshedBefore.sessionId, mechanism: "vision_pointer" } };
      }
      if (path.endsWith("/verify")) return { status: "PASS", verified: true,
        message: "Started appeared after the tracked click." };
      return {};
    });
    const groundVisualTarget = vi.fn(async () => grounding as any);
    const outcome = await executeChatComputerUseAction({
      action: { kind: "press_button", question: 'click "Start" and verify "Started"',
        targetLabel: "Start", expectedText: "Started" },
      enabled: true, projectId: "project-1", conversationId: "conversation-1",
      selectedTarget: target, bridge: { core: { invoke: invoke as any } }, api, groundVisualTarget,
    });

    expect(outcome.message).toBe("Started appeared after the tracked click.");
    expect(observationCount).toBe(2);
    expect(groundVisualTarget).toHaveBeenCalledWith("Start");
    expect(invoke).toHaveBeenCalledWith("click_vision_grounded_button", expect.objectContaining({
      captureSessionId: "capture-1", seedSequence: 8, candidateLabel: "Start",
    }));
    const verify = api.mock.calls.find(call => call[0].endsWith("/verify"));
    const receipt = JSON.parse(String(verify?.[1]?.body)).execution_receipt;
    expect(receipt.action).toBe("VISION_GROUNDED_POINTER");
    expect(receipt.alignment.verifiedSequence).toBe(9);
  });

  it("fails closed when the active capture target differs from Settings scope", async () => {
    const { bridge, api, invoke } = makeHarness();
    await expect(executeChatComputerUseAction({ action: { kind: "open_disclosure", question: "open General" },
      enabled: true, projectId: "project-1", conversationId: "conversation-1",
      selectedTarget: { ...target, applicationPid: 43 }, bridge, api }))
      .rejects.toThrow("COMPUTER_USE_TARGET_IDENTITY_MISMATCH");
    expect(api).not.toHaveBeenCalled();
    expect(invoke).not.toHaveBeenCalledWith("press_accessibility_disclosure", expect.anything());
  });

  it("refuses pointer-vision plans that this chat action does not execute", async () => {
    const { bridge, api, invoke } = makeHarness("vision_pointer");
    await expect(executeChatComputerUseAction({ action: { kind: "open_disclosure", question: "open General" },
      enabled: true, projectId: "project-1", conversationId: "conversation-1", selectedTarget: target, bridge, api }))
      .rejects.toThrow("COMPUTER_USE_ACTION_PLAN_REJECTED");
    expect(invoke).not.toHaveBeenCalledWith("press_accessibility_disclosure", expect.anything());
    expect(invoke).not.toHaveBeenCalledWith("click_accessibility_disclosure", expect.anything());
  });
});

describe("mixed autonomous goals", () => {
  it("chooses input then Enter through existing verified executors before completion", async () => {
    const harness = makeHarness("accessibility", "type_text");
    let nextIndex = 0;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/computer/goals") return { task_id: "goal-1" };
      if (path === "/computer/actions/goal/next") {
        const body = JSON.parse(String(init?.body));
        expect(body.project_id).toBeNull();
        if (nextIndex++ === 0) return { status: "NEXT_ACTION", action: {
          kind: "type_text", question: 'Search for "bird"', targetLabel: "Search", inputText: "bird" } };
        if (nextIndex === 2) return { status: "NEXT_ACTION", action: {
          kind: "press_key", question: "Search", key: "Enter", expectedText: "Results" } };
        expect(body.verified_effects).toHaveLength(2);
        return { status: "COMPLETE", verified: true, completion_text: "Results", message: "Complete" };
      }
      if (path === "/computer/actions/keyboard/key/plan") return { status: "PASS", plan_id: "key-plan",
        selected_element: { id: "window", label: "Search window" },
        action_route: { status: "PASS", verification_required: true, observation_id: before.observationId,
          session_id: before.sessionId, mechanism: "deterministic_input" } };
      return harness.api(path, init);
    });
    const result = await executeChatComputerUseAction({ action: { kind: "goal", question: 'Search for "bird" and confirm results' },
      enabled: true, projectId: null, conversationId: "conversation-1", selectedTarget: target,
      bridge: harness.bridge, api, mixedGoals: true });
    expect(result.message).toContain("Complete");
    expect(harness.invoke.mock.calls.filter(([command]) => command === "enter_accessibility_text")).toHaveLength(1);
    expect(harness.invoke.mock.calls.filter(([command]) => command === "press_accessibility_key")).toHaveLength(1);
    expect(api.mock.calls.filter(([path]) => path.endsWith("/verify"))).toHaveLength(2);
  });

  it("refreshes the screen after slow inference without rerunning the planner", async () => {
    const harness = makeHarness("accessibility", "type_text");
    let plans = 0;
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/computer/goals") return { task_id: "goal-1" };
      if (path === "/computer/actions/goal/next") return ++plans === 1
        ? { status: "REOBSERVE", proposal_id: "proposal-1" }
        : { status: "COMPLETE", verified: true, completion_text: "Results", message: "Complete" };
      if (path === "/computer/actions/goal/validate") {
        const body = JSON.parse(String(init?.body));
        expect(body.proposal_id).toBe("proposal-1");
        expect(body.observation.ax.observationId).toBeTruthy();
        expect(harness.invoke.mock.calls.some(([command]) => command === "enter_accessibility_text")).toBe(false);
        return { status: "NEXT_ACTION", action: {
          kind: "type_text", question: 'Search for "bird"', targetLabel: "Search", inputText: "bird" } };
      }
      return harness.api(path, init);
    });
    const result = await executeChatComputerUseAction({ action: { kind: "goal", question: 'Search for "bird" and confirm results' },
      enabled: true, projectId: null, conversationId: "conversation-1", selectedTarget: target,
      bridge: harness.bridge, api, mixedGoals: true });
    expect(result.message).toContain("Complete");
    expect(plans).toBe(2); // One plan per step, no inference replay for the stale observation.
    expect(harness.invoke.mock.calls.filter(([command]) => command === "enter_accessibility_text")).toHaveLength(1);
  });

  it("ends a stalled planning fetch at the goal deadline", async () => {
    vi.useFakeTimers();
    try {
      const harness = makeHarness("accessibility", "type_text");
      const api = vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/computer/goals") return { task_id: "goal-1" };
        if (path === "/computer/actions/goal/next") return new Promise((_, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("fetch aborted")), { once: true });
        });
        return {};
      });
      const pending = executeChatComputerUseAction({ action: { kind: "goal", question: 'Search for "bird"' },
        enabled: true, projectId: null, conversationId: "conversation-1", selectedTarget: target,
        bridge: harness.bridge, api, mixedGoals: true });
      const rejected = expect(pending).rejects.toThrow("TIME_BUDGET_EXCEEDED");
      for (let n = 0; n < 20; n++) await Promise.resolve();
      expect(api.mock.calls.some(([path]) => path === "/computer/actions/goal/next")).toBe(true);
      await vi.advanceTimersByTimeAsync(300_001);
      await rejected;
      expect(api.mock.calls.some(([path, init]) => path === "/computer/goals/goal-1"
        && JSON.parse(String(init?.body)).state === "failed")).toBe(true);
      expect(harness.invoke.mock.calls.some(([command]) => command === "enter_accessibility_text")).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it("does not dispatch after a cancelled planning request", async () => {
    const harness = makeHarness("accessibility", "type_text");
    const controller = new AbortController();
    const api = async (path: string) => {
      if (path === "/computer/goals") return { task_id: "goal-1" };
      controller.abort(new Error("cancelled"));
      return { status: "NEXT_ACTION", action: { kind: "type_text", question: "Search", targetLabel: "Search", inputText: "bird" } };
    };
    await expect(executeChatComputerUseAction({ action: { kind: "goal", question: 'Search for "bird"' },
      enabled: true, projectId: null, conversationId: "conversation-1", selectedTarget: target,
      bridge: harness.bridge, api, mixedGoals: true, signal: controller.signal })).rejects.toThrow("cancelled");
    expect(harness.invoke.mock.calls.some(([command]) => command === "enter_accessibility_text")).toBe(false);
  });

  it("stops a goal when a verified action is proposed again without progress", async () => {
    const harness = makeHarness("accessibility", "type_text");
    const api = async (path: string, init?: RequestInit) => path === "/computer/goals" ? { task_id: "goal-1" } : path === "/computer/actions/goal/next"
      ? { status: "NEXT_ACTION", action: { kind: "type_text", question: "Search", targetLabel: "Search", inputText: "bird" } }
      : harness.api(path, init);
    await expect(executeChatComputerUseAction({ action: { kind: "goal", question: 'Search for "bird"' },
      enabled: true, projectId: null, conversationId: "conversation-1", selectedTarget: target,
      bridge: harness.bridge, api, mixedGoals: true })).rejects.toThrow("NO_PROGRESS");
    expect(harness.invoke.mock.calls.filter(([command]) => command === "enter_accessibility_text")).toHaveLength(1);
  });
});

it("bootstraps only explicit app-scoped goals and keeps native/browser controls separate", async () => {
  const { parseAppScopedComputerUseGoal } = await import("./chatComputerUseAction");
  expect(parseAppScopedComputerUseGoal('Safariで「青い鳥」を検索して')?.target).toBe("Safari");
  expect(parseAppScopedComputerUseGoal("Reactで状態管理をするときの最近の実装例や使い方を、役に立ちそうな技術記事から3つ探して。タイトル・投稿者・タグ・更新日・内容の要点・記事リンクを教えて。")).toBeNull();
  expect(parseAppScopedComputerUseGoal('OLCR OpenCV Tracking Fixtureで「E2E note」欄に「OLCR-AUTONOMY-RESUME」と入力して')?.action.kind).toBe("type_text");
  expect(parseAppScopedComputerUseGoal('OLCR OpenCV Tracking Fixtureで「Start tracking demo」をクリックして、Started — tracking verified と表示されるまで進めてください。')).toEqual({
    target: "OLCR OpenCV Tracking Fixture",
    action: { kind: "press_button", question: '「Start tracking demo」をクリックして、Started — tracking verified と表示されるまで進めてください。',
      targetLabel: "Start tracking demo", expectedText: "Started — tracking verified" },
  });
  expect(parseAppScopedComputerUseGoal('Using Calculator, open settings')?.target).toBe("Calculator");
  expect(parseAppScopedComputerUseGoal('コンピュータユーズで「検索」欄に「鳥」を入力して')).toBeNull();
  expect(parseAppScopedComputerUseGoal('Safariで https://example.com をスクレイピングして')).toBeNull();
});
