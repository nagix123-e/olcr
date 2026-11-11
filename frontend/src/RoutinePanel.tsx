import { useEffect, useId, useMemo, useState } from "react";
import {useLanguage} from "./language";

type RoutineApi = (path: string, init?: RequestInit) => Promise<any>;
type Trace = { id: string; action: string; goal_signature: string; captured_at: number };
type Candidate = {
  id: string; name: string; status: "CANDIDATE" | "SUSPECT"; validated: boolean; source_trace_ids: string[];
  run_stats: { runs: number; verified_successes: number; failures: number };
  review: { decision: string; reviewed_at: number } | null;
  action: { kind?: string; mechanism?: string } | null;
  context: { source?: string; application_bundle_id?: string } | null;
  preconditions: Array<Record<string, unknown>>;
  expected_effects: Array<Record<string, unknown>>;
  actions: Array<{ mechanism?: string }>;
};
type Inventory = { traces: Trace[]; candidates: Candidate[] };

function normalizeInventory(result: any): Inventory {
  const traces = Array.isArray(result?.traces) ? result.traces.flatMap((item: any) => {
    if (!item || typeof item.id !== "string" || typeof item.action !== "string"
        || typeof item.goal_signature !== "string" || typeof item.captured_at !== "number"
        || !Number.isFinite(item.captured_at)) return [];
    return [{ id: item.id, action: item.action, goal_signature: item.goal_signature, captured_at: item.captured_at }];
  }) : [];
  const candidates = Array.isArray(result?.candidates) ? result.candidates.flatMap((item: any) => {
    if (!item || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const stats = item.run_stats && typeof item.run_stats === "object" ? item.run_stats : {};
    const nonnegativeInteger = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0
      ? Number(value) : 0;
    return [{ id: item.id, name: item.name,
      status: item.status === "SUSPECT" ? "SUSPECT" as const : "CANDIDATE" as const,
      validated: item.validated === true,
      run_stats: { runs: nonnegativeInteger(stats.runs),
        verified_successes: nonnegativeInteger(stats.verified_successes),
        failures: nonnegativeInteger(stats.failures) },
      source_trace_ids: Array.isArray(item.source_trace_ids)
        ? item.source_trace_ids.filter((id: unknown): id is string => typeof id === "string") : [],
      review: item.review && typeof item.review.decision === "string"
        ? { decision: item.review.decision, reviewed_at: Number(item.review.reviewed_at) || 0 } : null,
      action: item.action && typeof item.action === "object" ? item.action : null,
      context: item.context && typeof item.context === "object" ? item.context : null,
      preconditions: Array.isArray(item.preconditions) ? item.preconditions : [],
      expected_effects: Array.isArray(item.expected_effects) ? item.expected_effects : [],
      actions: Array.isArray(item.actions) ? item.actions.filter((action: any) => action && typeof action === "object") : [] }];
  }) : [];
  return { traces, candidates };
}

export function RoutinePanel({ api }: { api: RoutineApi }) {
  const {language}=useLanguage();const ja=language==="ja";
  const controlId = useId();
  const [inventory, setInventory] = useState<Inventory>({ traces: [], candidates: [] });
  const [selected, setSelected] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [matchGoal, setMatchGoal] = useState("");

  const refresh = async () => {
    setInventory(normalizeInventory(await api("/routines/candidates")));
  };

  useEffect(() => {
    let live = true;
    void api("/routines/candidates").then((result) => {
      if (live) setInventory(normalizeInventory(result));
    }).catch((error: unknown) => {
      if (live) setStatus(error instanceof Error ? error.message : String(error));
    });
    return () => { live = false; };
  }, [api]);

  const selectedTraces = useMemo(
    () => inventory.traces.filter((trace) => selected.includes(trace.id)),
    [inventory.traces, selected],
  );
  const signature = selectedTraces[0]?.goal_signature;
  const action = selectedTraces[0]?.action;
  const compatible = selectedTraces.length >= 2 && selectedTraces.every(
    (trace) => trace.goal_signature === signature && trace.action === action,
  );

  const toggle = (trace: Trace) => {
    setSelected((current) => {
      if (current.includes(trace.id)) return current.filter((id) => id !== trace.id);
      if (current.length >= 8) return current;
      if (current.length && (trace.goal_signature !== signature || trace.action !== action)) return current;
      return [...current, trace.id];
    });
    setStatus("");
  };

  const compile = async () => {
    if (!compatible || busy) return;
    setBusy(true);
    setStatus("");
    try {
      const result = await api("/routines/candidates/compile", {
        method: "POST",
        body: JSON.stringify({ trace_ids: selected }),
      });
      await refresh();
      setSelected([]);
      setStatus(ja?`候補案を作成しました: ${result.procedure?.name || "検証済みアクション"}`:`Draft candidate created: ${result.procedure?.name || "verified action"}.`);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const review = async (candidate: Candidate, decision: "APPROVED" | "REJECTED") => {
    if (busy) return;
    setBusy(true);
    setStatus("");
    try {
      await api(`/routines/candidates/${encodeURIComponent(candidate.id)}/review`, {
        method: "POST", body: JSON.stringify({ decision }),
      });
      await refresh();
      setStatus(ja?(decision === "APPROVED"?"承認を記録しました。候補は実行できません。":"候補の却下を記録しました。"):(decision === "APPROVED" ? "Review approval recorded; candidate remains non-executable." : "Candidate rejection recorded."));
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const match = async () => {
    if (!matchGoal.trim() || busy) return;
    setBusy(true);
    setStatus("");
    try {
      const result = await api("/routines/match", {
        method: "POST", body: JSON.stringify({ question: matchGoal }),
      });
      const candidate = result?.matches?.[0];
      setStatus(ja?(result?.status === "CANDIDATE_MATCHED"?`候補（${candidate?.procedure_id || "ルーチン"}）が${candidate?.element_id || "観測対象"}に一致しました。通常のプランナーによる確認が必要で、アクションは実行されていません。`:`安全なルーチン候補がありません（${result?.reason || "理由不明"}）。通常のプランニングを続けてください。`):(result?.status === "CANDIDATE_MATCHED"?`Candidate matched (${candidate?.procedure_id || "routine"}) to ${candidate?.element_id || "the observed target"}; planner fallback is still required and no action ran.`:`No safe routine match (${result?.reason || "unknown reason"}); continue with normal planning.`));
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return <section className="routine-workspace" aria-labelledby={`${controlId}-routine-title`}>
    <div className="routine-header">
      <div><p className="eyebrow">{ja?"検証済みアクション":"VERIFIED ACTIONS"}</p><h2 id={`${controlId}-routine-title`}>{ja?"ルーチンの記録":"Routine evidence"}</h2></div>
      <button type="button" disabled={busy} onClick={() => { setStatus(""); void refresh().catch((error: unknown) => setStatus(error instanceof Error ? error.message : String(error))); }}>{ja?"更新":"Refresh"}</button>
    </div>
    <p className="routine-note">{ja?"確認済みアクションの記録を表示します。同じアクションの記録を2〜8件選ぶと、レビュー用候補を作成できます。承認・却下を記録しても、候補は有効化・実行されません。":"Only confirmed action traces appear here. Select 2–8 traces of the same action to compile a reviewable candidate. Review records approval or rejection; approval does not activate or execute it."}</p>

    <section className="routine-section" aria-labelledby={`${controlId}-routine-match-title`}>
      <div className="routine-subhead"><h3 id={`${controlId}-routine-match-title`}>{ja?"承認済み候補を確認":"Check an approved candidate"}</h3></div>
      <p className="muted">{ja?"同じ目標を最新のデスクトップ状態と照合します。一致しても候補の提案にとどまり、通常のプランニングを行います。":"Compare an exact repeated goal with the latest fresh saved desktop World State. A match remains a non-executable proposal and normal planning still runs."}</p>
      <label className="routine-trace">{ja?"目標":"Goal"}
        <input aria-label="Goal to match against World State" value={matchGoal} maxLength={1000}
          onChange={(event) => setMatchGoal(event.target.value)} />
      </label>
      <button type="button" disabled={busy || !matchGoal.trim()} onClick={() => void match()}>
        {ja?(busy?"確認中…":"現在の状態を確認"):(busy ? "Checking…" : "Check current World State")}
      </button>
    </section>

    <section className="routine-section" aria-labelledby={`${controlId}-routine-traces-title`}>
      <div className="routine-subhead"><h3 id={`${controlId}-routine-traces-title`}>{ja?"確認済みの記録":"Confirmed traces"}</h3><span>{inventory.traces.length}</span></div>
      {inventory.traces.length === 0 ? <p className="muted">{ja?"確認済みの記録はまだありません。":"No confirmed traces have been captured yet."}</p> : <ul className="routine-list">
        {inventory.traces.map((trace) => {
          const isSelected = selected.includes(trace.id);
          const disabled = !isSelected && (selected.length >= 8 || Boolean(selected.length && (trace.action !== action || trace.goal_signature !== signature)));
          return <li key={trace.id}>
            <label className="routine-trace">
              <input type="checkbox" checked={isSelected} disabled={disabled || busy} onChange={() => toggle(trace)} />
              <span><strong>{trace.action.replaceAll("_", " ")}</strong><small>{new Date(trace.captured_at * 1000).toLocaleString()}</small></span>
              <code title={trace.goal_signature}>{trace.goal_signature.slice(0, 12)}</code>
            </label>
          </li>;
        })}
      </ul>}
      <div className="routine-actions">
        <span>{ja?`${selected.length}/8件を選択`:`${selected.length}/8 selected`}</span>
        <button type="button" disabled={!compatible || busy} onClick={() => void compile()}>{ja?(busy?"作成中…":"候補を作成"):(busy ? "Compiling…" : "Compile candidate")}</button>
        <button type="button" disabled={!selected.length || busy} onClick={() => setSelected([])}>{ja?"選択を解除":"Clear selection"}</button>
      </div>
    </section>

    <section className="routine-section" aria-labelledby={`${controlId}-routine-candidates-title`}>
      <div className="routine-subhead"><h3 id={`${controlId}-routine-candidates-title`}>{ja?"候補案":"Draft candidates"}</h3><span>{inventory.candidates.length}</span></div>
      {inventory.candidates.length === 0 ? <p className="muted">{ja?"ルーチン候補はまだありません。":"No routine candidates have been compiled."}</p> : <ul className="routine-list">
        {inventory.candidates.map((candidate) => <li className="routine-candidate" key={candidate.id}>
          <strong>{candidate.name}</strong>
          <span>{candidate.status === "SUSPECT" ? "Suspect · automatic matching disabled" : candidate.review?.decision === "APPROVED" ? "Approved · not executable" : candidate.review?.decision === "REJECTED" ? "Rejected · not executable" : "Awaiting review · not executable"}</span>
          <small>{candidate.source_trace_ids.length} source traces · {candidate.context?.source || "unknown source"}{candidate.context?.application_bundle_id ? ` · ${candidate.context.application_bundle_id}` : ""}</small>
          <small>Action: {candidate.action?.kind || "unknown"} via {candidate.actions[0]?.mechanism || "unknown route"}</small>
          {candidate.run_stats.runs > 0 && <small>Verified uses: {candidate.run_stats.verified_successes} · recorded failures: {candidate.run_stats.failures}</small>}
          <small>Precondition: {JSON.stringify(candidate.preconditions[0] || {})}</small>
          <small>Expected effect: {JSON.stringify(candidate.expected_effects[0] || {})}</small>
          {candidate.status === "SUSPECT"
            ? <small>Compile a replacement from new confirmed traces to review this routine again.</small>
            : <div className="routine-actions">
              <button type="button" disabled={busy} onClick={() => void review(candidate, "APPROVED")}>{ja?"承認":"Approve review"}</button>
              <button type="button" disabled={busy} onClick={() => void review(candidate, "REJECTED")}>{ja?"却下":"Reject"}</button>
            </div>}
        </li>)}
      </ul>}
    </section>
    <p className="routine-status" role="status">{status}</p>
  </section>;
}
