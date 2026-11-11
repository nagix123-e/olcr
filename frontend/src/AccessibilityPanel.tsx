import { useEffect, useRef, useState } from "react";
import { native } from "./native";
import { api } from "./api";
import { persistPerceptionPolicy, type PerceptionSnapshot } from "./perception";
import {useLanguage} from "./language";
import type { useScreenCapture } from "./ScreenCapture";

type Rect = { x: number; y: number; width: number; height: number };
type Shortcut = { keyCode: number; modifiers: number; key: string; provenance: string };
type ElementInfo = { id: string; role: string; subrole: string | null; label: string; frame: Rect | null; enabled: boolean | null; focused: boolean | null; selected: boolean | null; expanded: boolean | null; supportedActions: string[]; keyboardShortcut: Shortcut | null; depth: number };
type Observation = { observationId: string; permissionGranted: boolean; observedAt: number; sessionId: string; coordinateSpace: string; applicationPid: number | null; applicationBundleId?: string | null; windowTitle: string | null; elements: ElementInfo[]; truncated: boolean; uncertainty: string[] };
type RunningApplication = { applicationPid: number; applicationName: string; applicationBundleId: string };
export type CaptureTargetApplication = Pick<RunningApplication, "applicationPid" | "applicationBundleId">;
type MonitorStatus = { active: boolean; applicationPid: number | null };
type ChangeEvent = { source: string; notification: string; applicationPid: number; sessionId: string; observedAt: number };
type RecoveryPlan = { kind: "open_disclosure" | "open_find"; elementId: string; question: string;
  applicationPid: number | null; applicationBundleId?: string | null; bypassRoutineCache?: boolean;
  plan?: any; attempted?: boolean };
type PersistedWorldState = { session_id: string; updated_at: number; expires_at: number; state: { source: string; active_window: { title?: string | null; application_pid?: number | null }; ui_elements: ElementInfo[]; scene_epoch: number; coordinate_space: string; perception_state?: string; perception_policy?: { reason: string; target_fps: number; capture_fps_ceiling: number; achievable_fps: number; compute_level_target: number } } };
type UnifiedWorldState = { schema_version: 1; updated_at: number; desktop: { status: "AVAILABLE" | "EMPTY"; record: PersistedWorldState | null }; browser_sessions: { session_id: string; url: string | null; last_activity: number | null; status: string; source: "managed_playwright_session" }[] };

const verificationFailureMessage = (result: any, fallback: string) => {
  if (result?.verification_state === "INCONCLUSIVE") {
    return "Post-action observation is inconclusive. Observe the focused window again before retrying.";
  }
  if (result?.recovery_offer) {
    return "Expected effect was not confirmed. Review the available replan before choosing whether to execute it.";
  }
  return fallback;
};

export function AccessibilityPanel({ projectId, conversationId, onTargetApplicationChange, targetApplicationLocked = false, capture, view = "diagnostics" }: { projectId?: string; conversationId?: string | null; onTargetApplicationChange?: (application: CaptureTargetApplication | null) => void; targetApplicationLocked?: boolean; capture?: ReturnType<typeof useScreenCapture>; view?: "policy" | "diagnostics" }) {
  const {language}=useLanguage();const ja=language==="ja";
  const policyView = view === "policy";
  const [trusted, setTrusted] = useState(false);
  const [screenCaptureAccess, setScreenCaptureAccess] = useState(false);
  const [pointerAccess, setPointerAccess] = useState(false);
  const [observation, setObservation] = useState<Observation | null>(null);
  const [targetApplications, setTargetApplications] = useState<RunningApplication[]>([]);
  const [selectedTargetPid, setSelectedTargetPid] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [monitor, setMonitor] = useState<MonitorStatus>({ active: false, applicationPid: null });
  const [lastChange, setLastChange] = useState<ChangeEvent | null>(null);
  const [unifiedWorldState, setUnifiedWorldState] = useState<UnifiedWorldState | null>(null);
  const [persistenceMessage, setPersistenceMessage] = useState("");
  const [actionElement, setActionElement] = useState<string | null>(null);
  const [recoveryPlan, setRecoveryPlan] = useState<RecoveryPlan | null>(null);
  const unlisten = useRef<null | (() => void)>(null);
  const monitorPid = useRef<number | null>(null);
  const monitorOwned = useRef(false);
  const refreshTimer = useRef<number | null>(null);

  useEffect(() => {
    const bridge = native();
    if (!bridge) return;
    void bridge.core.invoke<boolean>("accessibility_permission_status")
      .then(setTrusted).catch(reason => setError(reason?.message || String(reason)));
    if (policyView) {
      void bridge.core.invoke<boolean>("screen_capture_permission_status")
        .then(setScreenCaptureAccess).catch(reason => setError(reason?.message || String(reason)));
    }
    void bridge.core.invoke<boolean>("pointer_event_permission_status")
      .then(setPointerAccess).catch(reason => setError(reason?.message || String(reason)));
    if (!policyView) {
      void bridge.core.invoke<MonitorStatus>("accessibility_monitor_status")
        .then(status => { setMonitor(status); monitorPid.current = status.applicationPid; monitorOwned.current = status.active; })
        .catch(reason => setError(reason?.message || String(reason)));
    }
    return () => {
      unlisten.current?.();
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      if (!policyView && monitorOwned.current) void native()?.core.invoke("stop_accessibility_monitoring");
    };
  }, [policyView]);

  const requestPermission = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try {
      const granted = await bridge.core.invoke<boolean>("request_accessibility_permission");
      setTrusted(granted);
      if (!granted) setError("Permission is still off. Enable OLCR in System Settings → Privacy & Security → Accessibility.");
    }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const refreshPermission = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try { setTrusted(await bridge.core.invoke<boolean>("accessibility_permission_status")); }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const requestScreenCaptureAccess = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try {
      const granted = await bridge.core.invoke<boolean>("request_screen_capture_permission");
      setScreenCaptureAccess(granted);
      if (!granted) setError("Screen Recording is still off. Enable OLCR in System Settings → Privacy & Security → Screen & System Audio Recording.");
    }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const refreshScreenCaptureAccess = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try { setScreenCaptureAccess(await bridge.core.invoke<boolean>("screen_capture_permission_status")); }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const requestPointerAccess = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try {
      const granted = await bridge.core.invoke<boolean>("request_pointer_event_permission");
      setPointerAccess(granted);
      if (!granted) setError("Pointer access is still off. Enable OLCR under System Settings → Privacy & Security → Accessibility, then refresh status.");
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const refreshPointerAccess = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try { setPointerAccess(await bridge.core.invoke<boolean>("pointer_event_permission_status")); }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const observe = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError(""); setObservation(null);
    try {
      const next = await bridge.core.invoke<Observation>("observe_focused_accessibility");
      setObservation(next);
      setRecoveryPlan(current => current && current.applicationPid === next.applicationPid
        && current.applicationBundleId === next.applicationBundleId
        ? { ...current, plan: undefined, attempted: false } : null);
      setRecoveryPlan(current => current && current.applicationPid === next.applicationPid
        && current.applicationBundleId === next.applicationBundleId
        ? { ...current, plan: undefined, attempted: false } : null);
      await persistObservation(next);
    }
    catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const refreshTargetApplications = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try {
      const applications = await bridge.core.invoke<RunningApplication[]>("list_running_accessibility_applications");
      setTargetApplications(applications);
      if (selectedTargetPid !== null && !applications.some(application => application.applicationPid === selectedTargetPid)) {
        setSelectedTargetPid(null);
        setObservation(null);
        onTargetApplicationChange?.(null);
      }
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const observeTargetApplication = async () => {
    const bridge = native();
    if (!bridge || selectedTargetPid === null) return;
    setBusy(true); setError(""); setObservation(null);
    try {
      const next = await bridge.core.invoke<Observation>("observe_accessibility_application", { applicationPid: selectedTargetPid });
      const selected = targetApplications.find(application => application.applicationPid === selectedTargetPid);
      if (!selected || next.applicationPid !== selectedTargetPid || next.applicationBundleId !== selected.applicationBundleId) {
        throw new Error("ACCESSIBILITY_TARGET_APPLICATION_IDENTITY_MISMATCH");
      }
      setObservation(next);
      setRecoveryPlan(current => current ? { ...current, plan: undefined, attempted: false } : null);
      await persistObservation(next);
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const replanFromLatestObservation = async () => {
    if (!recoveryPlan || recoveryPlan.attempted || !observation || !projectId || !conversationId
        || recoveryPlan.applicationPid !== observation.applicationPid
        || recoveryPlan.applicationBundleId !== observation.applicationBundleId) return;
    setRecoveryPlan({ ...recoveryPlan, attempted: true });
    setBusy(true); setError("");
    try {
      const disclosure = recoveryPlan.kind === "open_disclosure";
      const plan = await api<any>(disclosure
        ? "/computer/actions/open-disclosure/plan"
        : "/computer/actions/shortcut/open-find/plan", { method: "POST", body: JSON.stringify({
        question: recoveryPlan.question, project_id: projectId, conversation_id: conversationId,
        observation: { ax: observation }, bypass_routine_cache: recoveryPlan.bypassRoutineCache === true,
      }) });
      const route = plan.action_route;
      if (route?.status !== "PASS" || route.observation_id !== observation.observationId
          || route.session_id !== observation.sessionId || plan.selected_element?.id !== recoveryPlan.elementId) {
        throw new Error("RECOVERY_PLAN_OBSERVATION_OR_TARGET_MISMATCH");
      }
      setRecoveryPlan({ ...recoveryPlan, plan });
      setError("A fresh plan is ready. Review it, then choose Execute replanned action to continue.");
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const shouldBypassRoutineCache = (kind: RecoveryPlan["kind"], elementId: string, plan: any) =>
    typeof plan?.routine_candidate_id === "string"
    || (recoveryPlan?.kind === kind && recoveryPlan.elementId === elementId
      && recoveryPlan.plan?.plan_id === plan?.plan_id && recoveryPlan.bypassRoutineCache === true);

  const openDisclosure = async (element: ElementInfo, reviewedPlan?: any) => {
    const bridge = native();
    if (!bridge || !observation || !projectId || !conversationId || observation.applicationPid == null) return;
    setBusy(true); setActionElement(element.id); setError("");
    try {
      let visionPointerEvidence: Record<string, any> | null = null;
      let groundedForPointer = capture?.grounding ?? null;
      const hasUsableAxFrame = Boolean(element.frame
        && [element.frame.x, element.frame.y, element.frame.width, element.frame.height].every(Number.isFinite)
        && element.frame.width > 0 && element.frame.height > 0);
      const hasAxPress = element.supportedActions.includes("AXPress");
      if (!hasUsableAxFrame && !hasAxPress) {
        if (!capture || !pointerAccess || element.role !== "AXDisclosureTriangle") {
          throw new Error("VISION_POINTER_CAPTURE_AND_PERMISSION_REQUIRED");
        }
        const matchesTarget = (value: typeof groundedForPointer) => Boolean(value?.status === "GROUNDED"
          && value.candidate && value.candidate.label.trim().toLocaleLowerCase() === element.label.trim().toLocaleLowerCase()
          && value.provenance.capture_session_id === capture.status.captureSessionId
          && value.provenance.target_application.applicationPid === observation.applicationPid
          && value.provenance.target_application.applicationBundleId === observation.applicationBundleId
          && value.provenance.tracked_frame_sequence != null);
        if (!matchesTarget(groundedForPointer)) groundedForPointer = await capture.groundTarget(element.label);
        if (!matchesTarget(groundedForPointer) || !groundedForPointer?.candidate) {
          throw new Error("VISION_POINTER_GROUNDING_TARGET_MISMATCH");
        }
        const alignment = await capture.validatePointerAlignment(groundedForPointer);
        visionPointerEvidence = { candidate: groundedForPointer.candidate, provenance: groundedForPointer.provenance, alignment };
      }
      const plan = reviewedPlan ?? await api<any>("/computer/actions/open-disclosure/plan", { method: "POST", body: JSON.stringify({
        question: `Open ${element.label}`, project_id: projectId, conversation_id: conversationId,
        observation: { ax: observation, pointer_event_permission_granted: pointerAccess,
          ...(visionPointerEvidence ? { vision_pointer: visionPointerEvidence } : {}) },
      }) });
      if (plan.action_route?.status !== "PASS" || !["accessibility", "deterministic_input", "vision_pointer"].includes(plan.action_route.mechanism)
        || plan.action_route.verification_required !== true
        || plan.action_route.observation_id !== observation.observationId
        || plan.action_route.session_id !== observation.sessionId
        || plan.selected_element?.id !== element.id) throw new Error("ACCESSIBILITY_ACTION_ROUTE_MISMATCH");
      let result: { status: string; verified: boolean; executionAttempted: boolean; observation: Observation };
      if (plan.action_route.mechanism === "vision_pointer") {
        if (!visionPointerEvidence) throw new Error("VISION_POINTER_EVIDENCE_REQUIRED");
        const grounded = groundedForPointer;
        if (!grounded || !grounded.candidate) throw new Error("VISION_POINTER_EVIDENCE_REQUIRED");
        const candidate = grounded.candidate;
        const action = await bridge.core.invoke<{ alignment: { captureSessionId: string; seedSequence: number; verifiedSequence: number; verifiedAt: number; trackingSource: string; trackingConfidence: number; targetWindowContainsPoint: boolean; targetApplication: CaptureTargetApplication }; action: { status: string; verified: boolean; executionAttempted: boolean; observation: Observation } }>("click_vision_grounded_disclosure", {
          captureSessionId: grounded.provenance.capture_session_id,
          seedSequence: grounded.provenance.frame_sequence,
          seedBboxNormalized: candidate.bbox,
          candidateConfidence: candidate.confidence,
          targetApplication: grounded.provenance.target_application,
          observationId: observation.observationId, sessionId: observation.sessionId,
          applicationPid: observation.applicationPid, elementId: element.id, label: element.label,
          candidateLabel: candidate.label,
        });
        if (action.alignment.captureSessionId !== grounded.provenance.capture_session_id
            || action.alignment.seedSequence !== grounded.provenance.frame_sequence
            || action.alignment.verifiedSequence <= action.alignment.seedSequence
            || Date.now() / 1000 - action.alignment.verifiedAt > 1
            || action.alignment.verifiedAt - Date.now() / 1000 > 1
            || action.alignment.targetApplication.applicationPid !== observation.applicationPid
            || action.alignment.targetApplication.applicationBundleId !== observation.applicationBundleId
            || action.alignment.targetWindowContainsPoint !== true
            || !["APPLE_VISION_OBJECT_TRACKING", "OPENCV_LUCAS_KANADE"].includes(action.alignment.trackingSource)
            || !Number.isFinite(action.alignment.trackingConfidence) || action.alignment.trackingConfidence < 0.75) {
          throw new Error("VISION_POINTER_EXECUTION_ALIGNMENT_INVALID");
        }
        result = action.action;
      } else {
        const executor = plan.action_route.mechanism === "deterministic_input"
          ? "click_accessibility_disclosure" : "press_accessibility_disclosure";
        result = await bridge.core.invoke<{ status: string; verified: boolean; executionAttempted: boolean; observation: Observation }>(executor, {
          observationId: observation.observationId, sessionId: observation.sessionId,
          applicationPid: observation.applicationPid, elementId: element.id, label: element.label,
        });
      }
      setObservation(result.observation);
      if (result.status === "FAILED" && result.executionAttempted === true) {
        const verified = await api<any>("/computer/actions/open-disclosure/verify", { method: "POST", body: JSON.stringify({
          plan_id: plan.plan_id, observation: { ax: result.observation },
          execution_receipt: { source: "native_accessibility", action_kind: "open_disclosure",
            status: result.status, execution_attempted: result.executionAttempted },
        }) });
        await persistObservation(result.observation);
        if (verified.recovery_offer) setRecoveryPlan({ kind: "open_disclosure", elementId: element.id,
          question: verified.recovery_question || `Open ${element.label}`,
          applicationPid: result.observation.applicationPid, applicationBundleId: result.observation.applicationBundleId,
          bypassRoutineCache: shouldBypassRoutineCache("open_disclosure", element.id, plan) });
        throw new Error(verificationFailureMessage(verified, "ACCESSIBILITY_POSTCONDITION_NOT_VERIFIED"));
      }
      if (!result.verified || result.status !== "PASS") throw new Error("ACCESSIBILITY_POSTCONDITION_NOT_VERIFIED");
      await signalPerception("action_trigger");
      const verified = await api<any>("/computer/actions/open-disclosure/verify", { method: "POST", body: JSON.stringify({
        plan_id: plan.plan_id, observation: { ax: result.observation },
        execution_receipt: { source: "native_accessibility", action_kind: "open_disclosure",
          status: result.status, execution_attempted: result.executionAttempted },
      }) });
      if (!verified.verified) {
        await persistObservation(result.observation);
        if (verified.recovery_offer) setRecoveryPlan({ kind: "open_disclosure", elementId: element.id,
          question: verified.recovery_question || `Open ${element.label}`,
          applicationPid: result.observation.applicationPid, applicationBundleId: result.observation.applicationBundleId,
          bypassRoutineCache: shouldBypassRoutineCache("open_disclosure", element.id, plan) });
      }
      if (!verified.verified) throw new Error(verificationFailureMessage(verified,
        `ACCESSIBILITY_POSTCONDITION_NOT_VERIFIED: ${verified.reason || verified.status}`));
      await signalPerception("expected_effect");
      await persistObservation(result.observation);
      setRecoveryPlan(null);
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); setActionElement(null); }
  };

  const openFindWithShortcut = async (element: ElementInfo, reviewedPlan?: any) => {
    const bridge = native();
    if (!bridge || !observation || !projectId || !conversationId || observation.applicationPid == null) return;
    setBusy(true); setActionElement(element.id); setError("");
    try {
      const plan = reviewedPlan ?? await api<any>("/computer/actions/shortcut/open-find/plan", { method: "POST", body: JSON.stringify({
        question: "Open the Find search field", project_id: projectId, conversation_id: conversationId,
        observation: { ax: observation },
      }) });
      if (plan.action_route?.status !== "PASS" || plan.action_route.mechanism !== "keyboard_shortcut"
        || plan.action_route.verification_required !== true
        || plan.action_route.observation_id !== observation.observationId
        || plan.action_route.session_id !== observation.sessionId
        || plan.selected_element?.id !== element.id) throw new Error("SHORTCUT_ACTION_ROUTE_MISMATCH");
      const result = await bridge.core.invoke<{ status: string; verified: boolean; executionAttempted: boolean; observation: Observation }>("activate_accessibility_find_shortcut", {
        observationId: observation.observationId, sessionId: observation.sessionId,
        applicationPid: observation.applicationPid, elementId: element.id, label: element.label,
      });
      setObservation(result.observation);
      if (result.status === "FAILED" && result.executionAttempted === true) {
        const verified = await api<any>("/computer/actions/shortcut/open-find/verify", { method: "POST", body: JSON.stringify({
          plan_id: plan.plan_id, observation: { ax: result.observation },
          execution_receipt: { source: "native_accessibility", action_kind: "open_find",
            status: result.status, execution_attempted: result.executionAttempted },
        }) });
        await persistObservation(result.observation);
        if (verified.recovery_offer) setRecoveryPlan({ kind: "open_find", elementId: element.id,
          question: verified.recovery_question || "Open the Find search field",
          applicationPid: result.observation.applicationPid, applicationBundleId: result.observation.applicationBundleId,
          bypassRoutineCache: shouldBypassRoutineCache("open_find", element.id, plan) });
        throw new Error(verificationFailureMessage(verified, "SHORTCUT_POSTCONDITION_NOT_VERIFIED"));
      }
      if (!result.verified || result.status !== "PASS") throw new Error("SHORTCUT_POSTCONDITION_NOT_VERIFIED");
      await signalPerception("action_trigger");
      const verified = await api<any>("/computer/actions/shortcut/open-find/verify", { method: "POST", body: JSON.stringify({
        plan_id: plan.plan_id, observation: { ax: result.observation },
        execution_receipt: { source: "native_accessibility", action_kind: "open_find",
          status: result.status, execution_attempted: result.executionAttempted },
      }) });
      if (!verified.verified) {
        await persistObservation(result.observation);
        if (verified.recovery_offer) setRecoveryPlan({ kind: "open_find", elementId: element.id,
          question: verified.recovery_question || "Open the Find search field",
          applicationPid: result.observation.applicationPid, applicationBundleId: result.observation.applicationBundleId,
          bypassRoutineCache: shouldBypassRoutineCache("open_find", element.id, plan) });
      }
      if (!verified.verified) throw new Error(verificationFailureMessage(verified,
        `SHORTCUT_POSTCONDITION_NOT_VERIFIED: ${verified.reason || verified.status}`));
      await signalPerception("expected_effect");
      await persistObservation(result.observation);
      setRecoveryPlan(null);
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); setActionElement(null); }
  };

  const shortcutLabel = (shortcut: Shortcut) => {
    const keys: string[] = [];
    if (!(shortcut.modifiers & 8)) keys.push("⌘");
    if (shortcut.modifiers & 4) keys.push("⌃");
    if (shortcut.modifiers & 2) keys.push("⌥");
    if (shortcut.modifiers & 1) keys.push("⇧");
    return `${keys.join("")}${shortcut.key.toUpperCase()}`;
  };

  const signalPerception = async (signal: "action_trigger" | "expected_effect") => {
    const bridge = native();
    if (!bridge) return;
    try {
      const next = await bridge.core.invoke<PerceptionSnapshot>("signal_perception", { signal });
      await persistPerceptionPolicy(next);
    } catch (reason: any) {
      setError(`Action completed, but perception state could not be updated: ${reason?.message || String(reason)}`);
    }
  };

  const persistObservation = async (next: Observation) => {
    try {
      await api("/desktop/world-state", { method: "PUT", body: JSON.stringify({ observation: next }) });
      const bridge = native();
      if (bridge) {
        const policy = await bridge.core.invoke<PerceptionSnapshot>("perception_status");
        await persistPerceptionPolicy(policy);
      }
      setPersistenceMessage("Current desktop state saved locally for 10 minutes.");
    } catch (reason: any) {
      setPersistenceMessage(`Local state save failed: ${reason?.message || String(reason)}`);
    }
  };

  const restoreWorldState = async () => {
    setBusy(true); setError("");
    try {
      const state = await api<UnifiedWorldState>("/world-state");
      if (state.schema_version !== 1 || !Array.isArray(state.browser_sessions)
          || !state.desktop || !["AVAILABLE", "EMPTY"].includes(state.desktop.status)) {
        throw new Error("Unified World State response is invalid.");
      }
      if ((state.desktop.status === "AVAILABLE") !== Boolean(state.desktop.record)) {
        throw new Error("Unified World State desktop status is inconsistent.");
      }
      setUnifiedWorldState(state);
      const desktopLabel = state.desktop.record ? "desktop state restored" : "no unexpired desktop state";
      setPersistenceMessage(`Refreshed World State: ${desktopLabel}; ${state.browser_sessions.length} managed browser session(s).`);
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const startMonitoring = async () => {
    const bridge = native();
    if (!bridge || selectedTargetPid === null) return;
    setBusy(true); setError(""); setLastChange(null);
    monitorOwned.current = true;
    try {
      unlisten.current = await bridge.event.listen<ChangeEvent>("accessibility-change", ({ payload }) => {
        if (payload.source !== "AX" || payload.applicationPid !== monitorPid.current) return;
        setLastChange(payload);
        void bridge.core.invoke("signal_perception", { signal: "ax_change" })
          .catch(reason => setError(reason?.message || String(reason)));
        if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
        refreshTimer.current = window.setTimeout(() => {
          void native()?.core.invoke<Observation>("observe_accessibility_application", { applicationPid: payload.applicationPid })
            .then(next => { if (next.applicationPid === payload.applicationPid) { setObservation(next); void persistObservation(next); } })
            .catch(reason => setError(reason?.message || String(reason)));
        }, 120);
      });
      monitorPid.current = selectedTargetPid;
      const status = await bridge.core.invoke<MonitorStatus>("start_accessibility_monitoring", { applicationPid: selectedTargetPid });
      monitorPid.current = status.applicationPid;
      setMonitor(status);
      if (!status.active) throw new Error("Accessibility monitor did not start");
    } catch (reason: any) {
      unlisten.current?.(); unlisten.current = null;
      monitorPid.current = null;
      monitorOwned.current = false;
      setMonitor({ active: false, applicationPid: null });
      setError(reason?.message || String(reason));
    } finally { setBusy(false); }
  };

  const stopMonitoring = async () => {
    const bridge = native();
    if (!bridge) return;
    setBusy(true); setError("");
    try {
      const status = await bridge.core.invoke<MonitorStatus>("stop_accessibility_monitoring");
      unlisten.current?.(); unlisten.current = null;
      monitorPid.current = null;
      monitorOwned.current = false;
      setMonitor(status);
    } catch (reason: any) { setError(reason?.message || String(reason)); }
    finally { setBusy(false); }
  };

  const canGroundVisionPointerFor = (element: ElementInfo): boolean => {
    return Boolean(element.role === "AXDisclosureTriangle" && capture?.status.running
      && capture.status.captureSessionId
      && capture.targetApplication?.applicationPid === observation?.applicationPid
      && capture.targetApplication?.applicationBundleId === observation?.applicationBundleId
      && pointerAccess);
  };

  const hasVisionGroundingFor = (element: ElementInfo): boolean => {
    const grounded = capture?.grounding;
    const captureSessionId = capture?.status.captureSessionId;
    return Boolean(canGroundVisionPointerFor(element)
      && captureSessionId
      && grounded?.status === "GROUNDED" && grounded.candidate
      && grounded.candidate.label.trim().toLocaleLowerCase() === element.label.trim().toLocaleLowerCase()
      && grounded.provenance.capture_session_id === captureSessionId
      && grounded.provenance.target_application.applicationPid === observation?.applicationPid
      && grounded.provenance.target_application.applicationBundleId === observation?.applicationBundleId
      && grounded.provenance.tracked_frame_sequence != null);
  };

  return <section className="screen-capture-panel" aria-label={policyView ? (ja?"コンピューター操作の権限":"Computer Use permissions") : (ja?"アクセシビリティ診断":"Accessibility diagnostics")}>
    <div className="screen-capture-heading"><div><h3>{policyView ? (ja?"コンピューター操作の権限":"Computer Use permissions") : (ja?"アクセシビリティ診断":"Accessibility diagnostics")}</h3><p>{policyView ? (ja?"macOSでデスクトップ操作の権限を許可してください。チャットで操作するアプリ名を指定します。":"Grant desktop permissions through macOS. Name the app to control in your chat message.") : (ja?"トラブルシューティング用にアクセシビリティ情報を確認します。操作の開始と制御はチャットから行います。":"Inspect a bounded snapshot or accessibility events for troubleshooting. Chat tasks remain the normal way to start and control Computer Use.")}</p></div>
      <span className={trusted ? "screen-capture-state active" : "screen-capture-state"}>{ja?(trusted?"許可済み":"許可が必要"):(trusted ? "Permission granted" : "Permission needed")}</span></div>
    {policyView && <>
      <p className="screen-capture-permission">{ja?"アクセシビリティ権限は、ラベル付きコントロールと画面上の静的テキストを確認するために使います。入力欄の値（セキュア欄を含む）は読み取りません。macOSの確認ダイアログは、アクセシビリティ権限の要求を選んだ場合にのみ表示されます。":"Accessibility permission is used to inspect labelled controls and visible static text for verification. Editable field values, including secure fields, are never read. The OS prompt appears only after you choose Request Accessibility Permission."}</p>
      <div className="screen-capture-actions" aria-label="Screen Recording permission">
        <span role="status">Screen Recording {screenCaptureAccess ? "permission granted" : "permission needed"}</span>
        {!screenCaptureAccess && <button type="button" disabled={busy} onClick={() => void requestScreenCaptureAccess()}>{ja?"画面収録を許可":"Request Screen Recording Permission"}</button>}
        <button type="button" disabled={busy} onClick={() => void refreshScreenCaptureAccess()}>{ja?"画面収録の状態を更新":"Refresh screen capture status"}</button>
      </div>
      <div className="screen-capture-actions" aria-label="Pointer input permission">
        <span role="status">Pointer input {pointerAccess ? "permission granted" : "permission needed for geometric fallback"}</span>
        {!pointerAccess && <button type="button" disabled={busy} onClick={() => void requestPointerAccess()}>{ja?"ポインター入力を許可":"Request Pointer Input Permission"}</button>}
        <button type="button" disabled={busy} onClick={() => void refreshPointerAccess()}>{ja?"ポインターの状態を更新":"Refresh pointer status"}</button>
      </div>
      <div className="screen-capture-actions">
        {!trusted && <button type="button" disabled={busy} onClick={() => void requestPermission()}>{ja?"アクセシビリティを許可":"Request Accessibility Permission"}</button>}
        <button type="button" disabled={busy} onClick={() => void refreshPermission()}>{ja?"権限の状態を更新":"Refresh permission status"}</button>
      </div>
    </>}
    {!policyView && <>
      <p className="screen-capture-permission">{ja?"確認では範囲を限定したアクセシビリティ情報を読み取ります。監視は開始後にのみ登録され、フォーカスが変わると情報を更新します。要素の値は読み取りません。":"Observation reads a bounded accessibility snapshot. Watching subscribes only after you start it and refreshes that snapshot on focus changes; element values are never read."}</p>
      {!trusted
        ? <p role="status">{ja?"コントロールを確認・監視するには、設定でアクセシビリティ権限を許可してください。":"Enable Accessibility permission in Settings before inspecting or watching controls."}</p>
        : <div className="screen-capture-actions">
          <button type="button" className="primary" disabled={busy} onClick={() => void observe()}>{ja?"フォーカス中のウィンドウを確認":"Observe focused window"}</button>
          <button type="button" disabled={busy} onClick={() => { setObservation(null); setRecoveryPlan(null); setError(""); }}>{ja?"スナップショットを消去":"Clear snapshot"}</button>
        </div>}
    </>}
    {!policyView && trusted && <div className="screen-capture-actions" aria-label="Accessibility target application">
      <button type="button" disabled={busy || monitor.active} onClick={() => void refreshTargetApplications()}>{ja?"アプリ一覧を更新":"Refresh app list"}</button>
      <label>{ja?"対象アプリ":"Target app"} <select aria-label={ja?"対象アプリ":"Target app"} value={selectedTargetPid ?? ""} disabled={busy || monitor.active || targetApplicationLocked}
        onChange={event => { const pid = event.target.value ? Number(event.target.value) : null; const selected = targetApplications.find(application => application.applicationPid === pid); setSelectedTargetPid(pid); setObservation(null); setRecoveryPlan(null); setError(""); onTargetApplicationChange?.(selected ? { applicationPid: selected.applicationPid, applicationBundleId: selected.applicationBundleId } : null); }}>
        <option value="">{ja?"起動中のアプリを選択…":"Choose a running app…"}</option>
        {targetApplications.map(application => <option key={`${application.applicationPid}:${application.applicationBundleId}`} value={application.applicationPid}>
          {application.applicationName} · {application.applicationBundleId} · PID {application.applicationPid}
        </option>)}
      </select></label>
      <button type="button" className="primary" disabled={busy || selectedTargetPid === null} onClick={() => void observeTargetApplication()}>{ja?"選択したアプリを確認":"Observe selected app"}</button>
    </div>}
    {policyView && <div className="computer-use-scope-summary" aria-label="Computer Use app scope">
      <strong>{ja?"チャットでアプリを指定":"Choose the app in chat"}</strong>
      <p>{ja?"コンピューター操作を使うには、メッセージでアプリ名を指定します。OLCRは起動中のアプリを特定し、そのプロセスにセッションを紐付けます。画面収録、アクセシビリティ、ポインターの権限はmacOSで管理されます。":"When Computer Use is on, name the app in your message. OLCR resolves the running app and binds the session to its process. macOS Screen Recording, Accessibility, and pointer permissions remain controlled by macOS."}</p>
    </div>}
    {!policyView && trusted && <div className="screen-capture-actions">
      {!monitor.active
        ? <button type="button" disabled={busy || selectedTargetPid === null} onClick={() => void startMonitoring()}>{ja?"選択したアプリの監視を開始":"Start watching selected app"}</button>
        : <button type="button" disabled={busy} onClick={() => void stopMonitoring()}>{ja?"選択したアプリの監視を停止":"Stop watching selected app"}</button>}
      <span role="status">{monitor.active ? `Watching app ${monitor.applicationPid}` : "Accessibility event monitor stopped"}</span>
    </div>}
    {!policyView && lastChange && <p role="status">AX change received: {lastChange.notification} · {new Date(lastChange.observedAt * 1000).toLocaleTimeString()}</p>}
    {!policyView && <div className="screen-capture-actions">
      <button type="button" disabled={busy} onClick={() => void restoreWorldState()}>{ja?"統合ワールド状態を更新":"Refresh unified World State"}</button>
      <span role="status">{persistenceMessage}</span>
    </div>}
    {!policyView && unifiedWorldState && <div className="accessibility-observation" aria-label="Unified world state">
      <p>Desktop: {unifiedWorldState.desktop.record
        ? `${unifiedWorldState.desktop.record.state.active_window.title || "Untitled window"} · ${unifiedWorldState.desktop.record.state.ui_elements.length} element(s) · scene ${unifiedWorldState.desktop.record.state.scene_epoch}`
        : "No unexpired desktop state."}</p>
      {unifiedWorldState.desktop.record?.state.perception_policy && <p>Advisory perception policy: {unifiedWorldState.desktop.record.state.perception_state || "unknown"} · target {unifiedWorldState.desktop.record.state.perception_policy.target_fps} fps / capture ceiling {unifiedWorldState.desktop.record.state.perception_policy.capture_fps_ceiling} fps · {unifiedWorldState.desktop.record.state.perception_policy.reason}</p>}
      {unifiedWorldState.desktop.record && <ul>{unifiedWorldState.desktop.record.state.ui_elements.slice(0, 120).map(element => <li key={element.id}><strong>{element.role}</strong> {element.label}</li>)}</ul>}
      <p>Managed Playwright sessions: {unifiedWorldState.browser_sessions.length}</p>
      {unifiedWorldState.browser_sessions.length > 0 && <ul>{unifiedWorldState.browser_sessions.map(session => <li key={session.session_id}>
        <strong>{session.status}</strong> · {session.url || "URL unavailable"}
      </li>)}</ul>}
    </div>}
    {error && <p role="alert" className="screen-capture-error">{error}</p>}
    {!policyView && observation && <div className="accessibility-observation" aria-label="Focused window observation">
      <p>{observation.windowTitle || "Untitled window"} · {observation.elements.length} semantic element(s){observation.truncated ? " · truncated" : ""}</p>
      <ul>{observation.elements.map(element => {
        const hasUsableFrame = Boolean(element.frame
          && [element.frame.x, element.frame.y, element.frame.width, element.frame.height].every(Number.isFinite)
          && element.frame.width > 0 && element.frame.height > 0);
        const hasAxPress = element.supportedActions?.includes("AXPress") ?? false;
        const ordinaryDisclosureRoute = hasUsableFrame
          && (hasAxPress || pointerAccess);
        const visionDisclosureRoute = element.frame == null && !hasAxPress
          && canGroundVisionPointerFor(element);
        return <li key={element.id}><strong>{element.role}</strong> {element.label}
          {element.role === "AXDisclosureTriangle" && element.expanded === false && element.enabled === true
            && (ordinaryDisclosureRoute || visionDisclosureRoute) && projectId && conversationId
            && recoveryPlan?.elementId !== element.id
            && <button type="button" disabled={busy} onClick={() => void openDisclosure(element)}>
              {actionElement === element.id ? "Opening…" : hasAxPress
                ? "Open with Accessibility" : hasUsableFrame ? "Open with pointer"
                  : hasVisionGroundingFor(element) ? "Open with tracked pointer" : "Ground and open with tracked pointer"}
            </button>}
          {recoveryPlan?.kind === "open_disclosure" && recoveryPlan.elementId === element.id && <button type="button"
            disabled={busy || !observation || (recoveryPlan.attempted && !recoveryPlan.plan)}
            onClick={() => recoveryPlan.plan
              ? void openDisclosure(element, recoveryPlan.plan)
              : void replanFromLatestObservation()}>
            {recoveryPlan.plan ? "Execute replanned action" : "Replan from latest observation"}
          </button>}
          {element.role === "AXMenuItem" && element.enabled === true
            && element.keyboardShortcut?.provenance === "application_menu"
            && ["find", "find…", "search", "search…", "検索", "検索…"].includes(element.label.toLocaleLowerCase())
            && recoveryPlan?.elementId !== element.id
            && projectId && conversationId && <button type="button" disabled={busy} onClick={() => void openFindWithShortcut(element)}>
              {actionElement === element.id ? "Opening…" : `Open with ${shortcutLabel(element.keyboardShortcut)}`}
            </button>}
          {recoveryPlan?.kind === "open_find" && recoveryPlan.elementId === element.id && <button type="button"
            disabled={busy || !observation || (recoveryPlan.attempted && !recoveryPlan.plan)}
            onClick={() => recoveryPlan.plan
              ? void openFindWithShortcut(element, recoveryPlan.plan)
              : void replanFromLatestObservation()}>
            {recoveryPlan.plan ? "Execute replanned action" : "Replan from latest observation"}
          </button>}
        </li>;
      })}</ul>
    </div>}
  </section>;
}
