import { describe, expect, it, vi } from "vitest";
import { resolveComputerUseTarget, resolveOrLaunchComputerUseTarget } from "./computerUseTarget";

const apps = [
  { applicationPid: 12, applicationName: "My Game", applicationBundleId: "com.example.game" },
  { applicationPid: 13, applicationName: "Calculator", applicationBundleId: "com.apple.Calculator" },
];

describe("resolveComputerUseTarget", () => {
  it("resolves a named running app without a per-app settings grant", () => {
    expect(resolveComputerUseTarget(apps, [], "My Game")).toEqual({
      status: "READY", target: { applicationPid: 12, applicationBundleId: "com.example.game" },
    });
  });
  it("auto-selects only when exactly one non-OLCR app is running", () => {
    expect(resolveComputerUseTarget([apps[0]]).status).toBe("READY");
    expect(resolveComputerUseTarget(apps)).toMatchObject({
      status: "BLOCKED", reason: "TARGET_APPLICATION_AMBIGUOUS",
    });
  });
  it("requires an explicit target when several apps run and reports stopped display names", () => {
    const stopped = resolveComputerUseTarget(apps, [], "Safari");
    const byBundle = resolveComputerUseTarget([], [], "com.example.game");
    expect(stopped.status === "BLOCKED" && stopped.reason).toBe("TARGET_APPLICATION_NOT_RUNNING");
    expect(byBundle.status === "LAUNCH_REQUIRED" && byBundle.applicationBundleId).toBe("com.example.game");
  });
  it("launches a chat-named bundle after backend authorization and binds its live PID", async () => {
    const inventories = [[], [{ applicationPid: 41, applicationName: "Calculator", applicationBundleId: "com.apple.Calculator" }]];
    const calls: string[] = [];
    const bridge = { core: { invoke: async (command: string) => {
      calls.push(command);
      if (command === "list_running_accessibility_applications") return inventories.shift() || [];
      return undefined;
    } } };
    const api = async (path: string, init?: RequestInit) => {
      calls.push(`${path}:${init?.method}`);
      return { status: "AUTHORIZED", application_bundle_id: "com.apple.Calculator" };
    };
    await expect(resolveOrLaunchComputerUseTarget({ bridge, api,
      requestedName: "com.apple.Calculator" })).resolves.toEqual({
      applicationPid: 41, applicationBundleId: "com.apple.Calculator",
    });
    expect(calls).toEqual([
      "list_running_accessibility_applications",
      "/computer-use/authorize-launch:POST",
      "launch_computer_use_application",
      "list_running_accessibility_applications",
    ]);
  });
  it("does not launch when backend authorization returns another bundle", async () => {
    const launched: string[] = [];
    const bridge = { core: { invoke: async (command: string) => {
      if (command === "launch_computer_use_application") launched.push(command);
      return [];
    } } };
    const api = async () => ({ status: "DENIED", application_bundle_id: "com.example.other" });
    await expect(resolveOrLaunchComputerUseTarget({ bridge, api,
      requestedName: "com.example.other" })).rejects.toThrow("COMPUTER_USE_LAUNCH_SCOPE_NOT_AUTHORIZED");
    expect(launched).toEqual([]);
  });

  it("authorizes and launches an exact project app path, then binds the matching live path", async () => {
    const inventories = [[], [{ applicationPid: 72, applicationName: "Game",
      applicationBundleId: "local.example.game", applicationPath: "/workspace/build/Game.app" }]];
    const calls: Array<[string, any]> = [];
    const bridge = { core: { invoke: async (command: string, args?: any) => {
      calls.push([command, args]);
      if (command === "list_running_accessibility_applications") return inventories.shift() || [];
      return undefined;
    } } };
    const api = async (_path: string, init?: RequestInit) => {
      calls.push(["authorize", JSON.parse(String(init?.body))]);
      return { status: "AUTHORIZED", application_bundle_id: "local.example.game",
        application_path: "/workspace/build/Game.app" };
    };
    await expect(resolveOrLaunchComputerUseTarget({ bridge, api,
      allowedBundleIds: ["local.example.game"], requestedApplicationPath: "~/repo/build/Game.app",
      projectId: "project-1" })).resolves.toEqual({ applicationPid: 72, applicationBundleId: "local.example.game" });
    expect(calls).toEqual([
      ["authorize", { application_path: "~/repo/build/Game.app", project_id: "project-1" }],
      ["list_running_accessibility_applications", undefined],
      ["launch_computer_use_application_at_path", { applicationBundleId: "local.example.game",
        applicationPath: "/workspace/build/Game.app" }],
      ["list_running_accessibility_applications", undefined],
    ]);
  });

  it("refuses to attach to a same-ID app running from a different path", async () => {
    const bridge = { core: { invoke: async () => [{ applicationPid: 19, applicationName: "Game",
      applicationBundleId: "local.example.game", applicationPath: "/other/Game.app" }] } };
    const api = async () => ({ status: "AUTHORIZED", application_bundle_id: "local.example.game",
      application_path: "/workspace/Game.app" });
    await expect(resolveOrLaunchComputerUseTarget({ bridge, api, allowedBundleIds: ["local.example.game"],
      requestedApplicationPath: "/workspace/Game.app", projectId: "project-1" }))
      .rejects.toThrow("COMPUTER_USE_APPLICATION_ALREADY_RUNNING_FROM_DIFFERENT_PATH");
  });
});

it("does not require a settings grant before attaching to a running app", async () => {
  const invoke = vi.fn(async (command: string) => command === "list_running_accessibility_applications"
    ? [{ applicationPid: 99, applicationName: "Calculator", applicationBundleId: "com.apple.Calculator" }] : undefined);
  const result = await resolveOrLaunchComputerUseTarget({ bridge: { core: { invoke } },
    api: async () => ({ status: "AUTHORIZED", application_bundle_id: "com.apple.Calculator" }),
    requestedName: "Calculator" });
  expect(result.applicationPid).toBe(99);
});
