import { describe, expect, it } from "vitest";
import { parseComputerUseApplicationPath, parseComputerUseIntent, parseComputerUseTarget } from "./computerUseIntent";

describe("native Computer Use chat intent", () => {
  it("recognizes explicit native session start, status, and stop requests", () => {
    expect(parseComputerUseIntent("start computer use")).toBe("start");
    expect(parseComputerUseIntent("コンピュータユーズを開始して")).toBe("start");
    expect(parseComputerUseIntent("computer use status")).toBe("status");
    expect(parseComputerUseIntent("画面操作を停止して")).toBe("stop");
  });

  it("extracts a chat-named target for Computer Use start", () => {
    expect(parseComputerUseTarget("start computer use on My Game")).toBe("My Game");
    expect(parseComputerUseTarget("コンピュータユーズを「計算機」で開始して")).toBe("計算機");
    expect(parseComputerUseTarget("計算機で画面操作を開始して")).toBe("計算機");
    expect(parseComputerUseTarget("start computer use")).toBeNull();
  });

  it("preserves dotted bundle IDs and separates subsequent actions", () => {
    const id = "local.olcr.cu-fixture.20261003-101500-9c066855";
    expect(parseComputerUseTarget(`Start computer use for ${id}`)).toBe(id);
    expect(parseComputerUseTarget(`Start computer use for ${id} and click Start game`)).toBe(id);
    expect(parseComputerUseTarget(`Start computer use for ${id}.`)).toBe(id);
    expect(parseComputerUseTarget("Start computer use for My Game, then click Start")).toBe("My Game");
  });

  it("leaves scraping and project E2E commands to their existing chat routes", () => {
    expect(parseComputerUseIntent("computer use ONの状態で実E2Eを開始して")).toBe("start");
    expect(parseComputerUseIntent("start Playwright E2E at http://127.0.0.1:5173")).toBeNull();
    expect(parseComputerUseIntent("scrape https://example.test with computer use")).toBeNull();
    expect(parseComputerUseIntent("Start an E2E with Computer Use")).toBe("start");
    expect(parseComputerUseIntent("Start local E2E with Computer Use")).toBeNull();
    expect(parseComputerUseIntent("Start a real GUI E2E with Computer Use for Calculator")).toBe("start");
    expect(parseComputerUseIntent("Run a website E2E with Computer Use")).toBeNull();
    expect(parseComputerUseIntent("Run a real GUI E2E with Computer Use on my website")).toBeNull();
    expect(parseComputerUseIntent("Computer Useで自分のサイトのE2Eを開始して")).toBeNull();
  });

  it("extracts a named app from a native E2E start request", () => {
    expect(parseComputerUseTarget("Start a real GUI E2E with Computer Use for Calculator")).toBe("Calculator");
  });

  it("extracts only an explicit absolute or home-relative .app path", () => {
    expect(parseComputerUseApplicationPath('Start a real GUI E2E with Computer Use for "/Users/me/My Game/build/Game.app"'))
      .toBe("/Users/me/My Game/build/Game.app");
    expect(parseComputerUseApplicationPath("実GUI E2Eを開始: ~/Projects/Game/build/Game.app"))
      .toBe("~/Projects/Game/build/Game.app");
    expect(parseComputerUseApplicationPath("Start Computer Use for Calculator")).toBeNull();
  });
});
