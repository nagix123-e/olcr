import { api, hasSession } from "./api";

export type PerceptionSnapshot = {
  controllerId: string;
  revision: number;
  state: "IDLE" | "ACTIVE" | "HIGH" | "BURST";
  reason: string;
  targetFps: number;
  captureFpsCeiling: number;
  achievableFps: number;
  computeLevelTarget: number;
  burstAvailable: boolean;
  stateDurationsMs?: { idleMs: number; activeMs: number; highMs: number; burstMs: number };
  eventWakeups?: number;
  pixelChangeWakeups?: number;
};

type PersistedWorldState = {
  session_id: string;
  state?: { observation_id?: string };
};

/** Persist advisory APC telemetry only against the exact current observation. */
export async function persistPerceptionPolicy(snapshot: PerceptionSnapshot): Promise<boolean> {
  if (!hasSession()) return false;
  let record: PersistedWorldState;
  try {
    record = await api<PersistedWorldState>("/desktop/world-state");
  } catch (reason) {
    if (String(reason).includes("HTTP_404")) return false;
    throw reason;
  }
  if (!record.session_id || !record.state?.observation_id) return false;
  const policy = {
    controller_id: snapshot.controllerId,
    revision: snapshot.revision,
    state: snapshot.state,
    reason: snapshot.reason,
    target_fps: snapshot.targetFps,
    capture_fps_ceiling: snapshot.captureFpsCeiling,
    achievable_fps: snapshot.achievableFps,
    compute_level_target: snapshot.computeLevelTarget,
    burst_available: snapshot.burstAvailable,
    state_durations_ms: {
      idle_ms: snapshot.stateDurationsMs?.idleMs ?? 0,
      active_ms: snapshot.stateDurationsMs?.activeMs ?? 0,
      high_ms: snapshot.stateDurationsMs?.highMs ?? 0,
      burst_ms: snapshot.stateDurationsMs?.burstMs ?? 0,
    },
    event_wakeups: snapshot.eventWakeups ?? 0,
    pixel_change_wakeups: snapshot.pixelChangeWakeups ?? 0,
  };
  try {
    await api("/desktop/world-state/perception", {
      method: "PUT",
      body: JSON.stringify({ session_id: record.session_id, observation_id: record.state.observation_id, policy }),
    });
    return true;
  } catch (reason) {
    if (String(reason).includes("HTTP_409")) return false;
    throw reason;
  }
}
