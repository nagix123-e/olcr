import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
// These interaction fixtures deliberately exercise the English UI.
vi.mock("./language", () => ({ useLanguage: () => ({ language: "en", setLanguage: vi.fn() }) }));
import { BrowserViewport } from "./BrowserViewport";

afterEach(() => cleanup());

describe("BrowserViewport", () => {
  it("loads and refreshes a screenshot from the task-owned session", async () => {
    const api = vi.fn().mockResolvedValue({
      status: "AVAILABLE", mime_type: "image/png", data: "cG5n",
      url: "http://127.0.0.1:5173/game",
    });
    render(<BrowserViewport api={api} sessionId="local-session" />);

    const image = await screen.findByRole("img", { name: "Current page in the task-owned browser" });
    expect(image).toHaveAttribute("src", "data:image/png;base64,cG5n");
    expect(screen.getByText("http://127.0.0.1:5173/game")).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith("/web/browser/sessions/local-session/screenshot");
  });

  it("polls only while the task remains active", async () => {
    vi.useFakeTimers();
    const api = vi.fn().mockResolvedValue({ status: "AVAILABLE", mime_type: "image/png", data: "cG5n" });
    try {
      render(<BrowserViewport api={api} sessionId="local-session" active />);
      await act(async () => { await Promise.resolve(); });
      expect(api).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
      expect(api).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the previous task screenshot immediately when the session changes", async () => {
    const api = vi.fn()
      .mockResolvedValueOnce({ status: "AVAILABLE", mime_type: "image/png", data: "b2xk", url: "http://127.0.0.1/old" })
      .mockImplementationOnce(() => new Promise(() => {}));
    const view = render(<BrowserViewport api={api} sessionId="old-session" />);
    expect(await screen.findByRole("img", { name: "Current page in the task-owned browser" }))
      .toHaveAttribute("src", "data:image/png;base64,b2xk");

    view.rerender(<BrowserViewport api={api} sessionId="new-session" />);

    expect(screen.queryByRole("img", { name: "Current page in the task-owned browser" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading managed browser");
    expect(api).toHaveBeenLastCalledWith("/web/browser/sessions/new-session/screenshot");
  });

  it("clears a stale screenshot when the current session cannot provide a fresh frame", async () => {
    vi.useFakeTimers();
    const api = vi.fn()
      .mockResolvedValueOnce({ status: "AVAILABLE", mime_type: "image/png", data: "b2xk" })
      .mockRejectedValueOnce(new Error("BROWSER_SESSION_NOT_FOUND"));
    try {
      render(<BrowserViewport api={api} sessionId="local-session" active />);
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByRole("img", { name: "Current page in the task-owned browser" })).toBeInTheDocument();

      await act(async () => { await vi.advanceTimersByTimeAsync(2500); });

      expect(screen.queryByRole("img", { name: "Current page in the task-owned browser" })).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("BROWSER_SESSION_NOT_FOUND");
    } finally {
      vi.useRealTimers();
    }
  });
});

it("maps a list selection to CSS screenshot coordinates", async () => {
  const api = vi.fn().mockResolvedValue({status:"AVAILABLE",mime_type:"image/png",data:"frame"});
  const onSelect = vi.fn();
  render(<BrowserViewport api={api} sessionId="selection" onSelect={onSelect} />);
  const img = await screen.findByRole("img");
  Object.defineProperty(img, "naturalWidth", {value:1280});
  Object.defineProperty(img, "naturalHeight", {value:720});
  vi.spyOn(img,"getBoundingClientRect").mockReturnValue({left:100,top:50,width:640,height:360,right:740,bottom:410,x:100,y:50,toJSON:()=>({})});
  img.dispatchEvent(new MouseEvent("click", {bubbles:true,clientX:420,clientY:230}));
  expect(onSelect).toHaveBeenCalledWith({x:640,y:360});
});
