import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { ComputerUseCard, type ComputerUseTaskBlock } from "./ComputerUseCard";

const target = { applicationPid: 42, applicationBundleId: "local.example.fixture" };
const activeCapture = { running: true, captureSessionId: "capture-1", targetApplication: target };
const stoppedCapture = { running: false, captureSessionId: null, targetApplication: null };
const block: ComputerUseTaskBlock = {
  type: "computer_use_task",
  status: "RUNNING",
  action: "start",
  capture_session_id: "capture-1",
  application_pid: target.applicationPid,
  application_bundle_id: target.applicationBundleId,
};

afterEach(() => {
  cleanup();
  delete (window as any).__TAURI__;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ComputerUseCard", () => {
  it("keeps transcript receipts without mounting a second live preview", () => {
    const invoke = vi.fn();
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke }, event: { listen: vi.fn() } } });
    render(<ComputerUseCard block={block} view="status" />);
    expect(screen.getByText("Computer Use · STARTED")).toBeInTheDocument();
    expect(screen.queryByLabelText("Live Computer Use view")).not.toBeInTheDocument();
    expect(invoke).not.toHaveBeenCalled();
  });

  it("shows the live target window for the exact running capture session and target", async () => {
    const frame = {
      captureSessionId: "capture-1",
      targetApplication: target,
      sequence: 8,
      width: 640,
      height: 480,
      dataUrl: "data:image/jpeg;base64,ZmFrZQ==",
    };
    const listen = vi.fn().mockResolvedValue(() => undefined);
    const invoke = vi.fn(async (command: string) => command === "screen_capture_status" ? activeCapture : frame);
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke }, event: { listen } },
    });

    render(<ComputerUseCard block={block} />);

    const image = await screen.findByRole("img", { name: "Live window view for local.example.fixture" });
    expect(image).toHaveAttribute("src", frame.dataUrl);
    expect(screen.getByText("LIVE · frame 8")).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("latest_target_window_preview");
    expect(listen).toHaveBeenCalledWith("screen-capture-frame", expect.any(Function));
  });

  it("does not show a frame from another target or capture session", async () => {
    const staleFrame = {
      captureSessionId: "capture-old",
      targetApplication: target,
      sequence: 99,
      width: 640,
      height: 480,
      dataUrl: "data:image/jpeg;base64,c3RhbGU=",
    };
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke: vi.fn(async (command: string) => command === "screen_capture_status" ? activeCapture : staleFrame) }, event: { listen: vi.fn().mockResolvedValue(() => undefined) } },
    });

    render(<ComputerUseCard block={block} />);

    expect(await screen.findByText("Waiting for the target window frame…")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("refreshes the embedded view only for matching capture frame events", async () => {
    let onFrame: ((event: { payload: { captureSessionId: string; sequence: number } }) => void) | undefined;
    const invoke = vi.fn(async (command: string) => command === "screen_capture_status" ? activeCapture : ({
      captureSessionId: "capture-1", targetApplication: target, sequence: invoke.mock.calls.length, width: 640, height: 480,
      dataUrl: "data:image/jpeg;base64,bGl2ZQ==",
    }));
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: {
        core: { invoke },
        event: { listen: vi.fn(async (name: string, callback: typeof onFrame) => { if (name === "screen-capture-frame") onFrame = callback; return () => undefined; }) },
      },
    });
    render(<ComputerUseCard block={block} />);
    await screen.findByRole("img", { name: "Live window view for local.example.fixture" });
    expect(invoke).toHaveBeenCalledTimes(2);

    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-other", sequence: 3 } }); });
    expect(invoke).toHaveBeenCalledTimes(2);
    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-1", sequence: 3 } }); });
    expect(invoke).toHaveBeenCalledTimes(4);
  });

  it("renders native pointer feedback only in its matching live window preview", async () => {
    const callbacks: Record<string, (event: any) => void> = {};
    const geometry = { displayId: 1, originX: 100, originY: 200, widthPoints: 640, heightPoints: 480 };
    const invoke = vi.fn(async (command: string) => command === "screen_capture_status" ? activeCapture : ({
      captureSessionId: "capture-1", targetApplication: target, sequence: 10, width: 640, height: 480,
      displayGeometry: geometry, dataUrl: "data:image/jpeg;base64,bGl2ZQ==",
    }));
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: {
      core: { invoke }, event: { listen: vi.fn(async (name, callback) => {
        callbacks[name] = callback; return () => undefined;
      }) },
    } });
    render(<ComputerUseCard block={block} />);
    await screen.findByRole("img", { name: "Live window view for local.example.fixture" });
    const alignment = { captureSessionId: "capture-1", targetApplication: target, verifiedAt: Date.now() / 1000,
      verifiedSequence: 9, displayGeometry: geometry, mappedGlobalPoint: [420, 440],
      pointerOriginGlobal: [100, 200], pointerCurrentGlobal: [200, 300], distancePoints: 400, targetBboxNormalized: [.4, .4, .6, .6] };
    await act(async () => callbacks["computer-use-pointer"]({ payload: { phase: "PLANNED", alignment: { ...alignment, captureSessionId: "other" } } }));
    expect(screen.queryByLabelText("Virtual cursor")).not.toBeInTheDocument();
    await act(async () => callbacks["computer-use-pointer"]({ payload: { phase: "PLANNED", alignment } }));
    expect(screen.getByLabelText("Virtual cursor")).toBeInTheDocument();
    expect(screen.getByText("操作位置を確認中 · 対象まで 400 pt")).toBeInTheDocument();
    await act(async () => callbacks["computer-use-pointer"]({ payload: { phase: "CLICKED", alignment } }));
    expect(screen.getByText("前回のクリック位置 · 移動距離 400 pt")).toBeInTheDocument();
  });

  it("clears a stale preview when its native capture session has ended", async () => {
    vi.useFakeTimers();
    let statusReads = 0;
    const invoke = vi.fn(async (command: string) => {
      if (command === "screen_capture_status") return ++statusReads === 1 ? activeCapture : stoppedCapture;
      return { captureSessionId: "capture-1", targetApplication: target, sequence: 1, width: 640, height: 480, dataUrl: "data:image/jpeg;base64,bGl2ZQ==" };
    });
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: {
        core: { invoke },
        event: { listen: vi.fn().mockResolvedValue(() => undefined) },
      },
    });
    render(<ComputerUseCard block={block} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByRole("img", { name: "Live window view for local.example.fixture" })).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("Computer Use · SESSION ENDED")).toBeInTheDocument();
    expect(screen.queryByLabelText("Live Computer Use view")).not.toBeInTheDocument();
  });

  it("removes the pinned preview and reconciles its exact session after native stop", async () => {
    const ended = vi.fn();
    Object.defineProperty(window, "__TAURI__", { configurable: true, value: {
      core: { invoke: vi.fn(async () => stoppedCapture) }, event: { listen: vi.fn().mockResolvedValue(() => undefined) },
    } });
    const { container } = render(<ComputerUseCard block={block} view="preview" onSessionEnded={ended} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container).toBeEmptyDOMElement();
    expect(ended).toHaveBeenCalledWith("capture-1");
  });

  it("makes the red window control invoke capture close", async () => {
    const close = vi.fn();
    render(<ComputerUseCard block={block} view="preview" onClose={close} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop capture and close preview" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("clears the old image when the active session has no current preview frame", async () => {
    let onFrame: ((event: { payload: { captureSessionId: string; sequence: number } }) => void) | undefined;
    let previewReads = 0;
    const frame = { captureSessionId: "capture-1", targetApplication: target, sequence: 1, width: 640, height: 480, dataUrl: "data:image/jpeg;base64,bGl2ZQ==" };
    const invoke = vi.fn(async (command: string) => command === "screen_capture_status" ? activeCapture : (++previewReads === 1 ? frame : null));
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: {
        core: { invoke },
        event: { listen: vi.fn(async (name: string, callback: typeof onFrame) => { if (name === "screen-capture-frame") onFrame = callback; return () => undefined; }) },
      },
    });
    render(<ComputerUseCard block={block} />);
    await screen.findByRole("img", { name: "Live window view for local.example.fixture" });

    await act(async () => { onFrame?.({ payload: { captureSessionId: "capture-1", sequence: 2 } }); });

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for the target window frame…")).toBeInTheDocument();
  });
});
