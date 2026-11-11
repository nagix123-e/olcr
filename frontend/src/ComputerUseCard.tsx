import { useEffect, useRef, useState } from "react";
import { native } from "./native";
import { VirtualCursor, pointerMatchesFrame, type PointerFeedback, type PointerGeometry } from "./VirtualCursor";
import {useLanguage} from "./language";

export type ComputerUseTaskBlock = {
  type: "computer_use_task";
  status: "RUNNING" | "STOPPED" | "BLOCKED" | "FAILED";
  action: "start" | "stop" | "status";
  capture_session_id?: string | null;
  application_pid?: number | null;
  application_bundle_id?: string | null;
  followup_action?: boolean;
  reason?: string | null;
};

type PreviewFrame = {
  captureSessionId: string;
  targetApplication?: { applicationPid: number; applicationBundleId: string } | null;
  sequence: number;
  width: number;
  height: number;
  dataUrl: string;
  displayGeometry?: PointerGeometry;
};

type FrameNotice = { captureSessionId: string; sequence: number };
type CaptureStatus = {
  running: boolean;
  captureSessionId?: string | null;
  targetApplication?: { applicationPid: number; applicationBundleId: string } | null;
};

export function ComputerUseCard({ block, view = "full", onClose, onSessionEnded }: { block: ComputerUseTaskBlock; view?: "full" | "status" | "preview"; onClose?: () => void; onSessionEnded?: (sessionId: string) => void }) {
  const {language}=useLanguage();const ja=language==="ja";
  const endedCallback = useRef(onSessionEnded);
  endedCallback.current = onSessionEnded;
  const [pointer, setPointer] = useState<PointerFeedback | null>(null);
  const [frame, setFrame] = useState<PreviewFrame | null>(null);
  const [previewState, setPreviewState] = useState<"checking" | "running" | "ended" | "unavailable">("checking");
  const canPreview = view !== "status" && block.status === "RUNNING"
    && !!block.capture_session_id
    && Number.isInteger(block.application_pid)
    && !!block.application_bundle_id;

  useEffect(() => {
    setFrame(null);
    setPointer(null);
    setPreviewState("checking");
    if (!canPreview) return;
    const bridge = native();
    if (!bridge) return;

    let live = true;
    let inFlight = false;
    let lastRefreshAt = 0;
    const refresh = async () => {
      if (!live || inFlight) return;
      inFlight = true;
      try {
        const status = await bridge.core.invoke<CaptureStatus | null>("screen_capture_status");
        if (!live) return;
        if (!status?.running
            || status.captureSessionId !== block.capture_session_id
            || status.targetApplication?.applicationPid !== block.application_pid
            || status.targetApplication?.applicationBundleId !== block.application_bundle_id) {
          setPreviewState("ended");
          setFrame(null);
          setPointer(null);
          endedCallback.current?.(block.capture_session_id!);
          return;
        }
        setPreviewState("running");
        const next = await bridge.core.invoke<PreviewFrame | null>("latest_target_window_preview");
        if (!live) return;
        if (!next) {
          setFrame(null);
          return;
        }
        if (next.captureSessionId !== block.capture_session_id
            || next.targetApplication?.applicationPid !== block.application_pid
            || next.targetApplication?.applicationBundleId !== block.application_bundle_id) {
          setPreviewState("unavailable");
          setFrame(null);
          return;
        }
        setFrame(current => current?.sequence === next.sequence ? current : next);
      } catch {
        // Capture status remains visible; an unavailable preview must not affect actions.
        if (live) {
          setPreviewState("unavailable");
          setFrame(null);
        }
      } finally {
        inFlight = false;
      }
    };
    const onFrame = (event: { payload: FrameNotice }) => {
      if (event.payload.captureSessionId !== block.capture_session_id) return;
      const now = Date.now();
      if (now - lastRefreshAt < 900) return;
      lastRefreshAt = now;
      void refresh();
    };
    void refresh();
    const statusPoll = window.setInterval(() => void refresh(), 1200);
    let unlisten: (() => void) | undefined;
    let unlistenPointer: (() => void) | undefined;
    void bridge.event.listen<PointerFeedback>("computer-use-pointer", event => {
      const a = event.payload?.alignment;
      if (live && a?.captureSessionId === block.capture_session_id
          && a.targetApplication?.applicationPid === block.application_pid
          && a.targetApplication?.applicationBundleId === block.application_bundle_id) {
        setPointer(event.payload);
      }
    }).then(stop => { if (live) unlistenPointer = stop; else stop(); });
    void bridge.event.listen<FrameNotice>("screen-capture-frame", onFrame)
      .then(stop => { if (live) unlisten = stop; else stop(); });
    return () => {
      live = false;
      window.clearInterval(statusPoll);
      unlisten?.();
      unlistenPointer?.();
    };
  }, [block.status, block.capture_session_id, block.application_pid, block.application_bundle_id, canPreview]);

  if (view === "preview" && previewState === "ended") return null;

  return <section className="computer-use-task" aria-label={ja?"コンピューター操作タスク":"Computer Use task"}>
    {view !== "preview" && <div className="computer-use-task-status" role="status">
      <b>Computer Use · {view === "status" && block.status === "RUNNING" ? "STARTED" : block.status === "RUNNING" && previewState === "ended" ? "SESSION ENDED" : block.status}</b>
      {block.application_bundle_id && <small>{block.application_bundle_id} · PID {block.application_pid ?? "?"}</small>}
      {block.capture_session_id && <small>Capture session {block.capture_session_id}</small>}
      {block.status === "RUNNING" && block.followup_action && <small>Continuing with the action requested in chat.</small>}
      {block.reason && <small>{block.reason}</small>}
    </div>}
    {canPreview && previewState !== "ended" && <section className="computer-use-preview" aria-label={ja?"コンピューター操作のライブ表示":"Live Computer Use view"}>
      <header><span className="computer-use-window-controls">{onClose ? <button type="button" className="computer-use-close" aria-label={ja?"画面収録を停止してプレビューを閉じる":"Stop capture and close preview"} onClick={onClose}><i /></button> : <i aria-hidden="true" />}<i aria-hidden="true" /><i aria-hidden="true" /></span>
        <b>{block.application_bundle_id}</b><small>{frame ? `LIVE · frame ${frame.sequence}`
          : previewState === "unavailable" ? ja?("表示を利用できません"):("VIEW UNAVAILABLE") : ja?("ライブ表示を待機中…"):("Waiting for live view…")}</small></header>
      {previewState === "unavailable"
          ? <div className="computer-use-preview-placeholder" role="status">{ja?"ウィンドウのライブ表示を一時的に利用できません。":"The live window view is temporarily unavailable."}</div>
        : frame
        ? <div className="computer-use-preview-frame" style={{ maxWidth: `${340 * frame.width / frame.height}px` }}>
          <img src={frame.dataUrl} alt={`Live window view for ${block.application_bundle_id}`} />
          {pointer && pointerMatchesFrame(pointer, frame) && <VirtualCursor pointer={pointer} width={frame.width} height={frame.height} />}
        </div>
        : <div className="computer-use-preview-placeholder" role="status">{ja?"対象ウィンドウのフレームを待機中…":"Waiting for the target window frame…"}</div>}
    </section>}
  </section>;
}
