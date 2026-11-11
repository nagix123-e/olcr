import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { api } from "./api";
import { ScreenCapturePanel, useScreenCapture } from "./ScreenCapture";

vi.mock("./api", () => ({ api: vi.fn() }));

const targetApplication = { applicationPid: 445, applicationBundleId: "com.apple.TextEdit" };
const frameStatusCounts = { complete: 8, idle: 4, blank: 0, suspended: 0, started: 1, stopped: 0, unknown: 0, unavailable: 0 };
const status = { running: true, trackingBackend: "OPENCV_LUCAS_KANADE", captureSessionId: "capture-a7", targetApplication, framesReceived: 1, framesProcessed: 1, framesProcessingSkipped: 0, frameStatusCounts, lastFrameAt: 10, captureFpsCeiling: 30, configuredCaptureFps: 2, observedCaptureFps: 1.9 };
const frame = { captureSessionId: "capture-a7", targetApplication, sequence: 1, observedAt: Date.now() / 1000, width: 640, height: 480, displayGeometry: { displayId: 7, originX: -640, originY: 0, widthPoints: 640, heightPoints: 480 }, mimeType: "image/jpeg", dataUrl: "data:image/jpeg;base64,ZmFrZQ==" };

function Harness({ showSessionControls = true }: { showSessionControls?: boolean }) {
  const capture = useScreenCapture();
  return <><button type="button" onClick={() => capture.setTargetApplication(targetApplication)}>Choose capture target</button><button type="button" onClick={() => void capture.start(targetApplication)}>Start capture with explicit target</button><button type="button" onClick={() => void capture.start(targetApplication).then(() => capture.validatePointerAlignment({
    status: "GROUNDED", candidate: { id: "save-1", label: "Save", bbox: [0.1, 0.2, 0.3, 0.4], confidence: 0.9 }, confidence: 0.9,
    provenance: { capture_session_id: "capture-a7", frame_sequence: 1, observed_at: Date.now() / 1000,
      image_sha256: "01".repeat(32), width: 640, height: 480, target_application: targetApplication,
      display_geometry: frame.displayGeometry },
  })).catch(() => {})}>Start capture and immediately validate pointer</button><button type="button" onClick={() => void capture.start(targetApplication).then(() => capture.pinGroundingFrame()).catch(() => {})}>Start capture and immediately pin after preparation</button><button type="button" onClick={() => void capture.pinGroundingFrame().catch(() => {})}>Pin grounding frame</button><ScreenCapturePanel capture={capture} showSessionControls={showSessionControls} /></>;
}

describe("ScreenCapturePanel", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("can expose diagnostics without making Settings the session launcher", () => {
    render(<Harness showSessionControls={false} />);
    expect(screen.queryByRole("button", { name: "Start screen perception" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop screen perception" })).toBeNull();
    expect(screen.getByText(/Start or stop the session from chat/)).toBeTruthy();
  });

  it("preserves the native permission failure when ScreenCaptureKit cannot start", async () => {
    const permissionError = "SCREEN_CAPTURE_PERMISSION_REQUIRED: enable Screen & System Audio Recording, then reopen OLCR.";
    const invoke = vi.fn(async (command: string) => {
      if (command === "perception_status") return { state: "IDLE", targetFps: 2, captureFpsCeiling: 2, computeLevelTarget: 0 };
      if (command === "screen_capture_status") return { running: false, framesReceived: 0, lastFrameAt: null };
      if (command === "start_screen_capture") throw permissionError;
      throw new Error(`Unexpected command ${command}`);
    });
    const listen = vi.fn(async () => vi.fn());
    vi.stubGlobal("window", Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } }));

    render(<Harness />);
    await waitFor(() => expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function)));
    fireEvent.click(screen.getByRole("button", { name: "Choose capture target" }));
    fireEvent.click(screen.getByRole("button", { name: "Start screen perception" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(permissionError);
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("keeps the explicit target available to grounding immediately after chat starts capture", async () => {
    const invoke = vi.fn(async (command: string) => {
      if (command === "perception_status") return { state: "IDLE", targetFps: 2, captureFpsCeiling: 30, computeLevelTarget: 0 };
      if (command === "screen_capture_status") return { running: false, framesReceived: 0, lastFrameAt: null };
      if (command === "start_screen_capture") return status;
      if (command === "latest_screen_frame" || command === "pin_latest_grounding_frame") return frame;
      throw new Error(`Unexpected command ${command}`);
    });
    const listen = vi.fn(async () => vi.fn());
    vi.stubGlobal("window", Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } }));
    let resolveWarmup!: (value: { status: string }) => void;
    const warmup = new Promise<{ status: string }>(resolve => { resolveWarmup = resolve; });
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/computer/perception/gui-warmup") return await warmup as never;
      return { status: "READY" } as never;
    });

    render(<Harness />);
    await waitFor(() => expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function)));
    fireEvent.click(screen.getByRole("button", { name: "Start capture and immediately pin after preparation" }));
    await waitFor(() => expect(screen.getByText("Running")).toBeInTheDocument());
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/perception/gui-warmup", { method: "POST" }));
    expect(invoke).not.toHaveBeenCalledWith("pin_latest_grounding_frame");

    await act(async () => { resolveWarmup({ status: "READY" }); await warmup; });

    await waitFor(() => expect(invoke).toHaveBeenCalledWith("pin_latest_grounding_frame"));
    expect(await screen.findByAltText("Latest changed screen frame")).toHaveAttribute("src", frame.dataUrl);
  });

  it("validates pointer alignment using the capture target started in the same chat turn", async () => {
    const invoke = vi.fn(async (command: string, args?: any) => {
      if (command === "perception_status") return { state: "IDLE", targetFps: 2, captureFpsCeiling: 30, computeLevelTarget: 0 };
      if (command === "screen_capture_status") return { running: false, framesReceived: 0, lastFrameAt: null };
      if (command === "start_screen_capture") return status;
      if (command === "validate_grounded_pointer_target") return {
        status: "READY", captureSessionId: args.captureSessionId, targetApplication: args.targetApplication,
        seedSequence: args.seedSequence, verifiedSequence: args.seedSequence + 1, verifiedAt: Date.now() / 1000,
        displayId: 7, trackingSource: "OPENCV_LUCAS_KANADE", trackingConfidence: 0.9,
        targetWindowContainsPoint: true, mappedGlobalPoint: [240, 180],
      };
      throw new Error(`Unexpected command ${command}`);
    });
    const listen = vi.fn(async () => vi.fn());
    vi.stubGlobal("window", Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } }));
    vi.mocked(api).mockImplementation(async () => ({ status: "READY" }) as never);

    render(<Harness />);
    await waitFor(() => expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function)));
    fireEvent.click(screen.getByRole("button", { name: "Start capture and immediately validate pointer" }));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith("validate_grounded_pointer_target", expect.objectContaining({
      captureSessionId: "capture-a7", targetApplication, seedSequence: 1,
    })));
  });

  it("starts only on request, refreshes on frame events, and clears the latest frame on stop", async () => {
    let onFrame: ((event: { payload: { captureSessionId: string; sequence: number; framesReceived: number; framesProcessed: number; framesProcessingSkipped: number; frameStatusCounts?: typeof frameStatusCounts; observedAt: number; width: number; height: number; displayGeometry: typeof frame.displayGeometry; initialBaseline?: boolean; changed: boolean; changedRatio: number; sceneChange?: boolean; processingP50Ms?: number; processingP95Ms?: number; opencvProcessingMs?: number; captureFpsCeiling?: number; configuredCaptureFps?: number; observedCaptureFps?: number | null } }) => void) | undefined;
    let onTrack: ((event: { payload: any }) => void) | undefined;
    let running = false;
    let latestFrame = frame;
    let trackingCorruption: "target" | "session" | "stale" | null = null;
    let trackingFailure: string | null = null;
    let trackingFrameFreshAfterInference = false;
    let inferenceDelayMs = 0;
    let clockOffsetMs = 0;
    let trackingSource = "APPLE_VISION_OBJECT_TRACKING";
    let ambiguousOcr = false;
    let failNativeOcr = false;
    let eventWakeups = 0;
    let pixelChangeWakeups = 0;
    const invoke = vi.fn(async (command: string, args?: any) => {
      if (command === "perception_status") return { state: "IDLE", reason: "stable_scene", targetFps: 2, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 0, burstAvailable: true, eventWakeups, pixelChangeWakeups };
      if (command === "signal_perception") {
        if (args?.signal === "pixel_change" || args?.signal === "scene_change") pixelChangeWakeups += 1;
        if (["ax_change", "scroll", "action_click", "action_trigger", "expected_effect", "grounded_track_active"].includes(args?.signal)) eventWakeups += 1;
        return { state: "IDLE", reason: "stable_scene", targetFps: 2, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 0, burstAvailable: true, eventWakeups, pixelChangeWakeups };
      }
      if (command === "screen_capture_status") return running ? status : { running: false, framesReceived: 0, lastFrameAt: null };
      if (command === "start_screen_capture") { running = true; return status; }
      if (command === "latest_screen_frame") return latestFrame;
      if (command === "pin_latest_grounding_frame") return latestFrame;
      if (command === "recognize_image_text" && failNativeOcr) throw new Error("native OCR unavailable");
      if (command === "recognize_image_text") return {
        source: "APPLE_VISION_OCR", observationId: "ocr-screen-1",
        coordinateSpace: "normalized_image_top_left", width: latestFrame.width, height: latestFrame.height,
        elements: ambiguousOcr
          ? [{ text: "Save", bboxNormalized: [0.3, 0.3, 0.4, 0.4], confidence: 0.98 },
            { text: "Save", bboxNormalized: [0.6, 0.3, 0.7, 0.4], confidence: 0.98 }]
          : [{ text: "Save", bboxNormalized: [0.3, 0.3, 0.4, 0.4], confidence: 0.98 }],
      };
      if (command === "crop_ocr_grounded_roi") return {
        dataUrl: "data:image/jpeg;base64,cm9p", width: 256, height: 264,
        sourceBboxNormalized: [0.2, 0.25, 0.6, 0.8], sourceBboxPixels: [128, 120, 384, 384],
      };
      if (command === "track_latest_grounded_screen_region" && trackingFailure) throw trackingFailure;
      if (command === "track_latest_grounded_screen_region") return {
        captureSessionId: trackingCorruption === "session" ? "capture-other" : latestFrame.captureSessionId,
        targetApplication: trackingCorruption === "target" ? { ...targetApplication, applicationPid: 999 } : targetApplication,
        firstSequence: args.seedSequence,
        firstObservedAt: latestFrame.observedAt,
        nextSequence: args.seedSequence + 1,
        nextObservedAt: trackingCorruption === "stale" ? Date.now() / 1000 - 4
          : trackingFrameFreshAfterInference ? Date.now() / 1000 - 0.2 : latestFrame.observedAt + 0.5,
        width: latestFrame.width,
        height: latestFrame.height,
        displayGeometry: latestFrame.displayGeometry,
        tracking: { source: trackingSource, bboxNormalized: [0.12, 0.22, 0.32, 0.42], confidence: 0.91, processingMs: 2.1 },
        continuousTrackingArmed: trackingSource === "OPENCV_LUCAS_KANADE",
      };
      if (command === "validate_grounded_pointer_target") return {
        status: "READY", captureSessionId: args.captureSessionId,
        targetApplication: args.targetApplication, seedSequence: args.seedSequence,
        verifiedSequence: args.seedSequence + 2, verifiedAt: Date.now() / 1000,
        displayId: 7, trackingSource: trackingSource, trackingConfidence: 0.86,
        targetWindowContainsPoint: true, mappedGlobalPoint: [240, 180],
      };
      if (command === "stop_screen_capture") { running = false; return { running: false, framesReceived: 0, lastFrameAt: null }; }
      throw new Error(`Unexpected command ${command}`);
    });
    const listen = vi.fn(async (name: string, callback: (event: { payload: any }) => void) => {
      if (name === "screen-capture-frame") onFrame = callback as typeof onFrame;
      if (name === "screen-capture-track") onTrack = callback;
      return vi.fn();
    });
    vi.stubGlobal("window", Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } }));

    render(<Harness />);
    await waitFor(() => expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function)));
    expect(invoke).not.toHaveBeenCalledWith("start_screen_capture");
    fireEvent.click(screen.getByRole("button", { name: "Choose capture target" }));
    fireEvent.click(screen.getByRole("button", { name: "Start screen perception" }));
    await waitFor(() => expect(screen.getByText("Running")).toBeInTheDocument());
    expect(screen.getByText("Visual tracker: OpenCV Lucas–Kanade")).toBeTruthy();
    expect(screen.getByText(/ScreenCaptureKit status \(not OS drop counts\) · complete 8 · idle 4/)).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("start_screen_capture", { targetApplication });
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/perception/gui-warmup", { method: "POST" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Pin grounding frame" })); });
    expect(invoke).toHaveBeenCalledWith("pin_latest_grounding_frame");

    vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).fill(1).buffer) } });
    let roiFallbackTelemetry = false;
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path === "/computer/perception/gui-warmup") return { status: "READY" } as never;
      if (path === "/computer/perception/gui-release") return { status: "RELEASED" } as never;
      if (path === "/computer/perception/gui-ground" && inferenceDelayMs) {
        clockOffsetMs += inferenceDelayMs;
        inferenceDelayMs = 0;
      }
      const payload = JSON.parse(String(init?.body));
      return { status: "GROUNDED", candidate: { id: "save-1", label: "Save", bbox: [0.1, 0.2, 0.3, 0.4], confidence: 0.9 }, confidence: 0.9,
        model_call: { vision_scope: roiFallbackTelemetry ? "full_screen" : payload.ocr_roi ? "roi" : "full_screen",
          pixel_count: roiFallbackTelemetry ? payload.width * payload.height
            : payload.ocr_roi ? 256 * 264 : payload.width * payload.height,
          latency_ms: roiFallbackTelemetry ? 120 : 41.5 },
        ...(roiFallbackTelemetry ? { model_calls: [
          { vision_scope: "roi", pixel_count: 256 * 264, latency_ms: 41.5, vision_escalation_reason: "OCR_GROUNDED_TARGET" },
          { vision_scope: "full_screen", pixel_count: payload.width * payload.height, latency_ms: 120, vision_escalation_reason: "OCR_INSUFFICIENT" },
        ] } : {}),
        provenance: { capture_session_id: payload.capture_session_id, frame_sequence: payload.frame_sequence, observed_at: payload.observed_at,
          image_sha256: payload.image_sha256, width: payload.width, height: payload.height, display_geometry: payload.display_geometry,
          target_application: payload.target_application } } as never;
    });
    const realDateNow = Date.now.bind(Date);
    vi.spyOn(Date, "now").mockImplementation(() => realDateNow() + clockOffsetMs);
    latestFrame = { ...frame, observedAt: realDateNow() / 1000 };
    inferenceDelayMs = 3000;
    trackingFrameFreshAfterInference = true;
    fireEvent.change(screen.getByLabelText("Ground one visible target with UI-Venus"), { target: { value: "the Save button" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText("Save · UI-Venus 0.90 · tracked 1→2 · tracker confidence 0.91")).toBeInTheDocument();
    expect(await screen.findByText("Vision input: roi · 67,584 pixels · 41.50 ms")).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("track_latest_grounded_screen_region", {
      seedSequence: 1, seedBboxNormalized: [0.1, 0.2, 0.3, 0.4],
    });
    expect(Date.now() / 1000 - latestFrame.observedAt).toBeGreaterThan(2.5);
    expect(screen.queryByText(/INCONCLUSIVE · GUI_GROUNDING_FRAME_STALE/)).toBeNull();
    trackingFrameFreshAfterInference = false;
    clockOffsetMs = 0;
    expect(api).toHaveBeenCalledWith("/computer/perception/gui-ground", expect.objectContaining({ method: "POST", body: expect.any(String) }));
    const groundingCall = vi.mocked(api).mock.calls.find(([path]) => path === "/computer/perception/gui-ground");
    expect(JSON.parse(String(groundingCall?.[1]?.body))).toMatchObject({
      question: "the Save button", capture_session_id: "capture-a7", frame_sequence: 1,
      ocr_duration_ms: expect.any(Number),
      target_application: targetApplication,
      ocr_observation: { source: "APPLE_VISION_OCR", observationId: "ocr-screen-1", imageSha256: "01".repeat(32) },
      ocr_roi: {
        source: "APPLE_VISION_OCR", observation_id: "ocr-screen-1", image_sha256: "01".repeat(32),
        target_text: "Save", target_confidence: 0.98,
        source_bbox_normalized: [0.2, 0.25, 0.6, 0.8], source_bbox_pixels: [128, 120, 384, 384],
        width: 256, height: 264,
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check pointer alignment" }));
    expect(await screen.findByText(/Pointer alignment READY · frame 1→3/)).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("validate_grounded_pointer_target", {
      captureSessionId: "capture-a7", seedSequence: 1,
      seedBboxNormalized: [0.1, 0.2, 0.3, 0.4], candidateConfidence: 0.9,
      targetApplication,
    });
    latestFrame = { ...frame, sequence: 2, observedAt: realDateNow() / 1000 };

    roiFallbackTelemetry = true;
    failNativeOcr = true;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText("Vision analysis scope: roi → full_screen · OCR ROI insufficient · 2 attempts · 374,784 pixels total · 161.50 ms total inference")).toBeInTheDocument();
    const failedOcrPayload = JSON.parse(String(vi.mocked(api).mock.calls
      .filter(([path]) => path === "/computer/perception/gui-ground").at(-1)?.[1]?.body));
    expect(failedOcrPayload.ocr_attempt).toMatchObject({
      source: "APPLE_VISION_OCR", status: "error", image_sha256: "01".repeat(32),
    });
    expect(failedOcrPayload).not.toHaveProperty("ocr_observation");
    failNativeOcr = false;
    roiFallbackTelemetry = false;

    const roiCropCallsBeforeAmbiguousOcr = invoke.mock.calls.filter(([command]) => command === "crop_ocr_grounded_roi").length;
    ambiguousOcr = true;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText("Save · UI-Venus 0.90 · tracked 2→3 · tracker confidence 0.91")).toBeInTheDocument();
    expect(await screen.findByText("Vision input: full_screen · 307,200 pixels · 41.50 ms")).toBeInTheDocument();
    const ambiguousPayload = JSON.parse(String(vi.mocked(api).mock.calls.at(-1)?.[1]?.body));
    expect(ambiguousPayload.ocr_observation.elements).toHaveLength(2);
    expect(ambiguousPayload).not.toHaveProperty("ocr_roi");
    expect(invoke.mock.calls.filter(([command]) => command === "crop_ocr_grounded_roi")).toHaveLength(roiCropCallsBeforeAmbiguousOcr);
    ambiguousOcr = false;

    await act(async () => { onFrame?.({ payload: { captureSessionId: "stale-capture", sequence: 99, framesReceived: 99, framesProcessed: 99, framesProcessingSkipped: 0, observedAt: 11, width: 640, height: 480, displayGeometry: frame.displayGeometry, changed: true, changedRatio: 1 } }); });
    expect(screen.getByText(/1 received \/ 1 processed \/ 0 processing skips · configured 2 fps · observed 1\.9 fps · cap 30 fps/)).toBeInTheDocument();
    expect(invoke).not.toHaveBeenCalledWith("signal_perception", { signal: "scene_change", pixelChangeRatio: 1 });

    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-a7", sequence: 1, framesReceived: 2, framesProcessed: 1, framesProcessingSkipped: 1, observedAt: 10, width: 640, height: 480, displayGeometry: frame.displayGeometry, changed: false, changedRatio: 0 } }); });
    expect(invoke).not.toHaveBeenCalledWith("latest_screen_frame");
    latestFrame = { ...frame, sequence: 2, observedAt: Date.now() / 1000 };
    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-a7", sequence: 2, framesReceived: 3, framesProcessed: 2, framesProcessingSkipped: 1, observedAt: 10.5, width: 640, height: 480, displayGeometry: frame.displayGeometry, changed: true, changedRatio: 0.4, sceneChange: true, processingP50Ms: 0.5, processingP95Ms: 1.2, opencvProcessingMs: 2.75, captureFpsCeiling: 30, configuredCaptureFps: 20, observedCaptureFps: 18.7 } }); });
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "scene_change", pixelChangeRatio: 0.4 });
    expect(await screen.findByText(/event\/pixel wakeups 0\/1/)).toBeInTheDocument();
    expect(await screen.findByAltText("Latest changed screen frame")).toHaveAttribute("src", frame.dataUrl);
    expect(screen.getByText(/display 7 · bounds \(-640, 0\) 640×480 pt/)).toBeInTheDocument();
    expect(screen.getByText(/target com\.apple\.TextEdit \(PID 445\)/)).toBeInTheDocument();
    expect(screen.getByText(/capture capture-a7 · sequence 2/)).toBeInTheDocument();
    expect(screen.getByText(/Visual change detector p50\/p95: 0.50 \/ 1.20 ms/)).toBeInTheDocument();
    expect(screen.getByText("OpenCV change detection + grounded tracking · cumulative this capture session: 2.75 ms")).toBeInTheDocument();

    trackingSource = "OPENCV_LUCAS_KANADE";
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText("OpenCV live track · frame 3 · confidence 0.91 · bbox updates only; image/VLM refresh paused")).toBeInTheDocument();
    const imageRefreshesBeforeTrack = invoke.mock.calls.filter(([command]) => command === "latest_screen_frame").length;
    const guiGroundingRequestsBeforeTrack = vi.mocked(api).mock.calls
      .filter(([path]) => path === "/computer/perception/gui-ground").length;
    await act(async () => { onTrack?.({ payload: {
      captureSessionId: "capture-a7", targetApplication, sequence: 4, observedAt: Date.now() / 1000,
      width: 640, height: 480, displayGeometry: frame.displayGeometry, active: true, reason: null,
      tracking: { source: "OPENCV_LUCAS_KANADE", bboxNormalized: [0.13, 0.22, 0.33, 0.42], confidence: 0.88, processingMs: 1.7 },
      trackerProcessingP50Ms: 1.2, trackerProcessingP95Ms: 1.7,
    } }); });
    expect(await screen.findByText("OpenCV live track · frame 4 · confidence 0.88 · tracker p50/p95 1.20/1.70 ms · bbox updates only; image/VLM refresh paused")).toBeInTheDocument();
    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-a7", sequence: 4, framesReceived: 5, framesProcessed: 4, framesProcessingSkipped: 1, observedAt: Date.now() / 1000, width: 640, height: 480, displayGeometry: frame.displayGeometry, changed: true, changedRatio: 0.2 } }); });
    expect(invoke.mock.calls.filter(([command]) => command === "latest_screen_frame")).toHaveLength(imageRefreshesBeforeTrack);
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === "/computer/perception/gui-ground"))
      .toHaveLength(guiGroundingRequestsBeforeTrack);
    await act(async () => { onTrack?.({ payload: {
      captureSessionId: "capture-a7", targetApplication, sequence: 5, observedAt: Date.now() / 1000,
      width: 640, height: 480, displayGeometry: frame.displayGeometry, active: false, reason: "OPENCV_TRACK_UPDATE_FAILED", tracking: null,
    } }); });
    expect(await screen.findByText("OpenCV track stopped · OPENCV_TRACK_UPDATE_FAILED")).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "grounded_track_stopped" });
    await act(async () => { onTrack?.({ payload: {
      captureSessionId: "capture-a7", targetApplication, sequence: 4, observedAt: Date.now() / 1000,
      width: 640, height: 480, displayGeometry: frame.displayGeometry, active: true, reason: null,
      tracking: { source: "OPENCV_LUCAS_KANADE", bboxNormalized: [0.13, 0.22, 0.33, 0.42], confidence: 0.88, processingMs: 1.7 },
    } }); });
    const imageRefreshesAfterLoss = invoke.mock.calls.filter(([command]) => command === "latest_screen_frame").length;
    latestFrame = { ...frame, sequence: 6, observedAt: Date.now() / 1000 };
    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-a7", sequence: 6, framesReceived: 7, framesProcessed: 6, framesProcessingSkipped: 1, observedAt: Date.now() / 1000, width: 640, height: 480, displayGeometry: frame.displayGeometry, changed: true, changedRatio: 0.2 } }); });
    await waitFor(() => expect(invoke.mock.calls.filter(([command]) => command === "latest_screen_frame")).toHaveLength(imageRefreshesAfterLoss + 1));
    expect(vi.mocked(api).mock.calls.filter(([path]) => path === "/computer/perception/gui-ground"))
      .toHaveLength(guiGroundingRequestsBeforeTrack);

    trackingCorruption = "target";
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText(/INCONCLUSIVE · GUI_GROUNDING_/)).toBeInTheDocument();

    trackingCorruption = "session";
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText(/INCONCLUSIVE · GUI_GROUNDING_/)).toBeInTheDocument();

    trackingCorruption = "stale";
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText(/INCONCLUSIVE · GUI_GROUNDING_/)).toBeInTheDocument();

    trackingFailure = "OPENCV_TRACKING_INSUFFICIENT_FEATURES";
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Ground target" })); });
    expect(await screen.findByText("INCONCLUSIVE · OPENCV_TRACKING_INSUFFICIENT_FEATURES · frame 6")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Stop screen perception" }));
    await waitFor(() => expect(screen.getByText("Stopped")).toBeInTheDocument());
    expect(invoke).toHaveBeenCalledWith("stop_screen_capture");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/computer/perception/gui-release", { method: "POST" }));
    expect(screen.queryByAltText("Latest changed screen frame")).not.toBeInTheDocument();
  });

  it("shows the initial baseline frame without escalating its full-frame ratio to HIGH", async () => {
    let onFrame: ((event: { payload: { captureSessionId: string; sequence: number; observedAt: number; width: number; height: number; displayGeometry: typeof frame.displayGeometry; initialBaseline: boolean; changed: boolean; changedRatio: number; sceneChange: boolean } }) => void) | undefined;
    const invoke = vi.fn(async (command: string) => {
      if (command === "perception_status" || command === "signal_perception") return { state: "IDLE", reason: "stable_scene", targetFps: 2, captureFpsCeiling: 30, achievableFps: 2, computeLevelTarget: 0, burstAvailable: true };
      if (command === "screen_capture_status") return { running: false, framesReceived: 0, lastFrameAt: null };
      if (command === "start_screen_capture") return status;
      if (command === "latest_screen_frame") return frame;
      throw new Error(`Unexpected command ${command}`);
    });
    const listen = vi.fn(async (name: string, callback: (event: { payload: any }) => void) => {
      if (name === "screen-capture-frame") onFrame = callback as typeof onFrame;
      return vi.fn();
    });
    vi.stubGlobal("window", Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } }));
    vi.mocked(api).mockImplementation(async () => ({ status: "READY" } as never));

    render(<Harness />);
    await waitFor(() => expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function)));
    fireEvent.click(screen.getByRole("button", { name: "Choose capture target" }));
    fireEvent.click(screen.getByRole("button", { name: "Start screen perception" }));
    await waitFor(() => expect(screen.getByText("Running")).toBeInTheDocument());

    await act(async () => { onFrame?.({ payload: {
      captureSessionId: "capture-a7", sequence: 1, observedAt: 10, width: 640, height: 480,
      displayGeometry: frame.displayGeometry, initialBaseline: true, changed: true, changedRatio: 1, sceneChange: false,
    } }); });

    expect(invoke).toHaveBeenCalledWith("signal_perception", { signal: "stable_frame", pixelChangeRatio: 1 });
    expect(invoke).not.toHaveBeenCalledWith("signal_perception", { signal: "pixel_change", pixelChangeRatio: 1 });
    expect(invoke).toHaveBeenCalledWith("latest_screen_frame");
    expect(await screen.findByAltText("Latest changed screen frame")).toHaveAttribute("src", frame.dataUrl);
  });
});
