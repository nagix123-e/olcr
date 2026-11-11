import { useEffect, useState } from "react";
import { useRef } from "react";
import { native } from "./native";
import { api } from "./api";
import { prepareGuiGroundingOcr } from "./ocr";
import { persistPerceptionPolicy, type PerceptionSnapshot } from "./perception";
import {useLanguage} from "./language";
import "./screen-capture.css";

const MAX_GROUNDING_SEED_AGE_SECONDS = 10;

type CaptureTargetApplication = { applicationPid: number; applicationBundleId: string };
type CaptureFrameStatusCounts = { complete: number; idle: number; blank: number; suspended: number; started: number; stopped: number; unknown: number; unavailable: number };
type Status = { running: boolean; trackingBackend?: string; captureSessionId?: string | null; targetApplication?: CaptureTargetApplication | null; framesReceived: number; framesProcessed?: number; framesProcessingSkipped?: number; frameStatusCounts?: CaptureFrameStatusCounts; lastFrameAt: number | null; processingP50Ms?: number | null; processingP95Ms?: number | null; opencvProcessingMs?: number; captureFpsCeiling?: number; configuredCaptureFps?: number | null; observedCaptureFps?: number | null };
type CaptureDisplayGeometry = { displayId: number; originX: number; originY: number; widthPoints: number; heightPoints: number };
type Frame = { captureSessionId: string; targetApplication?: CaptureTargetApplication | null; sequence: number; observedAt: number; width: number; height: number; displayGeometry: CaptureDisplayGeometry; mimeType: string; dataUrl: string };
type GroundedCandidate = { id: string; label: string; bbox: number[]; confidence: number };
type TrackedRegion = {
  captureSessionId: string;
  targetApplication?: CaptureTargetApplication | null;
  firstSequence: number;
  firstObservedAt: number;
  nextSequence: number;
  nextObservedAt: number;
  width: number;
  height: number;
  displayGeometry: CaptureDisplayGeometry;
  tracking: { source: string; bboxNormalized: number[]; confidence: number; processingMs: number };
  continuousTrackingArmed?: boolean;
};
type ContinuousTrackNotice = {
  captureSessionId: string;
  targetApplication: CaptureTargetApplication;
  sequence: number;
  observedAt: number;
  width: number;
  height: number;
  displayGeometry: CaptureDisplayGeometry;
  active: boolean;
  reason: string | null;
  tracking: TrackedRegion["tracking"] | null;
  trackerProcessingP50Ms?: number | null;
  trackerProcessingP95Ms?: number | null;
};
type GuiGroundingResult = {
  status: "GROUNDED" | "INCONCLUSIVE";
  candidate: GroundedCandidate | null;
  confidence: number | null;
  reason?: string;
  model_call?: GuiGroundingModelCall;
  model_calls?: GuiGroundingModelCall[];
  tracking?: TrackedRegion["tracking"];
  provenance: {
    capture_session_id: string;
    frame_sequence: number;
    observed_at: number;
    image_sha256: string;
    width: number;
    height: number;
    target_application: CaptureTargetApplication;
    display_geometry: CaptureDisplayGeometry;
    tracked_frame_sequence?: number;
    tracked_frame_observed_at?: number;
  };
};
type GuiGroundingModelCall = {
  vision_scope?: "full_screen" | "roi" | "multi_roi";
  pixel_count?: number | null;
  latency_ms?: number | null;
  vision_escalation_reason?: string;
};
type GroundedPointerAlignment = {
  status: "READY";
  captureSessionId: string;
  targetApplication: CaptureTargetApplication;
  seedSequence: number;
  verifiedSequence: number;
  verifiedAt: number;
  displayId: number;
  trackingSource: string;
  trackingConfidence: number;
  targetWindowContainsPoint: true;
  mappedGlobalPoint: [number, number];
};

const stopped: Status = { running: false, framesReceived: 0, lastFrameAt: null };

function sameDisplayGeometry(left: CaptureDisplayGeometry, right: CaptureDisplayGeometry): boolean {
  if (!left || !right) return false;
  return left.displayId === right.displayId && left.originX === right.originX
    && left.originY === right.originY && left.widthPoints === right.widthPoints
    && left.heightPoints === right.heightPoints;
}

function isNormalizedBox(value: unknown): value is [number, number, number, number] {
  return Array.isArray(value) && value.length === 4
    && value.every(point => typeof point === "number" && Number.isFinite(point) && point >= 0 && point <= 1)
    && value[0] < value[2] && value[1] < value[3];
}

export function useScreenCapture() {
  const [status, setStatus] = useState<Status>(stopped);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [perception, setPerception] = useState<PerceptionSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [groundingBusy, setGroundingBusy] = useState(false);
  const [warmingGuiModel, setWarmingGuiModel] = useState(false);
  const [guiModelReady, setGuiModelReady] = useState(false);
  const guiModelReadySession = useRef<string | null>(null);
  const guiModelWarmup = useRef<{ sessionId: string; promise: Promise<boolean> } | null>(null);
  const [grounding, setGrounding] = useState<GuiGroundingResult | null>(null);
  const [pointerAlignment, setPointerAlignment] = useState<GroundedPointerAlignment | null>(null);
  const [continuousTrack, setContinuousTrack] = useState<ContinuousTrackNotice | null>(null);
  const [targetApplication, setTargetApplication] = useState<CaptureTargetApplication | null>(null);
  const lastPersistedPolicy = useRef("");
  const lastPersistedPolicyAt = useRef(0);
  const pendingPerceptionPolicy = useRef<PerceptionSnapshot | null>(null);
  const perceptionPersistTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeCaptureSessionId = useRef<string | null>(null);
  const continuousTrackActive = useRef(false);
  const continuousTrackSequence = useRef(-1);
  const continuousTrackObservedAt = useRef<number | null>(null);
  const targetApplicationRef = useRef<CaptureTargetApplication | null>(null);
  const updateTargetApplication = (target: CaptureTargetApplication | null) => {
    targetApplicationRef.current = target;
    setTargetApplication(target);
  };
  useEffect(() => {
    if (targetApplication) targetApplicationRef.current = targetApplication;
  }, [targetApplication]);

  const prepareGuiModel = (sessionId: string): Promise<boolean> => {
    if (activeCaptureSessionId.current !== sessionId) return Promise.resolve(false);
    if (guiModelReadySession.current === sessionId) return Promise.resolve(true);
    if (guiModelWarmup.current?.sessionId === sessionId) return guiModelWarmup.current.promise;
    setGuiModelReady(false);
    setWarmingGuiModel(true);
    const promise = (async () => {
      try {
        await api<{ status: string }>("/computer/perception/gui-warmup", { method: "POST" });
        if (activeCaptureSessionId.current !== sessionId) return false;
        guiModelReadySession.current = sessionId;
        setGuiModelReady(true);
        return true;
      } catch (reason: any) {
        if (activeCaptureSessionId.current === sessionId) setError(reason?.message || "GUI_GROUNDING_WARMUP_FAILED");
        return false;
      } finally {
        if (activeCaptureSessionId.current === sessionId) setWarmingGuiModel(false);
        if (guiModelWarmup.current?.sessionId === sessionId) guiModelWarmup.current = null;
      }
    })();
    guiModelWarmup.current = { sessionId, promise };
    return promise;
  };

  const persistPendingPerceptionPolicy = () => {
    const next = pendingPerceptionPolicy.current;
    if (!next) return;
    pendingPerceptionPolicy.current = null;
    lastPersistedPolicy.current = `${next.controllerId}:${next.revision}:${next.eventWakeups ?? 0}:${next.pixelChangeWakeups ?? 0}`;
    lastPersistedPolicyAt.current = Date.now();
    void persistPerceptionPolicy(next).catch(reason => setError(reason?.message || String(reason)));
  };

  const updatePerception = (next: PerceptionSnapshot) => {
    setPerception(next);
    const revisionKey = `${next.controllerId}:${next.revision}`;
    const key = `${revisionKey}:${next.eventWakeups ?? 0}:${next.pixelChangeWakeups ?? 0}`;
    if (lastPersistedPolicy.current === key) return;
    const previousRevisionKey = lastPersistedPolicy.current.split(":").slice(0, 2).join(":");
    const policyTransition = previousRevisionKey !== revisionKey;
    pendingPerceptionPolicy.current = next;
    const elapsedMs = Date.now() - lastPersistedPolicyAt.current;
    if (policyTransition || elapsedMs >= 1_000) {
      if (perceptionPersistTimer.current !== null) clearTimeout(perceptionPersistTimer.current);
      perceptionPersistTimer.current = null;
      persistPendingPerceptionPolicy();
    } else if (perceptionPersistTimer.current === null) {
      perceptionPersistTimer.current = setTimeout(() => {
        perceptionPersistTimer.current = null;
        persistPendingPerceptionPolicy();
      }, 1_000 - elapsedMs);
    }
  };

  const signalGroundedTracking = async (active: boolean) => {
    const bridge = native();
    if (!bridge) return;
    const next = await bridge.core.invoke<PerceptionSnapshot>("signal_perception", {
      signal: active ? "grounded_track_active" : "grounded_track_stopped",
    });
    updatePerception(next);
  };

  const refreshFrame = async (expectedSessionId: string) => {
    const bridge = native();
    if (!bridge) return;
    const latest = await bridge.core.invoke<Frame | null>("latest_screen_frame");
    if (latest?.captureSessionId === expectedSessionId
        && activeCaptureSessionId.current === expectedSessionId) setFrame(latest);
  };

  const pinGroundingFrame = async (): Promise<Frame | null> => {
    const bridge = native();
    const expectedSessionId = activeCaptureSessionId.current;
    const expectedTarget = targetApplicationRef.current;
    if (!bridge || !expectedSessionId) throw new Error("SCREEN_CAPTURE_NOT_RUNNING");
    if (!expectedTarget) throw new Error("SCREEN_CAPTURE_GROUNDING_TARGET_REQUIRED");
    const pinned = await bridge.core.invoke<Frame | null>("pin_latest_grounding_frame");
    if (!pinned
        || pinned.captureSessionId !== expectedSessionId
        || activeCaptureSessionId.current !== expectedSessionId
        || pinned.targetApplication?.applicationPid !== expectedTarget.applicationPid
        || pinned.targetApplication?.applicationBundleId !== expectedTarget.applicationBundleId
        || Date.now() / 1000 - pinned.observedAt > 2.5
        || pinned.observedAt - Date.now() / 1000 > 1
        || pinned.sequence < (frame?.sequence ?? 0)) return null;
    setFrame(pinned);
    return pinned;
  };

  const groundTarget = async (question: string): Promise<GuiGroundingResult> => {
    if (!question.trim()) throw new Error("GUI_GROUNDING_QUESTION_REQUIRED");
    const bridge = native();
    if (!bridge) throw new Error("SCREEN_CAPTURE_NATIVE_BRIDGE_UNAVAILABLE");
    const sessionId = activeCaptureSessionId.current;
    if (!sessionId) throw new Error("SCREEN_CAPTURE_NOT_RUNNING");
    if (!(await prepareGuiModel(sessionId))) throw new Error("GUI_GROUNDING_MODEL_NOT_READY");
    setGroundingBusy(true);
    setError("");
    setGrounding(null);
    setPointerAlignment(null);
    if (continuousTrackActive.current) {
      try { await signalGroundedTracking(false); }
      catch (reason: any) { setError(reason?.message || String(reason)); }
    }
    continuousTrackActive.current = false;
    continuousTrackObservedAt.current = null;
    setContinuousTrack(null);
    try {
      const pinned = await pinGroundingFrame();
      if (!pinned || !pinned.targetApplication) throw new Error("SCREEN_CAPTURE_GROUNDING_FRAME_UNAVAILABLE");
      const encoded = pinned.dataUrl.split(",", 2)[1];
      if (!encoded) throw new Error("SCREEN_CAPTURE_GROUNDING_IMAGE_INVALID");
      const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      const imageSha256 = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
      const ocrEvidence = await prepareGuiGroundingOcr(question, pinned.dataUrl, imageSha256);
      let result = await api<GuiGroundingResult>("/computer/perception/gui-ground", {
        method: "POST",
        body: JSON.stringify({
          question: question.trim(), data_url: pinned.dataUrl, image_sha256: imageSha256,
          capture_session_id: pinned.captureSessionId, frame_sequence: pinned.sequence,
          observed_at: pinned.observedAt, width: pinned.width, height: pinned.height,
          display_geometry: pinned.displayGeometry, target_application: pinned.targetApplication,
          ...(ocrEvidence ? { ocr_observation: ocrEvidence.ocr_observation,
            ocr_duration_ms: ocrEvidence.ocr_duration_ms,
            ...(ocrEvidence.ocr_attempt ? { ocr_attempt: ocrEvidence.ocr_attempt } : {}),
            ...(ocrEvidence.ocr_roi ? { ocr_roi: ocrEvidence.ocr_roi } : {}) } : {}),
        }),
      });
      if (activeCaptureSessionId.current !== pinned.captureSessionId) {
        throw new Error("GUI_GROUNDING_CAPTURE_SESSION_CHANGED");
      }
      if (Date.now() / 1000 - pinned.observedAt > MAX_GROUNDING_SEED_AGE_SECONDS
          || pinned.observedAt - Date.now() / 1000 > 1) {
        throw new Error("GUI_GROUNDING_FRAME_STALE");
      }
      if (result.provenance.capture_session_id !== pinned.captureSessionId
          || result.provenance.frame_sequence !== pinned.sequence
          || result.provenance.observed_at !== pinned.observedAt
          || result.provenance.image_sha256 !== imageSha256
          || result.provenance.width !== pinned.width
          || result.provenance.height !== pinned.height
          || !sameDisplayGeometry(result.provenance.display_geometry, pinned.displayGeometry)
          || result.provenance.target_application.applicationPid !== pinned.targetApplication.applicationPid
          || result.provenance.target_application.applicationBundleId !== pinned.targetApplication.applicationBundleId) {
        throw new Error("GUI_GROUNDING_PROVENANCE_MISMATCH");
      }
      if (result.status === "GROUNDED" && result.candidate) {
        try {
          if (!isNormalizedBox(result.candidate.bbox)) throw new Error("GUI_GROUNDING_BBOX_INVALID");
          const tracked = await bridge.core.invoke<TrackedRegion>("track_latest_grounded_screen_region", {
            seedSequence: pinned.sequence,
            seedBboxNormalized: result.candidate.bbox,
          });
          const tracking = tracked.tracking;
          if (tracked.captureSessionId !== pinned.captureSessionId
              || tracked.targetApplication?.applicationPid !== pinned.targetApplication.applicationPid
              || tracked.targetApplication?.applicationBundleId !== pinned.targetApplication.applicationBundleId
              || tracked.firstSequence !== pinned.sequence
              || Math.abs(tracked.firstObservedAt - pinned.observedAt) > 0.001
              || tracked.nextSequence <= tracked.firstSequence
              || tracked.nextObservedAt <= tracked.firstObservedAt
              || Date.now() / 1000 - tracked.nextObservedAt > 2.5
              || tracked.nextObservedAt - Date.now() / 1000 > 1
              || tracked.width !== pinned.width || tracked.height !== pinned.height
              || !sameDisplayGeometry(tracked.displayGeometry, pinned.displayGeometry)
              || !["APPLE_VISION_OBJECT_TRACKING", "OPENCV_LUCAS_KANADE"].includes(tracking.source)
              || !isNormalizedBox(tracking.bboxNormalized)
              || !Number.isFinite(tracking.confidence) || tracking.confidence < 0.5
              || !Number.isFinite(tracking.processingMs) || tracking.processingMs < 0
              || activeCaptureSessionId.current !== pinned.captureSessionId) {
            throw new Error("GUI_GROUNDING_TRACKING_PROVENANCE_INVALID");
          }
          result = {
            ...result,
            tracking,
            provenance: {
              ...result.provenance,
              tracked_frame_sequence: tracked.nextSequence,
              tracked_frame_observed_at: tracked.nextObservedAt,
              display_geometry: tracked.displayGeometry,
            },
          };
          if (tracked.continuousTrackingArmed && tracking.source === "OPENCV_LUCAS_KANADE") {
            continuousTrackActive.current = true;
            continuousTrackObservedAt.current = tracked.nextObservedAt;
            setContinuousTrack({
              captureSessionId: tracked.captureSessionId,
              targetApplication: tracked.targetApplication!,
              sequence: tracked.nextSequence,
              observedAt: tracked.nextObservedAt,
              width: tracked.width,
              height: tracked.height,
              displayGeometry: tracked.displayGeometry,
              active: true,
              reason: null,
              tracking,
            });
          }
        } catch (reason: any) {
          result = {
            ...result,
            status: "INCONCLUSIVE",
            candidate: null,
            reason: reason?.message || String(reason) || "GUI_GROUNDING_TRACKING_FAILED",
          };
        }
      }
      setGrounding(result);
      return result;
    } catch (reason: any) {
      setError(reason?.message || String(reason));
      throw reason;
    } finally {
      setGroundingBusy(false);
    }
  };

  const validatePointerAlignment = async (groundedOverride?: GuiGroundingResult): Promise<GroundedPointerAlignment> => {
    const bridge = native();
    const grounded = groundedOverride ?? grounding;
    // Chat-triggered capture can start and validate grounding in one async
    // handler. React state in that handler is a render-time snapshot, while
    // the ref is updated synchronously by start() and capture status events.
    const activeTargetApplication = targetApplicationRef.current;
    if (!bridge || !grounded?.candidate || grounded.status !== "GROUNDED") {
      throw new Error("VISION_POINTER_GROUNDED_TARGET_REQUIRED");
    }
    const provenance = grounded.provenance;
    if (!activeTargetApplication
        || provenance.capture_session_id !== activeCaptureSessionId.current
        || provenance.target_application.applicationPid !== activeTargetApplication.applicationPid
        || provenance.target_application.applicationBundleId !== activeTargetApplication.applicationBundleId
        || !isNormalizedBox(grounded.candidate.bbox)
        || !Number.isFinite(grounded.candidate.confidence)
        || grounded.candidate.confidence < 0.65) {
      throw new Error("VISION_POINTER_GROUNDING_PROVENANCE_INVALID");
    }
    setPointerAlignment(null);
    const alignment = await bridge.core.invoke<GroundedPointerAlignment>("validate_grounded_pointer_target", {
      captureSessionId: provenance.capture_session_id,
      seedSequence: provenance.frame_sequence,
      seedBboxNormalized: grounded.candidate.bbox,
      candidateConfidence: grounded.candidate.confidence,
      targetApplication: provenance.target_application,
    });
    if (alignment.status !== "READY"
        || alignment.captureSessionId !== provenance.capture_session_id
        || alignment.seedSequence !== provenance.frame_sequence
        || alignment.targetApplication.applicationPid !== provenance.target_application.applicationPid
        || alignment.targetApplication.applicationBundleId !== provenance.target_application.applicationBundleId
        || alignment.displayId !== provenance.display_geometry.displayId
        || alignment.verifiedSequence <= alignment.seedSequence
        || Date.now() / 1000 - alignment.verifiedAt > 2.5
        || alignment.verifiedAt - Date.now() / 1000 > 1
        || alignment.targetWindowContainsPoint !== true
        || !Array.isArray(alignment.mappedGlobalPoint) || alignment.mappedGlobalPoint.length !== 2
        || alignment.mappedGlobalPoint.some(value => !Number.isFinite(value))
        || !["APPLE_VISION_OBJECT_TRACKING", "OPENCV_LUCAS_KANADE"].includes(alignment.trackingSource)
        || !Number.isFinite(alignment.trackingConfidence) || alignment.trackingConfidence < 0.75
        || activeCaptureSessionId.current !== provenance.capture_session_id) {
      throw new Error("VISION_POINTER_ALIGNMENT_PROVENANCE_INVALID");
    }
    setPointerAlignment(alignment);
    return alignment;
  };

  useEffect(() => {
    const bridge = native();
    if (!bridge) return;
    let live = true;
    let unlisten: (() => void) | undefined;
    let unlistenPerception: (() => void) | undefined;
    let unlistenTrack: (() => void) | undefined;
    void bridge.event.listen<{ captureSessionId: string; sequence: number; framesReceived: number; framesProcessed: number; framesProcessingSkipped: number; frameStatusCounts?: CaptureFrameStatusCounts; observedAt: number; width: number; height: number; displayGeometry: CaptureDisplayGeometry; initialBaseline: boolean; changed: boolean; changedRatio: number; sceneChange: boolean; processingP50Ms: number; processingP95Ms: number; opencvProcessingMs: number; configuredCaptureFps: number; observedCaptureFps: number | null; captureFpsCeiling: number }>("screen-capture-frame", event => {
      if (!live) return;
      if (!event.payload.captureSessionId
          || event.payload.captureSessionId !== activeCaptureSessionId.current) return;
      setStatus(current => ({ ...current, running: true, captureSessionId: event.payload.captureSessionId, framesReceived: event.payload.framesReceived, framesProcessed: event.payload.framesProcessed, framesProcessingSkipped: event.payload.framesProcessingSkipped, frameStatusCounts: event.payload.frameStatusCounts ?? current.frameStatusCounts, lastFrameAt: event.payload.observedAt, processingP50Ms: event.payload.processingP50Ms, processingP95Ms: event.payload.processingP95Ms, opencvProcessingMs: event.payload.opencvProcessingMs, captureFpsCeiling: event.payload.captureFpsCeiling, configuredCaptureFps: event.payload.configuredCaptureFps, observedCaptureFps: event.payload.observedCaptureFps }));
      const signal = event.payload.initialBaseline
        ? "stable_frame"
        : event.payload.sceneChange ? "scene_change" : event.payload.changed ? "pixel_change" : "stable_frame";
      void bridge.core.invoke<PerceptionSnapshot>("signal_perception", { signal, pixelChangeRatio: event.payload.changedRatio })
        .then(next => { if (live) updatePerception(next); })
        .catch(reason => { if (live) setError(reason?.message || String(reason)); });
      if (!event.payload.changed && !event.payload.initialBaseline) return;
      if (continuousTrackActive.current) return;
      void bridge.core.invoke<Frame | null>("latest_screen_frame").then(latest => {
        if (live && latest?.captureSessionId === event.payload.captureSessionId
            && latest.sequence >= event.payload.sequence
            && activeCaptureSessionId.current === event.payload.captureSessionId) setFrame(latest);
      })
        .catch(reason => { if (live) setError(reason?.message || String(reason)); });
    }).then(stop => { if (live) unlisten = stop; else stop(); });
    void bridge.event.listen<PerceptionSnapshot>("perception-state", event => { if (live) updatePerception(event.payload); })
      .then(stop => { if (live) unlistenPerception = stop; else stop(); });
    void bridge.event.listen<ContinuousTrackNotice>("screen-capture-track", event => {
      const notice = event.payload;
      if (!live || notice.captureSessionId !== activeCaptureSessionId.current
          || notice.targetApplication?.applicationPid !== targetApplicationRef.current?.applicationPid
          || notice.targetApplication?.applicationBundleId !== targetApplicationRef.current?.applicationBundleId
          || !Number.isFinite(notice.sequence) || !Number.isFinite(notice.observedAt)
          || Date.now() / 1000 - notice.observedAt > 2.5 || notice.observedAt - Date.now() / 1000 > 1) return;
      if (notice.active && (!notice.tracking
          || notice.tracking.source !== "OPENCV_LUCAS_KANADE"
          || !isNormalizedBox(notice.tracking.bboxNormalized)
          || !Number.isFinite(notice.tracking.confidence) || notice.tracking.confidence < 0.5
          || notice.width <= 0 || notice.height <= 0)) return;
      if (notice.sequence <= continuousTrackSequence.current) return;
      const wasActive = continuousTrackActive.current;
      continuousTrackSequence.current = notice.sequence;
      setContinuousTrack(notice);
      continuousTrackActive.current = notice.active;
      continuousTrackObservedAt.current = notice.active ? notice.observedAt : null;
      if (wasActive && !notice.active) {
        void signalGroundedTracking(false).catch(reason => setError(reason?.message || String(reason)));
      }
    }).then(stop => { if (live) unlistenTrack = stop; else stop(); });
    void bridge.core.invoke<PerceptionSnapshot>("perception_status").then(next => { if (live) setPerception(next); })
      .catch(reason => { if (live) setError(reason?.message || String(reason)); });
    void bridge.core.invoke<Status>("screen_capture_status").then(next => {
      if (!live) return;
      activeCaptureSessionId.current = next.running ? next.captureSessionId || null : null;
      const nextTarget = next.running ? next.targetApplication || null : null;
      targetApplicationRef.current = nextTarget;
      setTargetApplication(nextTarget);
      continuousTrackActive.current = false;
      continuousTrackSequence.current = -1;
      continuousTrackObservedAt.current = null;
      setContinuousTrack(null);
      setStatus(next);
      if (next.running && next.captureSessionId) {
        void refreshFrame(next.captureSessionId).catch(reason => setError(reason?.message || String(reason)));
        void prepareGuiModel(next.captureSessionId);
      }
    }).catch(reason => { if (live) setError(reason?.message || String(reason)); });
    return () => {
      live = false;
      unlisten?.(); unlistenPerception?.(); unlistenTrack?.();
      if (perceptionPersistTimer.current !== null) clearTimeout(perceptionPersistTimer.current);
      perceptionPersistTimer.current = null;
      pendingPerceptionPolicy.current = null;
    };
  }, []);

  useEffect(() => {
    if (!continuousTrack?.active || !continuousTrack.captureSessionId) return;
    const sessionId = continuousTrack.captureSessionId;
    const timer = window.setInterval(() => {
      if (!continuousTrackActive.current || activeCaptureSessionId.current !== sessionId) return;
      const lastObservedAt = continuousTrackObservedAt.current;
      if (lastObservedAt === null || Date.now() / 1000 - lastObservedAt > 2.5) {
        continuousTrackActive.current = false;
        continuousTrackObservedAt.current = null;
        setContinuousTrack(current => current?.captureSessionId === sessionId && current.active
          ? { ...current, active: false, reason: "TRACKING_HEARTBEAT_EXPIRED", tracking: null }
          : current);
        void signalGroundedTracking(false).catch(reason => setError(reason?.message || String(reason)));
        return;
      }
      void signalGroundedTracking(true).catch(reason => setError(reason?.message || String(reason)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [continuousTrack?.active, continuousTrack?.captureSessionId]);

  const start = async (targetApplication?: CaptureTargetApplication | null): Promise<Status | null> => {
    const bridge = native();
    if (!bridge) { setError("Screen perception is available in the OLCR desktop app."); return null; }
    setBusy(true); setError("");
    targetApplicationRef.current = targetApplication ?? null;
    setTargetApplication(targetApplication ?? null);
    try {
      const next = await bridge.core.invoke<Status>("start_screen_capture", targetApplication ? { targetApplication } : {});
      if (!next.captureSessionId) throw new Error("SCREEN_CAPTURE_SESSION_ID_MISSING");
      activeCaptureSessionId.current = next.captureSessionId;
      continuousTrackActive.current = false;
      continuousTrackSequence.current = -1;
      setContinuousTrack(null);
      setStatus(next);
      setFrame(null);
      setGrounding(null);
      await prepareGuiModel(next.captureSessionId);
      return next;
    }
    catch (reason: any) {
      setError(reason?.message || String(reason));
      throw reason;
    }
    finally { setBusy(false); }
  };

  const stop = async (): Promise<Status | null> => {
    window.dispatchEvent(new Event("olcr-capture-stopping"));
    const bridge = native();
    if (!bridge) return null;
    setBusy(true); setError("");
    if (continuousTrackActive.current) {
      try { await signalGroundedTracking(false); }
      catch (reason: any) { setError(reason?.message || String(reason)); }
    }
    activeCaptureSessionId.current = null;
    guiModelReadySession.current = null;
    guiModelWarmup.current = null;
    targetApplicationRef.current = null;
    setTargetApplication(null);
    continuousTrackActive.current = false;
    continuousTrackObservedAt.current = null;
    continuousTrackSequence.current = -1;
    setContinuousTrack(null);
    setGrounding(null);
    setPointerAlignment(null);
    setGuiModelReady(false);
    setWarmingGuiModel(false);
    try {
      const stoppedStatus = await bridge.core.invoke<Status>("stop_screen_capture");
      setStatus(stoppedStatus); setFrame(null);
      await api("/computer/perception/gui-release", { method: "POST" });
      return stoppedStatus;
    }
    catch (reason: any) { setError(reason?.message || String(reason)); return null; }
    finally { setBusy(false); }
  };

  const reconcileEndedSession = (sessionId: string) => {
    if (activeCaptureSessionId.current !== sessionId) return;
    activeCaptureSessionId.current = null;
    guiModelReadySession.current = null;
    guiModelWarmup.current = null;
    continuousTrackActive.current = false;
    continuousTrackObservedAt.current = null;
    continuousTrackSequence.current = -1;
    setContinuousTrack(null);
    setGrounding(null);
    setPointerAlignment(null);
    setGuiModelReady(false);
    setWarmingGuiModel(false);
    setFrame(null);
    setStatus(stopped);
  };

  return { reconcileEndedSession, status, frame, perception, error, busy, targetApplication, setTargetApplication: updateTargetApplication, start, stop, pinGroundingFrame, groundTarget, groundingBusy, grounding, pointerAlignment, validatePointerAlignment, continuousTrack, warmingGuiModel, guiModelReady, prepareGuiModel };
}

export function ScreenCapturePanel({ capture, showSessionControls = true }: { capture: ReturnType<typeof useScreenCapture>; showSessionControls?: boolean }) {
  const {language}=useLanguage();const ja=language==="ja";
  const [groundingQuestion, setGroundingQuestion] = useState("");
  const [alignmentError, setAlignmentError] = useState("");
  const groundingCalls = capture.grounding?.model_calls?.length
    ? capture.grounding.model_calls
    : capture.grounding?.model_call ? [capture.grounding.model_call] : [];
  const groundingTotalPixels = groundingCalls.reduce((total, call) =>
    total + (Number.isFinite(call.pixel_count) ? Number(call.pixel_count) : 0), 0);
  const groundingTotalLatency = groundingCalls.reduce((total, call) =>
    total + (Number.isFinite(call.latency_ms) ? Number(call.latency_ms) : 0), 0);
  return <section className="screen-capture-panel" aria-label={ja?"画面認識":"Screen perception"}>
    <div className="screen-capture-heading"><div><h3>{ja?"画面認識":"Screen perception"}</h3><p>{ja?"ScreenCaptureKitのライブ映像ストリームです。固定した基準フレーム1枚と最新フレームのみをメモリに保持し、フレームファイルは作成しません。":"Live ScreenCaptureKit stream · one pinned seed and the latest frame stay in memory · no frame files are written."}</p></div>
      <span className={capture.status.running ? "screen-capture-state active" : "screen-capture-state"}>{ja?(capture.status.running?"実行中":"停止中"):(capture.status.running ? "Running" : "Stopped")}</span></div>
    <p className="screen-capture-permission">{ja?`設定で画面収録を許可してください。macOSが画面への直接アクセスを確認することがあります。OLCRが取得するのは映像フレームのみです。チャットで指定したアプリのプロセスに収録を紐付けます。${showSessionControls?"停止ボタンを押すと収録を終了します。":"チャットからセッションを開始・停止できます。"}64×36の画面変化検知とアクセシビリティイベントを使って認識の優先度を調整します。OpenCVの追跡が有効な場合、対応するmacOSでは20fpsを要求し、対象領域をメモリ上で更新しながらJPEG転送とUI-Venusの再処理を一時停止します。フレームは追跡用にメモリへ届き続け、追跡を失うと通常の認識に戻ります。`:`Grant Screen Recording in Settings. macOS may also ask you to Allow direct screen access; OLCR captures video frames only. Capture binds to the exact app process named in chat. ${showSessionControls ? "Capture stops when you press Stop." : "Start or stop the session from chat."} The 64×36 visual change sentinel and accessibility events adapt perception priority. A grounded OpenCV track requests 20 fps on supported macOS; while it is healthy, OLCR updates the target box in memory and pauses JPEG transfer and UI-Venus refresh. Frames still arrive in memory for tracking, and capture falls back when the track is lost.`}</p>
    {capture.perception && <p role="status">{ja?"認識":"Perception"} {ja?({IDLE:"待機",ACTIVE:"通常",HIGH:"高優先度",BURST:"バースト"} as Record<string,string>)[capture.perception.state]||capture.perception.state:capture.perception.state} · {ja?"目標":"target"} {capture.perception.targetFps} fps · {ja?"収録上限":"capture ceiling"} {capture.perception.captureFpsCeiling} fps · {ja?"処理レベル":"compute target"} L{capture.perception.computeLevelTarget} · {ja?"イベント／画素変化の起動回数":"event/pixel wakeups"} {capture.perception.eventWakeups ?? 0}/{capture.perception.pixelChangeWakeups ?? 0} · {ja?"状態別の経過時間 待機／通常／高優先度／バースト":"state time IDLE/ACTIVE/HIGH/BURST"} {capture.perception.stateDurationsMs ? `${(capture.perception.stateDurationsMs.idleMs / 1000).toFixed(1)}/${(capture.perception.stateDurationsMs.activeMs / 1000).toFixed(1)}/${(capture.perception.stateDurationsMs.highMs / 1000).toFixed(1)}/${(capture.perception.stateDurationsMs.burstMs / 1000).toFixed(1)} s` : "—"}</p>}
    {showSessionControls && <div className="screen-capture-actions">
      {!capture.status.running
        ? <button type="button" className="primary" disabled={capture.busy} onClick={() => void capture.start(capture.targetApplication).catch(() => {})}>{ja?"画面認識を開始":"Start screen perception"}</button>
        : <button type="button" disabled={capture.busy} onClick={() => void capture.stop()}>{ja?"画面認識を停止":"Stop screen perception"}</button>}
      {capture.status.running && <span>{ja?`${capture.status.framesReceived} 受信 / ${capture.status.framesProcessed ?? "—"} 処理済み / ${capture.status.framesProcessingSkipped ?? "—"} 処理スキップ · 設定 ${capture.status.configuredCaptureFps ?? "—"} fps · 実測 ${capture.status.observedCaptureFps?.toFixed(1) ?? "—"} fps · 上限 ${capture.status.captureFpsCeiling ?? capture.perception?.captureFpsCeiling ?? 2} fps · 最大 1920×1080`:`${capture.status.framesReceived} received / ${capture.status.framesProcessed ?? "—"} processed / ${capture.status.framesProcessingSkipped ?? "—"} processing skips · configured ${capture.status.configuredCaptureFps ?? "—"} fps · observed ${capture.status.observedCaptureFps?.toFixed(1) ?? "—"} fps · cap ${capture.status.captureFpsCeiling ?? capture.perception?.captureFpsCeiling ?? 2} fps · 1920×1080 max`}</span>}
    </div>}
    {capture.status.running && capture.status.frameStatusCounts && <p>{ja?"ScreenCaptureKitの状態別フレーム数（OSによるドロップ数ではありません）":"ScreenCaptureKit status (not OS drop counts)"} · {ja?"完了":"complete"} {capture.status.frameStatusCounts.complete} · {ja?"待機":"idle"} {capture.status.frameStatusCounts.idle} · {ja?"空白":"blank"} {capture.status.frameStatusCounts.blank} · {ja?"一時停止":"suspended"} {capture.status.frameStatusCounts.suspended} · {ja?"開始":"started"} {capture.status.frameStatusCounts.started} · {ja?"停止":"stopped"} {capture.status.frameStatusCounts.stopped} · {ja?"不明":"unknown"} {capture.status.frameStatusCounts.unknown} · {ja?"利用不可":"unavailable"} {capture.status.frameStatusCounts.unavailable}</p>}
    {capture.status.running && capture.targetApplication && <div className="screen-capture-grounding">
      <label htmlFor="screen-grounding-question">{ja?"UI-Venusで画面内の対象を指定":"Ground one visible target with UI-Venus"}</label>
      {!capture.guiModelReady && <p role="status">{capture.warmingGuiModel
        ? (ja?"最新フレームで対象を特定するためUI-Venusを準備中…":"Preparing UI-Venus for fresh-frame grounding…")
        : (ja?"UI-Venusによる対象特定の準備ができていません。":"UI-Venus is not ready for grounded capture.")}</p>}
      {!capture.guiModelReady && !capture.warmingGuiModel && <button type="button"
        onClick={() => capture.status.captureSessionId && void capture.prepareGuiModel(capture.status.captureSessionId)}>
        {ja?"UI-Venusの準備を再試行":"Retry UI-Venus preparation"}
      </button>}
      <input id="screen-grounding-question" value={groundingQuestion} maxLength={1000}
        onChange={event => setGroundingQuestion(event.target.value)} placeholder={ja?"例：保存ボタン":"e.g. the Save button"} />
      <button type="button" disabled={!capture.guiModelReady || capture.warmingGuiModel || capture.groundingBusy || !groundingQuestion.trim()}
        onClick={() => void capture.groundTarget(groundingQuestion).catch(() => undefined)}>
        {ja?(capture.groundingBusy?"対象を特定中…":capture.warmingGuiModel?"UI-Venusを準備中…":"対象を特定"):(capture.groundingBusy ? "Grounding…" : capture.warmingGuiModel ? "Preparing UI-Venus…" : "Ground target")}
      </button>
      {capture.grounding && <p role="status">{capture.grounding.status === "GROUNDED" && capture.grounding.candidate
        ? `${capture.grounding.candidate.label} · UI-Venus ${capture.grounding.candidate.confidence.toFixed(2)} · ${ja?"追跡":"tracked"} ${capture.grounding.provenance.frame_sequence}→${capture.grounding.provenance.tracked_frame_sequence ?? "—"} · ${ja?"追跡信頼度":"tracker confidence"} ${capture.grounding.tracking?.confidence.toFixed(2) ?? (ja?"利用不可":"unavailable")}`
        : `${ja?"特定できません":"INCONCLUSIVE"} · ${capture.grounding.reason ?? (ja?"候補がありません":"no eligible candidate")} · ${ja?"フレーム":"frame"} ${capture.grounding.provenance.frame_sequence}`}</p>}
      {capture.grounding?.status === "GROUNDED" && capture.grounding.candidate && <>
        <button type="button" disabled={capture.groundingBusy}
          onClick={() => {
            setAlignmentError("");
            void capture.validatePointerAlignment().catch(reason => {
              setAlignmentError(reason?.message || String(reason));
            });
          }}>
          {ja?"ポインター位置の整合性を確認":"Check pointer alignment"}
        </button>
        <p className="screen-capture-grounding-telemetry">
          {ja?"読み取り専用の確認です。既存のアクセシビリティ権限とポインターイベント権限を使い、許可ダイアログを開いたりクリックを送信したりしません。":"Read-only check; requires existing Accessibility and pointer-event permissions. It never opens a permission prompt or sends a click."}
        </p>
        {capture.pointerAlignment && <p role="status">
          {ja?"ポインター位置の整合性 OK":"Pointer alignment READY"} · {ja?"フレーム":"frame"} {capture.pointerAlignment.seedSequence}→{capture.pointerAlignment.verifiedSequence}
          · {capture.pointerAlignment.trackingSource} {capture.pointerAlignment.trackingConfidence.toFixed(2)}
          · {ja?"アクセシビリティ対象ウィンドウ内に変換後の座標があります":"AX target window contains mapped point"} · {ja?"ディスプレイ":"display"} {capture.pointerAlignment.displayId}
        </p>}
        {alignmentError && <p className="screen-capture-error" role="alert">{alignmentError}</p>}
      </>}
      {capture.continuousTrack?.active && capture.continuousTrack.tracking && <p role="status">
        {ja?"OpenCVの追跡中":"OpenCV live track"} · {ja?"フレーム":"frame"} {capture.continuousTrack.sequence} · {ja?"信頼度":"confidence"} {capture.continuousTrack.tracking.confidence.toFixed(2)}
        {Number.isFinite(capture.continuousTrack.trackerProcessingP50Ms) && Number.isFinite(capture.continuousTrack.trackerProcessingP95Ms)
          && ` · ${ja?"追跡処理 p50/p95":"tracker p50/p95"} ${capture.continuousTrack.trackerProcessingP50Ms?.toFixed(2)}/${capture.continuousTrack.trackerProcessingP95Ms?.toFixed(2)} ms`}
        {ja?" · 対象領域の更新のみ。画像／VLMの再処理は一時停止中":" · bbox updates only; image/VLM refresh paused"}
      </p>}
      {capture.continuousTrack && !capture.continuousTrack.active && <p role="status">
        {ja?"OpenCVの追跡を停止":"OpenCV track stopped"} · {capture.continuousTrack.reason ?? (ja?"対象を再特定してください":"re-ground the target")}
      </p>}
      {groundingCalls.length > 1 ? <p className="screen-capture-grounding-telemetry">
        {ja?"Visionの解析範囲":"Vision analysis scope"}: {groundingCalls.map(call => call.vision_scope ?? "unknown").join(" → ")}
        {groundingCalls.some(call => call.vision_escalation_reason === "OCR_INSUFFICIENT") && (ja?" · OCR領域の情報が不十分":" · OCR ROI insufficient")}
        {` · ${groundingCalls.length} ${ja?"回":"attempts"}`}
        {` · ${groundingTotalPixels.toLocaleString()} ${ja?"画素 合計":"pixels total"}`}
        {` · ${groundingTotalLatency.toFixed(2)} ms ${ja?"推論時間 合計":"total inference"}`}
      </p> : capture.grounding?.model_call && <p className="screen-capture-grounding-telemetry">
        {ja?"Visionへの入力":"Vision input"}: {capture.grounding.model_call.vision_scope ?? "unknown"}
        {Number.isFinite(capture.grounding.model_call.pixel_count) && ` · ${capture.grounding.model_call.pixel_count?.toLocaleString()} ${ja?"画素":"pixels"}`}
        {Number.isFinite(capture.grounding.model_call.latency_ms) && ` · ${capture.grounding.model_call.latency_ms?.toFixed(2)} ms`}
      </p>}
    </div>}
    {capture.status.running && capture.status.processingP50Ms != null && capture.status.processingP95Ms != null
      && <p>{ja?"画面変化検知の処理 p50/p95":"Visual change detector p50/p95"}: {capture.status.processingP50Ms.toFixed(2)} / {capture.status.processingP95Ms.toFixed(2)} ms · {ja?"直近最大120フレーム":"last 120 frames max"}</p>}
    {capture.status.running && <p>{ja?"画面追跡":"Visual tracker"}: {capture.status.trackingBackend === "OPENCV_LUCAS_KANADE" ? "OpenCV Lucas–Kanade" : capture.status.trackingBackend === "APPLE_VISION_OBJECT_TRACKING" ? "Apple Vision" : (ja?"利用不可":"unavailable")}</p>}
    {capture.status.running && (capture.status.opencvProcessingMs ?? 0) > 0
      && <p>{ja?"OpenCVの画面変化検知＋対象追跡 · この収録セッションの累計":"OpenCV change detection + grounded tracking · cumulative this capture session"}: {capture.status.opencvProcessingMs?.toFixed(2)} ms</p>}
    {capture.error && <p className="screen-capture-error" role="alert">{capture.error}</p>}
    {capture.frame && <figure className="screen-capture-preview"><img src={capture.frame.dataUrl} alt={ja?"直近の画面変化フレーム":"Latest changed screen frame"} />
      <figcaption>{ja?"直近の変化フレーム":"Latest changed frame"} · {capture.frame.width}×{capture.frame.height} px · {ja?"ディスプレイ":"display"} {capture.frame.displayGeometry.displayId} · {ja?"座標範囲":"bounds"} ({capture.frame.displayGeometry.originX}, {capture.frame.displayGeometry.originY}) {capture.frame.displayGeometry.widthPoints}×{capture.frame.displayGeometry.heightPoints} pt · {ja?"対象":"target"} {capture.frame.targetApplication ? `${capture.frame.targetApplication.applicationBundleId} (PID ${capture.frame.targetApplication.applicationPid})` : (ja?"未紐付け":"unbound")} · {ja?"収録":"capture"} {capture.frame.captureSessionId} · {ja?"順序番号":"sequence"} {capture.frame.sequence}</figcaption></figure>}
  </section>;
}
