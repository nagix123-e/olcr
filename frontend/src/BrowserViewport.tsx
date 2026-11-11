import { useEffect, useState } from "react";
import {useLanguage} from "./language";

type Props = {
  api: (path: string, init?: RequestInit) => Promise<any>;
  sessionId: string;
  active?: boolean;
  selectionRects?: {x: number; y: number; width: number; height: number}[];
  onSelect?: (point: { x: number; y: number }) => void;
};

/** Render the current frame from the task-owned managed Playwright session. */
export function BrowserViewport({ api, sessionId, active = false, onSelect, selectionRects = [] }: Props) {
  const {language}=useLanguage();const ja=language==="ja";
  const [frame, setFrame] = useState<{ data: string; mimeType: string; url?: string } | null>(null);
  const [status, setStatus] = useState("Loading managed browser…");

  // A session switch changes the controlled page identity. Never leave the
  // previous session's pixels visible while the new task frame is loading.
  useEffect(() => {
    setFrame(null);
    setStatus(sessionId ? "Loading managed browser…" : "");
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    let live = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await api(`/web/browser/sessions/${encodeURIComponent(sessionId)}/screenshot`);
        if (!live) return;
        if (result?.status !== "AVAILABLE" || !result?.data || !result?.mime_type) {
          throw new Error("BROWSER_SCREENSHOT_UNAVAILABLE");
        }
        setFrame({ data: result.data, mimeType: result.mime_type, url: result.url });
        setStatus("");
      } catch (error: any) {
        if (live) {
          setFrame(null);
          setStatus(error?.message || "Browser frame unavailable");
        }
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const timer = active ? window.setInterval(() => void refresh(), 2500) : undefined;
    return () => {
      live = false;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [api, sessionId, active]);

  if (!sessionId) return null;
  return <section className="browser-viewport" aria-label={ja?"管理ブラウザーのライブ表示":"Managed browser live view"}>
    <div className="browser-viewport-heading"><b>{ja?"管理ブラウザー":"MANAGED BROWSER"}</b>{frame?.url && <span>{frame.url}</span>}</div>
    {frame
      ? <div style={{position:"relative",lineHeight:0}}><img src={`data:${frame.mimeType};base64,${frame.data}`} alt={ja?"タスク専用ブラウザーの現在のページ":"Current page in the task-owned browser"} style={onSelect ? { cursor: "crosshair" } : undefined} onClick={onSelect ? event => {
          const img = event.currentTarget, bounds = img.getBoundingClientRect();
          onSelect({ x: (event.clientX - bounds.left) * img.naturalWidth / bounds.width, y: (event.clientY - bounds.top) * img.naturalHeight / bounds.height });
        } : undefined} />
        {selectionRects.map((r,i) => <div key={i} style={{position:"absolute",pointerEvents:"none",border:"2px solid #83cc18",background:"rgba(131,204,24,.12)",left:`${r.x}%`,top:`${r.y}%`,width:`${r.width}%`,height:`${r.height}%`}} />)}
      </div>
      : <p role="status">{status}</p>}
    {status && frame && <small role="status">{status}</small>}
  </section>;
}
