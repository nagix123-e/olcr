import { afterEach, describe, expect, it, vi } from "vitest";
import { api, hasSession } from "./api";
import { persistPerceptionPolicy, type PerceptionSnapshot } from "./perception";

vi.mock("./api", () => ({ api: vi.fn(), hasSession: vi.fn() }));

const snapshot: PerceptionSnapshot = {
  controllerId: "a1b2c3d4e5f60718", revision: 3, state: "HIGH", reason: "significant_pixel_motion",
  targetFps: 20, captureFpsCeiling: 2, achievableFps: 2, computeLevelTarget: 2, burstAvailable: true,
  eventWakeups: 2, pixelChangeWakeups: 5,
};

describe("persistPerceptionPolicy", () => {
  afterEach(() => vi.clearAllMocks());

  it("does not request the backend before GUI session authentication is ready", async () => {
    vi.mocked(hasSession).mockReturnValue(false);
    await expect(persistPerceptionPolicy(snapshot)).resolves.toBe(false);
    expect(api).not.toHaveBeenCalled();
  });

  it("attaches APC telemetry only to the latest observation identity", async () => {
    vi.mocked(hasSession).mockReturnValue(true);
    vi.mocked(api)
      .mockResolvedValueOnce({ session_id: "macos-ax-1", state: { observation_id: "obs-1" } } as never)
      .mockResolvedValueOnce({} as never);

    await expect(persistPerceptionPolicy(snapshot)).resolves.toBe(true);

    expect(api).toHaveBeenNthCalledWith(1, "/desktop/world-state");
    expect(api).toHaveBeenNthCalledWith(2, "/desktop/world-state/perception", {
      method: "PUT",
      body: JSON.stringify({
        session_id: "macos-ax-1", observation_id: "obs-1",
        policy: {
          controller_id: "a1b2c3d4e5f60718", revision: 3, state: "HIGH", reason: "significant_pixel_motion",
          target_fps: 20, capture_fps_ceiling: 2, achievable_fps: 2, compute_level_target: 2, burst_available: true,
          state_durations_ms: { idle_ms: 0, active_ms: 0, high_ms: 0, burst_ms: 0 },
          event_wakeups: 2, pixel_change_wakeups: 5,
        },
      }),
    });
  });

  it("treats missing or changed observations as an advisory update that must be retried on the next transition", async () => {
    vi.mocked(hasSession).mockReturnValue(true);
    vi.mocked(api).mockRejectedValueOnce(new Error("HTTP_404: DESKTOP_WORLD_STATE_NOT_FOUND"));
    await expect(persistPerceptionPolicy(snapshot)).resolves.toBe(false);

    vi.mocked(api)
      .mockResolvedValueOnce({ session_id: "macos-ax-1", state: { observation_id: "obs-1" } } as never)
      .mockRejectedValueOnce(new Error("HTTP_409: DESKTOP_WORLD_STATE_OBSERVATION_STALE"));
    await expect(persistPerceptionPolicy(snapshot)).resolves.toBe(false);
  });
});
