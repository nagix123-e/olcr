export type RunningComputerUseApplication = {
  applicationPid: number;
  applicationName: string;
  applicationBundleId: string;
  applicationPath?: string | null;
};

export type ComputerUseTargetResolution =
  | { status: "READY"; target: Pick<RunningComputerUseApplication, "applicationPid" | "applicationBundleId"> }
  | { status: "LAUNCH_REQUIRED"; applicationBundleId: string }
  | { status: "BLOCKED"; reason: "NO_TARGET_RUNNING" | "TARGET_APPLICATION_NOT_RUNNING" | "TARGET_APPLICATION_AMBIGUOUS"; candidates?: string[] };

type ComputerUseLaunchBridge = {
  core: { invoke: (command: string, args?: Record<string, unknown>) => Promise<any> };
};
type ComputerUseLaunchApi = (path: string, init?: RequestInit) => Promise<any>;

/** Resolve a chat-named target against live native app identities. */
export function resolveComputerUseTarget(
  applications: RunningComputerUseApplication[],
  _allowedBundleIds: string[] = [],
  requestedName?: string | null,
): ComputerUseTargetResolution {
  const live = applications.filter(app => Number.isSafeInteger(app.applicationPid) && app.applicationPid > 0
    && app.applicationBundleId.trim()
    && app.applicationBundleId !== "local.olcr.desktop");
  const query = requestedName?.trim().toLocaleLowerCase();
  if (!live.length) {
    if (query && /^[a-z0-9][a-z0-9._-]{1,254}$/i.test(requestedName || "") && (requestedName || "").includes(".")) {
      return { status: "LAUNCH_REQUIRED", applicationBundleId: requestedName!.trim() };
    }
    return { status: "BLOCKED", reason: query ? "TARGET_APPLICATION_NOT_RUNNING" : "NO_TARGET_RUNNING" };
  }
  if (query) {
    const exact = live.filter(app => app.applicationName.trim().toLocaleLowerCase() === query
      || app.applicationBundleId.toLocaleLowerCase() === query);
    const matches = exact.length ? exact : live.filter(app => app.applicationName.toLocaleLowerCase().includes(query));
    if (!matches.length) return { status: "BLOCKED", reason: "TARGET_APPLICATION_NOT_RUNNING" };
    if (matches.length > 1) return { status: "BLOCKED", reason: "TARGET_APPLICATION_AMBIGUOUS", candidates: matches.map(app => app.applicationName) };
    const app = matches[0];
    return { status: "READY", target: { applicationPid: app.applicationPid, applicationBundleId: app.applicationBundleId } };
  }
  if (live.length > 1) return { status: "BLOCKED", reason: "TARGET_APPLICATION_AMBIGUOUS", candidates: live.map(app => app.applicationName) };
  return { status: "READY", target: { applicationPid: live[0].applicationPid, applicationBundleId: live[0].applicationBundleId } };
}

/** Resolve a running app or launch an explicitly named bundle ID / project app. */
export async function resolveOrLaunchComputerUseTarget({
  bridge, api, allowedBundleIds = [], requestedName, requestedApplicationPath, projectId,
}: {
  bridge: ComputerUseLaunchBridge;
  api: ComputerUseLaunchApi;
  allowedBundleIds?: string[];
  requestedName?: string | null;
  requestedApplicationPath?: string | null;
  projectId?: string | null;
}): Promise<Pick<RunningComputerUseApplication, "applicationPid" | "applicationBundleId">> {
  const list = () => bridge.core.invoke("list_running_accessibility_applications") as Promise<RunningComputerUseApplication[]>;
  if (requestedApplicationPath) {
    if (!projectId) throw new Error("COMPUTER_USE_PROJECT_APP_REQUIRES_PROJECT");
    const authorization = await api("/computer-use/authorize-launch", {
      method: "POST",
      body: JSON.stringify({ application_path: requestedApplicationPath, project_id: projectId }),
    });
    const bundleId = String(authorization?.application_bundle_id || "");
    const applicationPath = String(authorization?.application_path || "");
    if (authorization?.status !== "AUTHORIZED" || !bundleId || !applicationPath
      || bundleId === "local.olcr.desktop") {
      throw new Error("COMPUTER_USE_LAUNCH_SCOPE_NOT_AUTHORIZED");
    }
    let applications = await list();
    const alreadyRunning = applications.find(app => app.applicationBundleId === bundleId);
    if (alreadyRunning) {
      if (alreadyRunning.applicationPath !== applicationPath) {
        throw new Error("COMPUTER_USE_APPLICATION_ALREADY_RUNNING_FROM_DIFFERENT_PATH");
      }
      return { applicationPid: alreadyRunning.applicationPid, applicationBundleId: bundleId };
    }
    await bridge.core.invoke("launch_computer_use_application_at_path", {
      applicationBundleId: bundleId,
      applicationPath,
    });
    for (let attempt = 0; attempt < 120; attempt += 1) {
      applications = await list();
      const launched = applications.find(app => app.applicationBundleId === bundleId);
      if (launched) {
        if (launched.applicationPath !== applicationPath) {
          throw new Error("COMPUTER_USE_APPLICATION_LAUNCHED_FROM_DIFFERENT_PATH");
        }
        return { applicationPid: launched.applicationPid, applicationBundleId: bundleId };
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error(`COMPUTER_USE_APPLICATION_LAUNCH_TIMEOUT:${bundleId}`);
  }
  let applications = await list();
  const resolved = resolveComputerUseTarget(applications, allowedBundleIds, requestedName);
  if (resolved.status === "READY") return resolved.target;
  if (resolved.status === "BLOCKED") {
    throw new Error(`${resolved.reason}${resolved.candidates?.length ? `:${resolved.candidates.join(", ")}` : ""}`);
  }

  const authorization = await api("/computer-use/authorize-launch", {
    method: "POST",
    body: JSON.stringify({ application_bundle_id: resolved.applicationBundleId }),
  });
  if (authorization?.status !== "AUTHORIZED"
      || authorization?.application_bundle_id !== resolved.applicationBundleId) {
    throw new Error("COMPUTER_USE_LAUNCH_SCOPE_NOT_AUTHORIZED");
  }
  await bridge.core.invoke("launch_computer_use_application", {
    applicationBundleId: resolved.applicationBundleId,
  });

  // Launch Services is asynchronous. Re-read the native inventory until the
  // requested bundle obtains a live PID, then bind capture to that PID.
  for (let attempt = 0; attempt < 120; attempt += 1) {
    applications = await list();
    const launched = applications.find(app => app.applicationBundleId === resolved.applicationBundleId
      && Number.isSafeInteger(app.applicationPid) && app.applicationPid > 0);
    if (launched) return { applicationPid: launched.applicationPid,
      applicationBundleId: launched.applicationBundleId };
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`COMPUTER_USE_APPLICATION_LAUNCH_TIMEOUT:${resolved.applicationBundleId}`);
}
