import {describe, expect, it} from "vitest";
import {chatWorkspaceCommand} from "./chatWorkspaceCommand";
describe("chat management requests", () => {
  it.each([
    ["設定を開いて", "/settings"], ["open settings", "/settings"],
    ["スクレイピングの管理を開いて", "/scrape manage"],
    ["ルーチン一覧を表示して", "/routines"],
    ["スクレイピングをオンにして", "/scrape on"],
    ["ローカルE2Eをオフにして", "/e2e off"],
  ])("routes %s to %s", (text, command) => expect(chatWorkspaceCommand(text)).toBe(command));
  it.each(["設定とは何ですか", "Explain routines", "スクレイピングで設定を開いている場合", "show settings and delete everything"])(
    "does not claim explanatory or ambiguous prose: %s", text => expect(chatWorkspaceCommand(text)).toBeNull());
});
