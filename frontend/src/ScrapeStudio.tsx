import { useEffect, useId, useState } from "react";
import { ScrapeDataTable } from "./ScrapeDataTable";
import { BrowserViewport } from "./BrowserViewport";
import {useLanguage} from "./language";

type BrowserSession = { session_id: string; url: string; last_activity: number; profile_id?: string | null; interactive_login?: boolean };
type BrowserProfile = { profile_id: string; in_use: boolean };
type PageEvidence = { title?: string; url?: string; clean_markdown?: string; warnings?: string[] };
type NetworkEvidence = { index: number; method: string; url: string; status_code?: number | null; status_text?: string };
type NetworkJsonEvidence = { request_index: number; url: string; status_code: number; content_type: string; preview: unknown; truncated: boolean; persisted: false };
type PickerCandidate = { kind: string; tag: string; role?: string; text: string; attributes: Record<string, string>; bbox: { x:number; y:number; width:number; height:number }; dom_path: string; selector_candidates: string[]; relative_selector?: string };
type FieldMapping = { name: string; selector: string; extract: "text" | "href" };
type SavedRecipe = { id: string; name: string; status: string; record_selector: string; source?: string; updated_at?: number; start?: { url?: string }; browser_profile_id?: string | null };
type RecipeFieldDefinition = { selector: string; extract: string; attribute?: string | null };
type RecoveryFieldMapping = { name: string; selector: string; extract: string; attribute: string | null };
type RecipeDefinition = {
  id: string; name: string; start?: { url?: string }; record_selector: string; browser_profile_id?: string | null;
  fields: Record<string, RecipeFieldDefinition>;
  navigation?: Record<string, unknown>;
  detail?: { link_field: string; record_selector: string; fields: Record<string, RecipeFieldDefinition> } | null;
  wait_strategy?: string; deduplicate_keys?: string[]; max_pages?: number; max_records?: number;
  max_duration_seconds?: number; output_format?: string;
};
type RecoveryRecipeEditor = { recipe: RecipeDefinition; fields: RecoveryFieldMapping[]; detailFields: RecoveryFieldMapping[] };
type RecoveryCandidate = { candidate_id: string; selector: string; label?: string; record_count: number; field_match_counts: Record<string, number>; error?: string | null };
type ScrapeRun = { id: string; recipe_id: string; status: string; page_cursor: number; pages_completed: number; record_count: number; metrics?: { pages_visited: number; records_discovered: number; duplicates_removed: number; dom_extractions: number; network_extractions: number; detail_pages_visited: number; detail_extractions: number; selector_recoveries: number; records_written: number; elapsed_ms: number; records_per_second: number }; output_format?: string; stop_reason?: string; error?: string | null; pause_requested?: boolean; stop_requested?: boolean; recovery?: { page_number: number; reason: string; original_selector: string; recipe_updated_at: number; unmatched_fields: string[]; candidates: RecoveryCandidate[] } };
type FailedPageReview = { run_id: string; page_number: number; url: string; clean_markdown: string; truncated: boolean };
type Props = { api: (path: string, init?: RequestInit) => Promise<any>; download?: (path: string, filename: string) => Promise<void>; initialSessionId?: string; initialUrl?: string; initialInstruction?: string; initialRunId?: string; embedded?: boolean };

export function ScrapeStudio({ api, download, initialSessionId = "", initialUrl = "", initialInstruction = "", initialRunId = "", embedded = false }: Props) {
  const {language}=useLanguage();const ja=language==="ja";
  const controlId = useId();
  const [sessions, setSessions] = useState<BrowserSession[]>([]);
  const [profiles, setProfiles] = useState<BrowserProfile[]>([]);
  const [newProfileId, setNewProfileId] = useState("");
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [url, setUrl] = useState(initialUrl);
  const [page, setPage] = useState<PageEvidence | null>(null);
  const [networkEvidence, setNetworkEvidence] = useState<NetworkEvidence[]>([]);
  const [networkJsonEvidence, setNetworkJsonEvidence] = useState<NetworkJsonEvidence | null>(null);
  const [jsonRecordsPointer, setJsonRecordsPointer] = useState("");
  const [jsonFieldMappings, setJsonFieldMappings] = useState<{ name: string; pointer: string }[]>([]);
  const [jsonFieldName, setJsonFieldName] = useState("");
  const [jsonFieldPointer, setJsonFieldPointer] = useState("");
  const [pickerKind, setPickerKind] = useState("row");
  const [candidates, setCandidates] = useState<PickerCandidate[]>([]);
  const [selectedCandidate, setSelectedCandidate] = useState<PickerCandidate | null>(null);
  const [fieldName, setFieldName] = useState("");
  const [fieldMappings, setFieldMappings] = useState<FieldMapping[]>([]);
  const [fieldExtract, setFieldExtract] = useState<"text" | "href">("text");
  const [recipeInstruction, setRecipeInstruction] = useState(initialInstruction);
  const [detailEnabled, setDetailEnabled] = useState(false);
  const [detailLinkField, setDetailLinkField] = useState("");
  const [detailFieldName, setDetailFieldName] = useState("");
  const [detailSelector, setDetailSelector] = useState("");
  const [detailFields, setDetailFields] = useState<{ name: string; selector: string }[]>([]);
  const [recipes, setRecipes] = useState<SavedRecipe[]>([]);
  const [recoveryRecipeId, setRecoveryRecipeId] = useState("");
  const [recoveryCandidates, setRecoveryCandidates] = useState<RecoveryCandidate[]>([]);
  const [recoveryEditor, setRecoveryEditor] = useState<RecoveryRecipeEditor | null>(null);
  const [recoveryFieldName, setRecoveryFieldName] = useState("");
  const [recoveryFieldSelector, setRecoveryFieldSelector] = useState("");
  const [recipePreview, setRecipePreview] = useState<{ name: string; rows: Record<string, unknown>[] } | null>(null);
  const [recipeName, setRecipeName] = useState("");
  const [navigationType, setNavigationType] = useState("none");
  const [pageParameter, setPageParameter] = useState("page");
  const [nextSelector, setNextSelector] = useState("a[rel=next]");
  const [maxPages, setMaxPages] = useState(1);
  const [maxRecords, setMaxRecords] = useState(1000);
  const [outputFormat, setOutputFormat] = useState("csv");
  const [run, setRun] = useState<ScrapeRun | null>(null);
  const [failedPageReview, setFailedPageReview] = useState<FailedPageReview | null>(null);
  const [selectionRects, setSelectionRects] = useState<{x:number;y:number;width:number;height:number}[]>([]);
  const [quickTool, setQuickTool] = useState("list");
  const [quickPaginate, setQuickPaginate] = useState(false);
  const [quickScroll, setQuickScroll] = useState(false);
  const [followLinks, setFollowLinks] = useState(false);
  const [quickSpeed, setQuickSpeed] = useState("normal");
  const [batchUrls, setBatchUrls] = useState("");
  const [quickRecipeId, setQuickRecipeId] = useState("");
  const [selectList, setSelectList] = useState(false);
  const [recipeImport, setRecipeImport] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  useEffect(() => { setQuickRecipeId(""); setSelectionRects([]); }, [sessionId, url, recipeName, navigationType, pageParameter, nextSelector, maxPages, maxRecords, quickPaginate, quickScroll, followLinks]);

  const refresh = async () => {
    const [result, profileResult] = await Promise.allSettled([
      api("/web/browser/sessions"), api("/web/browser/profiles"),
    ]);
    if (result.status === "rejected") throw result.reason;
    const active: BrowserSession[] = result.value.sessions || [];
    setSessions(active);
    if (profileResult.status === "fulfilled") setProfiles(profileResult.value.profiles || []);
    setSessionId(current => active.some(item => item.session_id === current)
      ? current : active[0]?.session_id || "");
  };

  const refreshRecipes = async () => {
    const result = await api("/web/recipes");
    setRecipes(result.recipes || []);
  };

  useEffect(() => {
    void Promise.all([refresh(), refreshRecipes()]).catch(error => setStatus(error.message));
  }, []);

  const open = async (profileId?: string, interactiveLogin = false) => {
    setBusy(true); setStatus("");
    try {
      const result = await api("/web/browser/sessions", {
        method: "POST",
        ...(profileId ? { body: JSON.stringify({ profile_id: profileId, interactive_login: interactiveLogin }) } : {}),
      });
      setSessionId(result.session_id); setPage(null); await refresh();
      setRecipeName("");
      setStatus(interactiveLogin
        ? `Persistent login window opened for “${profileId}”. Sign in directly in that browser; OLCR does not read password fields.`
        : "Managed browser is ready. Navigate to a public HTTP(S) page.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const navigate = async () => {
    if (!sessionId || !url.trim()) return;
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/navigate`, {
        method: "POST", body: JSON.stringify({ url: url.trim() }),
      });
      setPage(result); setNetworkEvidence([]); setNetworkJsonEvidence(null);
      setJsonRecordsPointer(""); setJsonFieldMappings([]); setJsonFieldName(""); setJsonFieldPointer("");
      setUrl(result.url || url); await refresh();
      setCandidates([]); setSelectedCandidate(null); setFieldMappings([]);
      setDetailFields([]); setDetailEnabled(false); setDetailLinkField("");
      setStatus("Page snapshot captured. Page content is untrusted input.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const snapshot = async (forRecovery = false) => {
    if (!sessionId) return;
    setBusy(true); setStatus("");
    try {
      setPage(await api(`/web/browser/sessions/${sessionId}/snapshot`));
      setStatus(forRecovery
        ? "Snapshot refreshed from the selected interactive browser session. It may differ from the failed run page; page content is untrusted input."
        : "Snapshot refreshed.");
    }
    catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (!initialSessionId) return;
    let live = true;
    setSessionId(initialSessionId);
    setUrl(initialUrl);
    setRecipeInstruction(initialInstruction);
    void api(`/web/browser/sessions/${initialSessionId}/snapshot`).then(pageEvidence => {
      if (!live) return;
      setPage(pageEvidence);
      setStatus("Browser session attached to this chat.");
    }).catch(error => {
      if (live) setStatus(`Chat browser session needs refresh: ${error?.message || String(error)}`);
    });
    return () => { live = false; };
  }, [initialSessionId]);

  useEffect(() => {
    if (!initialRunId) return;
    let live = true;
    void Promise.all([refreshRun(initialRunId), refreshRecipes()]).then(() => {
      if (live) setStatus("Scrape run restored from chat. Status and checkpoints refresh automatically.");
    }).catch(error => {
      if (live) setStatus(`Scrape run needs refresh: ${error?.message || String(error)}`);
    });
    return () => { live = false; };
  }, [initialRunId]);

  const reopenRunRecoveryPage = async (runId: string) => {
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/scrape/runs/${runId}/recovery-snapshot`, { method: "POST" });
      setFailedPageReview({ run_id: runId, page_number: result.page_number,
        url: result.url || "", clean_markdown: result.clean_markdown || "",
        truncated: Boolean(result.truncated) });
      setStatus(`Reopened failed Recipe page ${result.page_number} in a temporary managed session. Snapshot is transient and page content is untrusted.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const navigateBack = async () => {
    if (!sessionId) return;
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/back`, { method: "POST" });
      setPage(result); setNetworkEvidence([]); setNetworkJsonEvidence(null);
      setJsonRecordsPointer(""); setJsonFieldMappings([]); setJsonFieldName(""); setJsonFieldPointer("");
      setUrl(result.url || ""); await refresh();
      setCandidates([]); setSelectedCandidate(null); setFieldMappings([]);
      setDetailFields([]); setDetailEnabled(false); setDetailLinkField("");
      setStatus("Returned to the previous page. Page content is untrusted input.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const reloadPage = async () => {
    if (!sessionId) return;
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/reload`, { method: "POST" });
      setPage(result); setNetworkEvidence([]); setNetworkJsonEvidence(null);
      setJsonRecordsPointer(""); setJsonFieldMappings([]); setJsonFieldName(""); setJsonFieldPointer("");
      setUrl(result.url || url); await refresh();
      setCandidates([]); setSelectedCandidate(null); setFieldMappings([]); setDetailFields([]);
      setStatus("Current public URL reloaded. Page content is untrusted input.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const inspectNetwork = async () => {
    if (!sessionId) return;
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/network`);
      setNetworkEvidence(result.requests || []);
      setNetworkJsonEvidence(null);
      setStatus(`${result.requests?.length || 0} fetch/XHR request(s) observed. Request metadata omits query values, headers, and bodies.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const inspectNetworkJson = async (requestIndex: number) => {
    if (!sessionId) return;
    setBusy(true); setStatus(""); setNetworkJsonEvidence(null);
    setJsonRecordsPointer(""); setJsonFieldMappings([]); setJsonFieldName(""); setJsonFieldPointer("");
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/network/${requestIndex}/json`);
      setNetworkJsonEvidence(result);
      setStatus(`Reviewed response ${requestIndex} as transient, redacted JSON evidence. It was not saved; values remain untrusted.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const saveNetworkJsonRecipe = async () => {
    if (!page?.url || !networkJsonEvidence || !jsonFieldMappings.length) return;
    let endpointPath = "";
    let safeStartUrl = "";
    try {
      endpointPath = new URL(networkJsonEvidence.url).pathname;
      const start = new URL(page.url);
      safeStartUrl = `${start.origin}${start.pathname}`;
    }
    catch { setStatus("The reviewed response URL is invalid."); return; }
    setBusy(true); setStatus("");
    try {
      const result = await api("/web/recipes", { method: "POST", body: JSON.stringify({
        name: recipeName.trim() || `${page.title || "Untitled"} JSON`,
        start_url: safeStartUrl, record_selector: "body", fields: {}, source: "network_json",
        network_json: {
          endpoint_path: endpointPath,
          records_pointer: jsonRecordsPointer,
          fields: Object.fromEntries(jsonFieldMappings.map(field => [field.name, { pointer: field.pointer }])),
        },
        navigation: { type: "none" }, wait_strategy: "dom_stable", deduplicate_keys: [],
        max_pages: 1, max_records: Number(maxRecords), max_duration_seconds: 900,
        output_format: outputFormat,
        browser_profile_id: sessions.find(item => item.session_id === sessionId)?.profile_id || null,
      }) });
      await refreshRecipes();
      setStatus(`Draft JSON Recipe saved: ${result.name}. It reads the selected same-origin endpoint at run time and requires Preview & validate.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const inspect = async () => {
    if (!sessionId) return;
    setBusy(true); setStatus(""); setSelectedCandidate(null);
    try {
      const result = await api(`/web/browser/sessions/${sessionId}/picker`, {
        method: "POST", body: JSON.stringify({ kind: pickerKind }),
      });
      setCandidates(result.candidates || []);
      setStatus(`${result.candidates?.length || 0} visible elements found. Review locator evidence before using it.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const saveDraftRecipe = async () => {
    if (!page?.url || selectedCandidate?.kind !== "row") return;
    const selector = selectedCandidate.dom_path.replace(/:nth-of-type\(\d+\)/g, "") || selectedCandidate.selector_candidates[0];
    if (!selector) { setStatus("The selected row has no usable selector evidence."); return; }
    if (!fieldMappings.length) { setStatus("Inspect a table cell and add at least one output field mapping first."); return; }
    if (detailEnabled && (!detailLinkField || !detailFields.length)) { setStatus("Choose an href field and add at least one detail-page field."); return; }
    setBusy(true); setStatus("");
    try {
      const result = await api("/web/recipes", { method: "POST", body: JSON.stringify({
        name: recipeName.trim() || page.title || "Untitled scraper",
        start_url: page.url,
        record_selector: selector,
        fields: Object.fromEntries(fieldMappings.map(field => [field.name, { selector: field.selector, extract: field.extract }])),
        detail: detailEnabled ? { link_field: detailLinkField, record_selector: "body",
          fields: Object.fromEntries(detailFields.map(field => [field.name, { selector: field.selector, extract: "text" }])) } : null,
        navigation: navigationType === "url_parameter" ? { type: navigationType, page_parameter: pageParameter.trim() }
          : navigationType === "next" || navigationType === "load_more" ? { type: navigationType, selector: nextSelector.trim() }
          : { type: navigationType },
        wait_strategy: "dom_stable",
        deduplicate_keys: [],
        max_pages: Number(maxPages),
        max_records: Number(maxRecords),
        max_duration_seconds: 900,
        output_format: outputFormat,
        browser_profile_id: sessions.find(item => item.session_id === sessionId)?.profile_id || null,
      }) });
      await refreshRecipes();
      setStatus(`Draft Recipe saved: ${result.name} with ${fieldMappings.length} field mapping(s). It remains non-executable until validation.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const draftRecipeFromText = async () => {
    if (!sessionId || !recipeInstruction.trim()) return;
    setBusy(true); setStatus("");
    try {
      const recipe = await api("/web/recipes/draft-from-text", {
        method: "POST", body: JSON.stringify({ session_id: sessionId, instruction: recipeInstruction.trim() }),
      });
      await refreshRecipes();
      setRecipeInstruction("");
      setStatus(`Draft Recipe proposed: ${recipe.name}. Review it with Preview & validate before any run.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const previewAndValidateRecipe = async (recipe: SavedRecipe) => {
    if (!sessionId) return;
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/recipes/${recipe.id}/validate`, {
        method: "POST", body: JSON.stringify({ session_id: sessionId, limit: 5 }),
      });
      setRecoveryCandidates([]); setRecoveryRecipeId(""); setRecoveryEditor(null);
      setRecipePreview({ name: recipe.name, rows: result.rows || [] });
      await refreshRecipes();
      setStatus(`Preview matched ${result.record_count} row(s) and validated ${recipe.name}.`);
    } catch (error: any) {
      const message = error?.message || String(error);
      if (message.includes("RECIPE_RECORD_SELECTOR_MATCHED_NO_ROWS") || message.includes("RECIPE_FIELDS_MATCHED_NO_CELLS")) {
        const kind = message.includes("RECIPE_RECORD_SELECTOR_MATCHED_NO_ROWS") ? "row" : "field";
        try {
          const freshPage = await api(`/web/browser/sessions/${sessionId}/snapshot`);
          setPage(freshPage);
          setPickerKind(kind); setSelectedCandidate(null);
          if (kind === "row") {
            const recovery = await api(`/web/recipes/${recipe.id}/recovery-alternatives`, {
              method: "POST", body: JSON.stringify({ session_id: sessionId }),
            });
            setRecoveryRecipeId(recipe.id); setRecoveryCandidates(recovery.candidates || []); setRecoveryEditor(null);
            setCandidates([]);
            setStatus(`Recipe validation failed. Compared ${recovery.candidates?.length || 0} bounded row selector candidate(s); saved Recipe is unchanged.`);
          } else {
            const evidence = await api(`/web/browser/sessions/${sessionId}/picker`, {
              method: "POST", body: JSON.stringify({ kind }),
            });
            setRecoveryCandidates([]); setRecoveryRecipeId(recipe.id); setRecoveryEditor(null); setCandidates(evidence.candidates || []);
            setStatus(`Recipe validation failed. Reacquired ${evidence.candidates?.length || 0} visible field candidate(s) for review; saved selectors were not changed.`);
          }
        } catch (recoveryError: any) { setStatus(`${message} · DOM recovery unavailable: ${recoveryError?.message || String(recoveryError)}`); }
      } else setStatus(message);
    }
    finally { setBusy(false); }
  };

  const openRecoveryEditor = async (targetRecipeId = recoveryRecipeId) => {
    if (!targetRecipeId) return;
    setBusy(true); setStatus("");
    try {
      const recipe: RecipeDefinition = await api(`/web/recipes/${targetRecipeId}`);
      const toMappings = (fields: Record<string, RecipeFieldDefinition> | undefined): RecoveryFieldMapping[] =>
        Object.entries(fields || {}).map(([name, field]) => ({
          name, selector: field.selector, extract: field.extract || "text", attribute: field.attribute ?? null,
        }));
      setRecoveryRecipeId(targetRecipeId);
      setRecoveryEditor({ recipe, fields: toMappings(recipe.fields), detailFields: toMappings(recipe.detail?.fields) });
      setRecoveryFieldName(""); setRecoveryFieldSelector("");
      setStatus(`Loaded ${recipe.name} for manual recovery. Snapshot and page text are untrusted; enter selectors only after reviewing the page evidence.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const saveRecoveryEditor = async () => {
    if (!recoveryRecipeId || !recoveryEditor || recoveryEditor.fields.length === 0) return;
    const fields = Object.fromEntries(recoveryEditor.fields.map(({ name, selector, extract, attribute }) => [name, {
      selector, extract, ...(extract === "attribute" && attribute ? { attribute } : {}),
    }]));
    const detail = recoveryEditor.recipe.detail ? {
      ...recoveryEditor.recipe.detail,
      fields: Object.fromEntries(recoveryEditor.detailFields.map(({ name, selector, extract, attribute }) => [name, {
        selector, extract, ...(extract === "attribute" && attribute ? { attribute } : {}),
      }])),
    } : null;
    setBusy(true); setStatus("");
    try {
      const recipe = recoveryEditor.recipe;
      const result = await api(`/web/recipes/${recoveryRecipeId}`, { method: "PUT", body: JSON.stringify({
        name: recipe.name,
        start_url: recipe.start?.url,
        record_selector: recipe.record_selector,
        fields,
        navigation: recipe.navigation || { type: "none" },
        detail,
        wait_strategy: recipe.wait_strategy || "dom_stable",
        deduplicate_keys: recipe.deduplicate_keys || [],
        max_pages: recipe.max_pages || 1,
        max_records: recipe.max_records || 10000,
        max_duration_seconds: recipe.max_duration_seconds || 900,
        output_format: recipe.output_format || "csv",
        browser_profile_id: recipe.browser_profile_id || null,
      }) });
      setRecoveryEditor(null); setRecoveryCandidates([]);
      await refreshRecipes();
      setStatus(`Saved ${result.name} as DRAFT. Review the updated selectors with Preview & validate before running or resuming.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const applyRecoverySelector = async (candidate: RecoveryCandidate, targetRecipeId = recoveryRecipeId) => {
    if (!targetRecipeId) return;
    setBusy(true);
    try {
      const result = await api(`/web/recipes/${targetRecipeId}/record-selector`, {
        method: "PATCH", body: JSON.stringify({ record_selector: candidate.selector }),
      });
      await refreshRecipes();
      setRecoveryCandidates([]); setRecoveryRecipeId("");
      setRecoveryEditor(null);
      setStatus(`Updated ${result.name} row selector from visible DOM evidence. Recipe is DRAFT; preview and validate it again before running.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const refreshRun = async (runId: string) => {
    const result = await api(`/web/scrape/runs/${runId}`);
    setRun(result);
  };

  const startRun = async (recipe: SavedRecipe) => {
    setBusy(true); setStatus("");
    try {
      const result = await api(`/web/recipes/${recipe.id}/runs`, { method: "POST" });
      setRun(result);
      setStatus(`Started deterministic run for ${recipe.name}.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const controlRun = async (action: "pause" | "resume" | "stop") => {
    if (!run) return;
    setBusy(true);
    try {
      const result = await api(`/web/scrape/runs/${run.id}/${action}`, { method: "POST" });
      setRun(result);
      setStatus(action === "pause" ? "Pause requested; the current page will checkpoint first." : action === "stop" ? "Stop requested; the current page will checkpoint first." : "Run resumed.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const exportRun = async () => {
    if (!run || !download) return;
    setBusy(true);
    try { await download(`/web/scrape/runs/${run.id}/export?format=${outputFormat}`, `olcr-${run.id}.${outputFormat}`); setStatus(`Saved ${outputFormat.toUpperCase()} file to this computer.`); }
    catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (!run || !["QUEUED", "RUNNING"].includes(run.status)) return;
    const timer = window.setInterval(() => { void refreshRun(run.id).catch(error => setStatus(error.message)); }, 1000);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.status]);

  const close = async () => {
    if (!sessionId) return;
    setBusy(true);
    try {
      await api(`/web/browser/sessions/${sessionId}`, { method: "DELETE" });
      setPage(null); setCandidates([]); setSelectedCandidate(null); setFieldMappings([]); setDetailFields([]); setDetailEnabled(false); setDetailLinkField(""); setSessionId(""); await refresh(); setStatus("Browser session closed.");
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const forgetProfile = async (profileId: string) => {
    if (!window.confirm(`Forget local login data for “${profileId}”?`)) return;
    setBusy(true);
    try {
      await api(`/web/browser/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
      await refresh();
      setStatus(`Forgot login data for “${profileId}”.`);
    } catch (error: any) { setStatus(error?.message || String(error)); }
    finally { setBusy(false); }
  };

  const quickPreview = async (point?: { x: number; y: number }) => {
    if (!sessionId) return;
    setBusy(true); setStatus(""); setQuickRecipeId("");
    try {
      const result = await api("/web/recipes/auto", { method: "POST", body: JSON.stringify({
        session_id: sessionId, tool: point ? "list" : quickTool, speed: quickSpeed, follow_internal_links: followLinks, auto_paginate: quickPaginate,
        name: recipeName, urls: batchUrls.split(/\s+/).filter(Boolean), max_pages: maxPages, max_records: maxRecords,
        navigation: quickScroll ? { type: "infinite_scroll" } : navigationType === "none" ? {} : { type: navigationType, ...(navigationType === "url_parameter" ? { page_parameter: pageParameter } : ["next", "load_more"].includes(navigationType) ? { selector: nextSelector } : {}) },
        ...(point || {}),
      }) });
      setQuickRecipeId(result.recipe.id); setRecipePreview({ name: result.recipe.name, rows: result.rows });
      setSelectList(false); setSelectionRects(result.record_rects || []); await refreshRecipes();
      setStatus(`Found ${result.record_count} records. Review the sample, then start collection.`);
    } catch (reason: any) { setStatus(reason.message || String(reason)); }
    finally { setBusy(false); }
  };
  const quickStart = async () => {
    setBusy(true);
    try {
      await api(`/web/recipes/${quickRecipeId}/validate`, { method: "POST", body: JSON.stringify({ session_id: sessionId, limit: 5 }) });
      setRun(await api(`/web/recipes/${quickRecipeId}/runs`, { method: "POST" }));
      await refreshRecipes(); setStatus("Collection started. Stop keeps collected rows.");
    } catch (reason: any) { setStatus(reason.message || String(reason)); }
    finally { setBusy(false); }
  };

  const recoveryRecipe = run?.recovery ? recipes.find(item => item.id === run.recipe_id) : undefined;
  const recoveryResumeBlocked = Boolean(run?.recovery && (
    recoveryRecipe?.status !== "VALIDATED" || !recoveryRecipe.updated_at ||
    recoveryRecipe.updated_at <= run.recovery.recipe_updated_at
  ));

  return <section className={embedded ? `scrape-studio chat-scrape-studio${advancedOpen ? " scrape-advanced-open" : ""}` : "scrape-studio"} aria-labelledby={embedded ? undefined : "scrape-title"}>
    <div className="scrape-heading">
      <div><p className="eyebrow">{ja?"ウェブ閲覧 · 制御されたブラウザー":"WEB READ · CONTROLLED BROWSER"}</p><h2 id={embedded ? undefined : "scrape-title"}>{ja?(embedded?"ウェブスクレイピング":"スクレイピングスタジオ"):(embedded ? "Web scraping" : "Scrape Studio")}</h2>
        <p>{ja?"一時ブラウザーまたはローカルのログインプロファイルを使います。固定された送信プロキシを通じて公開HTTP(S)宛てのみ接続します。":"Use a temporary browser or a local persistent login profile. Only public HTTP(S) destinations pass the pinned egress proxy."}</p></div>
      <div className="scrape-browser-open-controls">
        {embedded && <button type="button" aria-expanded={advancedOpen} onClick={() => setAdvancedOpen(value => !value)}>{ja?(advancedOpen?"操作を隠す":"操作"):(advancedOpen ? "Hide controls" : "Controls")}</button>}
        {!sessionId && <button className="primary" disabled={busy || sessions.length >= 2} onClick={() => void open()}>{ja?(busy?"起動中…":"ブラウザーを開く"):(busy ? "Starting…" : "Open browser")}</button>}
        {sessionId && <button disabled={busy} onClick={() => void close()}>{ja?"セッションを閉じる":"Close session"}</button>}
        <label>{ja?"ログインプロファイルID":"Login profile ID"}<input aria-label={ja?"ログインプロファイルID":"Login profile ID"} value={newProfileId} onChange={event => setNewProfileId(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 64))} placeholder={ja?"例：個人用アカウント":"example-account"} maxLength={64} /></label>
        <button disabled={busy || sessions.length >= 2 || !/^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/.test(newProfileId)} onClick={() => void open(newProfileId, true)}>{ja?"画面表示付きログインブラウザーを開く":"Open headed login browser"}</button>
      </div>
    </div>
    {sessionId && <>
      <section className="scrape-quick-tools" aria-label={ja?"クイック抽出":"Quick extraction"}>
        <h3>{ja?"ページデータを抽出":"Extract page data"}</h3>
        <label>{ja?"抽出ツール":"Extraction tool"}<select value={quickTool} onChange={e => { setQuickTool(e.target.value); setQuickRecipeId(""); }}>
          {[["list", "Repeating list"], ["page", "Page metadata"], ["text", "Page text"], ["emails", "Email addresses"], ["social", "Social links"], ["images", "Image URLs"], ["sitemap", "Sitemap URLs"], ["shopify", "Shopify products / variants"]].map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <label>{ja?"速度":"Speed"}<select value={quickSpeed} onChange={e => { setQuickSpeed(e.target.value); setQuickRecipeId(""); }}><option value="fast">{ja?"高速":"Fast"}</option><option value="normal">{ja?"通常 · 1秒":"Normal · 1 second"}</option><option value="slow">{ja?"低速 · 3秒":"Slow · 3 seconds"}</option></select></label>
        <label>{ja?"レシピ名":"Recipe name"}<input value={recipeName} onChange={e => setRecipeName(e.target.value)} placeholder={ja?"保存するスクレイパー名":"Saved scraper name"} maxLength={120} /></label>
        <label>{ja?"収集ページ数の上限":"Collection page limit"}<input type="number" min={1} max={500} value={maxPages} onChange={e => setMaxPages(Number(e.target.value))} /></label>
        <label>{ja?"収集行数の上限":"Collection row limit"}<input type="number" min={1} max={100000} value={maxRecords} onChange={e => setMaxRecords(Number(e.target.value))} /></label>
        <label><input type="checkbox" checked={quickPaginate} onChange={e => { setQuickPaginate(e.target.checked); setQuickScroll(false); setFollowLinks(false); if(e.target.checked) setMaxPages(Math.max(10,maxPages)); }} />{ja?"次へ／さらに読み込むを自動検出":"Detect Next / Load More automatically"}</label>
        <label><input type="checkbox" checked={quickScroll} onChange={e => { setQuickScroll(e.target.checked); setQuickPaginate(false); setFollowLinks(false); if(e.target.checked) setMaxPages(Math.max(10,maxPages)); }} />{ja?"無限スクロールで収集":"Collect with infinite scroll"}</label>
        <label><input type="checkbox" checked={followLinks} onChange={e => { setFollowLinks(e.target.checked); setQuickPaginate(false); setQuickScroll(false); setQuickRecipeId(""); if(e.target.checked) setMaxPages(Math.max(10,maxPages)); }} />{ja?"内部リンクも巡回 · 1階層目、ページ上限を適用":"Also visit internal links · first level, page limit applies"}</label>
        <label>{ja?"複数ページURL（任意）":"Multiple page URLs (optional)"}<textarea value={batchUrls} onChange={e => { setBatchUrls(e.target.value); setQuickRecipeId(""); }} placeholder={ja?"1行に1つのURL":"One URL per line"} /></label>
        <button disabled={busy} onClick={() => void quickPreview()}>{ja?"自動抽出をプレビュー":"Preview automatic extraction"}</button>
        <button disabled={busy || quickTool !== "list"} onClick={() => setSelectList(v => !v)}>{ja?(selectList?"リスト選択をキャンセル":"ページ内のリストを選択"):(selectList ? "Cancel list selection" : "Select a list in the page")}</button>
        <button className="primary" disabled={busy || !quickRecipeId} onClick={() => void quickStart()}>{ja?"プレビューした収集を開始":"Start previewed collection"}</button>
        {selectList && <p>{ja?"ブラウザープレビューでカードまたは行をクリックしてください。同じ形式の要素が3つ以上必要です。":"Click one card or row in the browser preview. At least three similar siblings are required."}</p>}
      </section>
      <BrowserViewport api={api} sessionId={sessionId} active selectionRects={selectionRects} onSelect={selectList && !busy ? point => void quickPreview(point) : undefined} />
    </>}
    {sessions.length > 0 && <label className="model">{ja?"アクティブなブラウザーセッション":"Active browser session"}
      <select value={sessionId} onChange={event => { setSessionId(event.target.value); setPage(null); setCandidates([]); setSelectedCandidate(null); setFieldMappings([]); setDetailFields([]); setDetailEnabled(false); setDetailLinkField(""); }}>
        {sessions.map(item => <option key={item.session_id} value={item.session_id}>{item.url || "New session"} · {item.profile_id ? `profile ${item.profile_id}` : "temporary"} · {item.session_id.slice(0, 8)}</option>)}
      </select>
    </label>}
    {profiles.length > 0 && <section className="scrape-browser-profiles" aria-label={ja?"保存済みログインプロファイル":"Saved login profiles"}><h3>{ja?"ローカルログインプロファイル":"Local login profiles"}</h3><ul>{profiles.map(profile => <li key={profile.profile_id}><code>{profile.profile_id}</code>{profile.in_use ? " · in use" : ""}<button disabled={busy || profile.in_use} onClick={() => void forgetProfile(profile.profile_id)}>{ja?"ローカルデータを削除":"Forget local data"}</button></li>)}</ul><small>{ja?"ログインCookieとブラウザーデータはアプリのサポートフォルダー内に保存されます。レシピにはプロファイルIDのみ保存されます。":"Login cookies and browser storage stay in the app support folder. Recipe data stores only the profile ID."}</small></section>}
    <form className="scrape-navigation" onSubmit={event => { event.preventDefault(); void navigate(); }}>
      <label htmlFor={`${controlId}-scrape-url`}>{ja?"公開ページURL":"Public page URL"}</label>
      <div><input id={`${controlId}-scrape-url`} type="url" required value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com" />
        <button disabled={!sessionId || busy || !url.trim()}>{ja?(busy?"処理中…":"移動"):(busy ? "Working…" : "Navigate")}</button></div>
    </form>
    {sessionId && <div className="scrape-actions"><button disabled={busy || !page} onClick={() => void navigateBack()}>{ja?"戻る":"Go back"}</button>
      <button disabled={busy || !page} onClick={() => void reloadPage()}>{ja?"ページを再読み込み":"Reload page"}</button>
      <button disabled={busy || !page} onClick={() => void inspectNetwork()}>{ja?"Fetch/XHRメタデータを確認":"Inspect fetch/XHR metadata"}</button>
      <button disabled={busy || !page} onClick={() => void snapshot()}>{ja?"スナップショットを更新":"Refresh snapshot"}</button>
      <span>{sessions.find(item => item.session_id === sessionId)?.profile_id ? "This session uses its local persistent profile." : "Temporary session data is in memory and expires after 15 minutes idle."}</span></div>}
    {sessionId && page && <section className="scrape-picker" aria-label={ja?"ページ要素の選択":"Page element picker"}>
      <h3>{ja?"ページ要素を選択":"Pick page elements"}</h3>
      <p>{ja?"OLCRは固定された読み取り専用スクリプトで画面上のDOM情報を取得します。選択した要素をクリックすることはありません。":"OLCR reads visible DOM evidence with a fixed, read-only script. It does not click the selected page element."}</p>
      <div className="scrape-recipe-draft" aria-label={ja?"自然文からレシピ案を作成":"Natural language Recipe draft"}>
        <label htmlFor={`${controlId}-recipe-instruction`}>{ja?"収集する内容を説明":"Describe what to collect"}</label>
        <textarea id={`${controlId}-recipe-instruction`} value={recipeInstruction} onChange={event => setRecipeInstruction(event.target.value)} maxLength={2000} placeholder={ja?"組織名とプロフィールへのリンクを収集":"Collect each organization name and its profile link"} />
        <button disabled={busy || !recipeInstruction.trim()} onClick={() => void draftRecipeFromText()}>{ja?"レシピ案を作成":"Propose Recipe draft"}</button>
        <small>{ja?"モデルが利用できるのは画面上で確認できるDOM候補のみです。プレビューと検証が終わるまで案は下書きのままです。":"The model can use only visible DOM candidates. The proposal stays a draft until you preview and validate it."}</small>
      </div>
      <div className="scrape-picker-controls"><label>{ja?"要素の種類":"Element type"}<select value={pickerKind} onChange={event => setPickerKind(event.target.value)}>
        <option value="row">{ja?"行":"Rows"}</option><option value="field">{ja?"フィールド":"Fields"}</option><option value="navigation">{ja?"ページ移動リンク":"Navigation links"}</option><option value="all">{ja?"すべての表示候補":"All visible candidates"}</option>
      </select></label><button disabled={busy} onClick={() => void inspect()}>{ja?"ページを調査":"Inspect page"}</button></div>
      {candidates.length > 0 && <div className="scrape-candidates" role="list" aria-label={ja?"ページ上の表示要素":"Visible page elements"}>{candidates.map((candidate,index)=><button aria-pressed={selectedCandidate===candidate} key={`${candidate.dom_path}:${index}`} onClick={()=>setSelectedCandidate(candidate)}>
        <strong>{candidate.role || candidate.kind} · {candidate.text || candidate.tag}</strong><small>{candidate.dom_path || candidate.tag} · {candidate.bbox.width}×{candidate.bbox.height}</small>
      </button>)}</div>}
      {selectedCandidate && <div className="scrape-selection" aria-label={ja?"選択要素の情報":"Selected element evidence"}><strong>{ja?"選択中":"Selected"} {selectedCandidate.kind}: {selectedCandidate.text || selectedCandidate.tag}</strong>
        <p>Role: {selectedCandidate.role || "unknown"} · Tag: {selectedCandidate.tag} · Bounds: {selectedCandidate.bbox.x}, {selectedCandidate.bbox.y}, {selectedCandidate.bbox.width}×{selectedCandidate.bbox.height}</p>
        <p>{ja?"DOMパス":"DOM path"}: <code>{selectedCandidate.dom_path}</code></p><p>{ja?"セレクター候補":"Selector candidates"}:</p>
        <ul>{selectedCandidate.selector_candidates.map(value=><li key={value}><code>{value}</code></li>)}</ul>
        {selectedCandidate.kind === "field" && <><p>Row-relative selector: <code>{selectedCandidate.relative_selector || "行の外"}</code></p>
          {selectedCandidate.relative_selector && <div className="scrape-field-mapping"><label htmlFor={`${controlId}-field-name`}>{ja?"出力フィールド名":"Output field name"}</label><input id={`${controlId}-field-name`} value={fieldName} onChange={event => setFieldName(event.target.value)} placeholder={selectedCandidate.text || "field_name"} maxLength={120} />
            <label htmlFor={`${controlId}-field-extract`}>{ja?"抽出内容":"Extract"}<select id={`${controlId}-field-extract`} value={fieldExtract} onChange={event => setFieldExtract(event.target.value as "text" | "href")}><option value="text">{ja?"テキスト":"Text"}</option><option value="href">{ja?"リンクURL":"Link URL"}</option></select></label>
            <button disabled={busy || !fieldName.trim() || fieldMappings.some(field => field.name === fieldName.trim())} onClick={() => { setFieldMappings(current => [...current, { name: fieldName.trim(), selector: selectedCandidate.relative_selector!, extract: fieldExtract }]); setFieldName(""); }}>{ja?"フィールド対応を追加":"Add field mapping"}</button>
          </div>}</>}
        <p className="muted">{ja?"要素位置の情報はプレビューです。検証が完了するまでレシピは実行できません。":"Locator evidence is a preview. Recipe execution remains disabled until validation."}</p>
      </div>}
      {fieldMappings.length > 0 && <div className="scrape-field-mappings" aria-label={ja?"レシピ出力フィールドの対応":"Recipe output field mappings"}><strong>{ja?"出力フィールド":"Output fields"}</strong><ul>{fieldMappings.map(field => <li key={field.name}><code>{field.name}</code> ← <code>{field.selector}</code><button aria-label={ja?`${field.name}を削除`:`Remove ${field.name}`} onClick={() => setFieldMappings(current => current.filter(item => item.name !== field.name))}>Remove</button></li>)}</ul></div>}
      {selectedCandidate?.kind === "row" && <div className="scrape-recipe-draft">
        <label htmlFor={`${controlId}-recipe-name`}>{ja?"レシピ案の名前":"Draft Recipe name"}</label>
        <input id={`${controlId}-recipe-name`} value={recipeName} onChange={event => setRecipeName(event.target.value)} placeholder={page.title || "Untitled scraper"} maxLength={120} />
        <label htmlFor={`${controlId}-recipe-navigation`}>{ja?"ページ移動方法":"Navigation"}</label><select id={`${controlId}-recipe-navigation`} value={navigationType} onChange={event => { setNavigationType(event.target.value); setMaxPages(event.target.value === "none" ? 1 : Math.max(2, maxPages)); }}>
          <option value="none">{ja?"単一ページ":"Single page"}</option><option value="url_parameter">{ja?"URLのページ番号":"URL page parameter"}</option><option value="next">{ja?"同一サイトの次ページリンク":"Same-site next link"}</option>
          <option value="load_more">{ja?"さらに読み込むボタン":"Load more button"}</option><option value="infinite_scroll">{ja?"無限スクロール":"Infinite scroll"}</option></select>
        {navigationType === "url_parameter" && <><label htmlFor={`${controlId}-recipe-page-param`}>{ja?"ページ番号パラメーター":"Page parameter"}</label><input id={`${controlId}-recipe-page-param`} value={pageParameter} onChange={event => setPageParameter(event.target.value)} maxLength={128} /></>}
        {navigationType === "next" && <><label htmlFor={`${controlId}-recipe-next-selector`}>{ja?"次ページリンクのCSSセレクター":"Next link CSS selector"}</label><input id={`${controlId}-recipe-next-selector`} value={nextSelector} onChange={event => setNextSelector(event.target.value)} maxLength={512} /></>}
        {navigationType === "load_more" && <><label htmlFor={`${controlId}-recipe-next-selector`}>{ja?"「さらに読み込む」ボタンのCSSセレクター":"Load more button CSS selector"}</label><input id={`${controlId}-recipe-next-selector`} value={nextSelector} onChange={event => setNextSelector(event.target.value)} maxLength={512} /></>}
        <label><input type="checkbox" checked={detailEnabled} onChange={event => setDetailEnabled(event.target.checked)} />{ja?"各行の詳細ページも表示":" Visit each row's detail page"}</label>
        {detailEnabled && <><label htmlFor={`${controlId}-detail-link-field`}>{ja?"詳細ページリンクの項目":"Mapped detail link field"}</label><select id={`${controlId}-detail-link-field`} value={detailLinkField} onChange={event => setDetailLinkField(event.target.value)}>
          <option value="">{ja?"hrefフィールドを選択":"Choose an href field"}</option>{fieldMappings.filter(field => field.extract === "href").map(field => <option key={field.name} value={field.name}>{field.name}</option>)}
        </select><label htmlFor={`${controlId}-detail-field-name`}>{ja?"詳細ページの出力項目名":"Detail output field name"}</label><input id={`${controlId}-detail-field-name`} value={detailFieldName} onChange={event => setDetailFieldName(event.target.value)} maxLength={120} />
          <label htmlFor={`${controlId}-detail-field-selector`}>{ja?"詳細項目のCSSセレクター":"Detail field CSS selector"}</label><input id={`${controlId}-detail-field-selector`} value={detailSelector} onChange={event => setDetailSelector(event.target.value)} maxLength={512} />
          <button disabled={busy || !detailFieldName.trim() || !detailSelector.trim() || fieldMappings.some(field => field.name === detailFieldName.trim()) || detailFields.some(field => field.name === detailFieldName.trim())}
            onClick={() => { setDetailFields(current => [...current, { name: detailFieldName.trim(), selector: detailSelector.trim() }]); setDetailFieldName(""); setDetailSelector(""); }}>Add detail field</button>
          {detailFields.length > 0 && <ul aria-label={ja?"詳細ページの出力項目":"Detail-page output fields"}>{detailFields.map(field => <li key={field.name}><code>{field.name}</code> ← <code>{field.selector}</code></li>)}</ul>}
          <small>{ja?"詳細リンクは同一オリジン内に限られます。抽出したhref値からクエリー文字列は除外されます。":"Detail links must stay on the same origin. Link query strings are omitted from extracted href values."}</small>
        </>}
        <label htmlFor={`${controlId}-recipe-max-pages`}>{ja?"最大ページ数":"Maximum pages"}</label><input id={`${controlId}-recipe-max-pages`} type="number" min={1} max={500} disabled={navigationType === "none"} value={maxPages} onChange={event => setMaxPages(Number(event.target.value))} />
        <label htmlFor={`${controlId}-recipe-max-records`}>{ja?"最大レコード数":"Maximum records"}</label><input id={`${controlId}-recipe-max-records`} type="number" min={1} max={100000} value={maxRecords} onChange={event => setMaxRecords(Number(event.target.value))} />
        <label htmlFor={`${controlId}-recipe-output`}>{ja?"出力形式":"Output format"}</label><select id={`${controlId}-recipe-output`} value={outputFormat} onChange={event => setOutputFormat(event.target.value)}><option value="csv">CSV</option><option value="json">JSON</option><option value="jsonl">JSONL</option></select>
        <button disabled={busy || !page.url} onClick={() => void saveDraftRecipe()}>{ja?"レシピ案を保存":"Save draft Recipe"}</button>
        <small>{ja?"行セレクター、開始URL、行に紐づくフィールド、ページ移動方法と上限を保存します。検証が完了するまで下書きは実行できません。":"The row selector, start URL, row-relative fields, navigation and stop limits are saved. The draft cannot run until validation."}</small>
      </div>}
    </section>}
    {recipes.length > 0 && <section className="scrape-saved-recipes" aria-label={ja?"保存済みスクレイピングレシピ":"Saved scraper Recipes"}>
      <h3>{ja?"保存済みレシピ":"Saved Recipes"}</h3><ul>{recipes.map(recipe => <li key={recipe.id}><div><strong>{recipe.name}</strong> · {recipe.status} · {recipe.source === "network_json" ? "same-origin JSON" : <code>{recipe.record_selector}</code>}{recipe.browser_profile_id ? ` · profile ${recipe.browser_profile_id}` : " · temporary session"}</div>
        {sessionId && recipe.status === "DRAFT" && <button disabled={busy} onClick={() => void previewAndValidateRecipe(recipe)}>{ja?"プレビューして検証":"Preview & validate"}</button>}
        {recipe.status === "VALIDATED" && <button disabled={busy || Boolean(recipe.browser_profile_id && profiles.find(profile => profile.profile_id === recipe.browser_profile_id)?.in_use)} onClick={() => void startRun(recipe)}>{ja?"レシピを実行":"Run Recipe"}</button>}
        {recipe.status === "VALIDATED" && recipe.browser_profile_id && profiles.find(profile => profile.profile_id === recipe.browser_profile_id)?.in_use && <small>{ja?"ヘッドレス実行を開始する前に、このプロファイルの画面表示ブラウザーを閉じてください。":"Close the headed browser session for this profile before starting its headless run."}</small>}
      </li>)}</ul>
    </section>}
    {recoveryRecipeId && <section className="scrape-recipe-recovery" aria-label={ja?"レシピセレクターの修復候補":"Recipe selector recovery candidates"}>
      <h3>{ja?"セレクター修復の根拠":"Selector recovery evidence"}</h3>
      <p>{ja?"現在の管理ページから読み取り専用で情報を取得します。セレクターの適用やレシピの編集後は下書きとして保存され、再度プレビューと検証が必要です。":"Read-only evidence from the current managed page. Applying a selector or editing the Recipe saves it as DRAFT and requires a new Preview & validate."}</p>
      {recoveryCandidates.length > 0 && <ul>{recoveryCandidates.map(candidate => <li key={candidate.candidate_id}>
        <code>{candidate.selector}</code> · {candidate.record_count} row(s)
        {Object.entries(candidate.field_match_counts).map(([field, count]) => <span key={field}> · {field}: {count}</span>)}
        {candidate.error && <span> · {candidate.error}</span>}
        <button disabled={busy || candidate.record_count < 1} onClick={() => void applyRecoverySelector(candidate)}>{ja?"このセレクターを使用":"Use this selector"}</button>
      </li>)}</ul>}
      {recoveryCandidates.length === 0 || recoveryCandidates.every(candidate => candidate.record_count < 1)
        ? <button disabled={busy || !sessionId} onClick={() => void snapshot(true)}>{ja?"ブラウザーのアクセシビリティ情報を確認":"Review browser accessibility snapshot"}</button>
        : null}
      <button disabled={busy} onClick={() => void openRecoveryEditor()}>{ja?"レシピ項目を編集":"Edit Recipe fields"}</button>
    </section>}
    {run && <ScrapeDataTable api={api} runId={run.id} status={run.status} recordCount={run.record_count} download={download} onUseUrls={urls => { setBatchUrls(urls.join("\n")); setQuickTool("page"); setQuickPaginate(false); setQuickScroll(false); setFollowLinks(false); setNavigationType("none"); setMaxPages(Math.max(1,urls.length)); setQuickRecipeId(""); setStatus("URLs transferred to page extraction. Preview the new collection above."); }} />}
    {run && <section className="scrape-run" aria-label={ja?"スクレイピング実行状況":"Scrape run status"}><h3>Recipe run · {run.status}</h3>
      <p>{run.pages_completed} page(s) checkpointed · {run.record_count} unique record(s){run.stop_reason ? ` · ${run.stop_reason}` : ""}{run.error ? ` · ${run.error}` : ""}</p>
      {run.metrics && <small aria-label={ja?"スクレイピング実行の統計":"Scrape run metrics"}>{run.metrics.pages_visited} page(s) visited · {run.metrics.records_discovered} discovered · {run.metrics.records_written} written · {run.metrics.duplicates_removed} duplicates removed · DOM {run.metrics.dom_extractions} / JSON {run.metrics.network_extractions} extractions · detail {run.metrics.detail_pages_visited} page(s) / {run.metrics.detail_extractions} extraction(s) · {run.metrics.selector_recoveries} selector recovery · {(run.metrics.elapsed_ms / 1000).toFixed(1)}s · {run.metrics.records_per_second.toFixed(2)} records/s</small>}
      {run.recovery && <div className="scrape-recipe-recovery" role="group" aria-label={ja?"実行復旧の確認":"Run recovery review"}>
        <h4>Selector recovery · page {run.recovery.page_number}</h4>
        <p>{ja?"失敗したページの状態は保存されていません。設定済みのブラウザープロファイルを使って一時セッションでページを開き直し、アクセシビリティ情報を確認してください。ページ内容は信頼せず、レシピを更新してから再度プレビューと検証を行い、再開してください。":"The failed page was not checkpointed. Reopen it in a temporary managed session using the Recipe’s saved browser profile when configured, then review the transient accessibility snapshot. Treat page content as untrusted; update the Recipe, then Preview & validate before resuming."}</p>
        {(run.recovery.unmatched_fields?.length ?? 0) > 0 && <p>Fields without matches: {run.recovery.unmatched_fields.join(", ")}</p>}
        {(run.recovery.candidates?.length ?? 0) > 0
          ? <ul>{run.recovery.candidates.map(candidate => <li key={candidate.candidate_id}>
            <code>{candidate.selector}</code> · {candidate.record_count} row(s)
            {Object.entries(candidate.field_match_counts).map(([field, count]) => <span key={field}> · {field}: {count}</span>)}
            {candidate.error && <span> · {candidate.error}</span>}
            <button disabled={busy || candidate.record_count < 1} onClick={() => void applyRecoverySelector(candidate, run.recipe_id)}>{ja?"このセレクターを使用":"Use this selector"}</button>
          </li>)}</ul>
          : <p>{ja?"使用できる別の行候補がありません。現在のページを確認し、レシピのセレクターを編集して再検証してください。":"No usable row alternative was found. Review the current page, edit the Recipe selectors, then validate it again."}</p>}
        {(!(run.recovery.candidates?.length) || run.recovery.candidates.every(candidate => candidate.record_count < 1)) && <button disabled={busy} onClick={() => void reopenRunRecoveryPage(run.id)}>{ja?"失敗したページを再度開いて確認":"Reopen failed page for review"}</button>}
        {failedPageReview?.run_id === run.id && <div aria-label={ja?"失敗したレシピページのスナップショット":"Failed Recipe page snapshot"}>
          <p>{failedPageReview.url} · transient accessibility snapshot{failedPageReview.truncated ? " (truncated)" : ""} · untrusted page content</p>
          <pre>{failedPageReview.clean_markdown}</pre>
        </div>}
        <button disabled={busy} onClick={() => void openRecoveryEditor(run.recipe_id)}>{ja?"レシピ項目を編集":"Edit Recipe fields"}</button>
      </div>}
      <div className="scrape-run-controls"><button disabled={busy || !["QUEUED", "RUNNING"].includes(run.status)} onClick={() => void controlRun("pause")}>{ja?"一時停止":"Pause"}</button>
        <button disabled={busy || !["PAUSED", "INTERRUPTED", "FAILED"].includes(run.status) || recoveryResumeBlocked} onClick={() => void controlRun("resume")}>{ja?"再開":"Resume"}</button>
        <button disabled={busy || !["QUEUED", "RUNNING"].includes(run.status)} onClick={() => void controlRun("stop")}>{ja?"停止":"Stop"}</button>
        <button disabled={busy} onClick={() => void refreshRun(run.id)}>{ja?"更新":"Refresh"}</button>
        <button disabled={busy || !download || run.record_count < 1} onClick={() => void exportRun()}>{ja?"エクスポート":"Export"}</button>
      </div>
    </section>}
    {recoveryEditor && <section className="scrape-recipe-recovery-editor" aria-label={ja?"レシピ項目の編集":"Edit Recipe fields"}>
      <h3>Edit Recipe fields · {recoveryEditor.recipe.name}</h3>
      <p>{ja?"確認済みページのスナップショットを根拠として使います。ページ内容は信頼せず、OLCRが自動でセレクターに変換することはありません。保存するとレシピは下書きに戻ります。":"Use the reviewed page snapshot as evidence. Page content is untrusted; OLCR will not convert it into selectors automatically. Saving returns the Recipe to DRAFT."}</p>
      <label>{ja?"レコードセレクター":"Record selector"}<input aria-label={ja?"復旧用レコードセレクター":"Recovery record selector"} value={recoveryEditor.recipe.record_selector}
        onChange={event => setRecoveryEditor(current => current ? { ...current, recipe: { ...current.recipe, record_selector: event.target.value } } : current)} maxLength={512} /></label>
      <strong>{ja?"出力フィールド":"Output fields"}</strong>
      <ul>{recoveryEditor.fields.map((field, index) => <li key={`${field.name}-${index}`}>
        <label>{ja?"項目名":"Field name"}<input aria-label={`Recovery field name ${index + 1}`} value={field.name} maxLength={120}
          onChange={event => setRecoveryEditor(current => current ? { ...current, fields: current.fields.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) } : current)} /></label>
        <label>{ja?"セレクター":"Selector"}<input aria-label={`Recovery field selector ${field.name}`} value={field.selector} maxLength={512}
          onChange={event => setRecoveryEditor(current => current ? { ...current, fields: current.fields.map((item, itemIndex) => itemIndex === index ? { ...item, selector: event.target.value } : item) } : current)} /></label>
        <label>{ja?"抽出内容":"Extract"}<select aria-label={`Recovery field extract ${field.name}`} value={field.extract}
          onChange={event => setRecoveryEditor(current => current ? { ...current, fields: current.fields.map((item, itemIndex) => itemIndex === index ? { ...item, extract: event.target.value } : item) } : current)}>
          {(["text", "inner_html", "href", "src", "attribute", "number", "date", "boolean", "list"] as const).map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        {field.extract === "attribute" && <label>{ja?"属性":"Attribute"}<input aria-label={`Recovery field attribute ${field.name}`} value={field.attribute || ""} maxLength={128}
          onChange={event => setRecoveryEditor(current => current ? { ...current, fields: current.fields.map((item, itemIndex) => itemIndex === index ? { ...item, attribute: event.target.value } : item) } : current)} /></label>}
        <button disabled={busy} onClick={() => setRecoveryEditor(current => current ? { ...current, fields: current.fields.filter((_, itemIndex) => itemIndex !== index) } : current)}>{ja?"項目を削除":"Remove field"}</button>
      </li>)}</ul>
      {recoveryEditor.recipe.detail && <>
        <strong>{ja?"詳細ページの項目":"Detail-page fields"}</strong>
        <ul>{recoveryEditor.detailFields.map((field, index) => <li key={`${field.name}-${index}`}>
          <label>{ja?"項目名":"Field name"}<input aria-label={`Recovery detail field name ${index + 1}`} value={field.name} maxLength={120}
            onChange={event => setRecoveryEditor(current => current ? { ...current, detailFields: current.detailFields.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) } : current)} /></label>
          <label>{ja?"セレクター":"Selector"}<input aria-label={`Recovery detail field selector ${field.name}`} value={field.selector} maxLength={512}
            onChange={event => setRecoveryEditor(current => current ? { ...current, detailFields: current.detailFields.map((item, itemIndex) => itemIndex === index ? { ...item, selector: event.target.value } : item) } : current)} /></label>
          <button disabled={busy} onClick={() => setRecoveryEditor(current => current ? { ...current, detailFields: current.detailFields.filter((_, itemIndex) => itemIndex !== index) } : current)}>{ja?"詳細項目を削除":"Remove detail field"}</button>
        </li>)}</ul>
      </>}
      <label>{ja?"新しい項目名":"New field name"}<input aria-label={ja?"復旧用の新しい項目名":"New recovery field name"} value={recoveryFieldName} onChange={event => setRecoveryFieldName(event.target.value)} maxLength={120} /></label>
      <label>{ja?"新しい項目のセレクター":"New field selector"}<input aria-label={ja?"復旧用の新しいセレクター":"New recovery field selector"} value={recoveryFieldSelector} onChange={event => setRecoveryFieldSelector(event.target.value)} maxLength={512} /></label>
      <button disabled={busy || !recoveryFieldName.trim() || !recoveryFieldSelector.trim() || recoveryEditor.fields.some(field => field.name === recoveryFieldName.trim())}
        onClick={() => { setRecoveryEditor(current => current ? { ...current, fields: [...current.fields, { name: recoveryFieldName.trim(), selector: recoveryFieldSelector.trim(), extract: "text", attribute: null }] } : current); setRecoveryFieldName(""); setRecoveryFieldSelector(""); }}>Add field</button>
      <button disabled={busy || !recoveryEditor.fields.length} onClick={() => void saveRecoveryEditor()}>{ja?"案として保存":"Save as draft"}</button>
      <button disabled={busy} onClick={() => setRecoveryEditor(null)}>{ja?"キャンセル":"Cancel"}</button>
    </section>}
    <details><summary>{ja?"レシピをインポート／エクスポート":"Import / export a Recipe"}</summary>
      <label>{ja?"レシピJSON":"Recipe JSON"}<textarea value={recipeImport} onChange={e => setRecipeImport(e.target.value)} /></label>
      <button disabled={busy || !recipeImport.trim()} onClick={() => { setBusy(true); void Promise.resolve().then(() => api("/web/recipes/import", { method: "POST", body: JSON.stringify(JSON.parse(recipeImport)) })).then(refreshRecipes).then(() => setStatus("Recipe imported. Preview and validate it before running.")).catch(reason => setStatus(reason.message)).finally(() => setBusy(false)); }}>{ja?"レシピをインポート":"Import Recipe"}</button>
      {download && recipes.map(recipe => <button key={recipe.id} onClick={() => { void download(`/web/recipes/${encodeURIComponent(recipe.id)}/download`, "olcr-recipe.json").catch(reason => setStatus(reason.message)); }}>Export {recipe.name}</button>)}
    </details>
    {recipePreview && <section className="scrape-recipe-preview" aria-label={ja?"レシピのプレビュー":"Recipe preview"}><h3>{ja?"プレビュー":"Preview"} · {recipePreview.name}</h3>
      <p>{ja?"読み取り専用のサンプルです。このプレビューではページ巡回やレコードのエクスポートは行いません。":"Read-only sample. This preview does not run pagination or export records."}</p><pre>{JSON.stringify(recipePreview.rows, null, 2)}</pre>
    </section>}
    {networkEvidence.length > 0 && <section aria-label={ja?"観測したネットワークリクエスト":"Observed network requests"}><h3>{ja?"Fetch/XHRの根拠":"Fetch/XHR evidence"}</h3>
      <p>{ja?"メタデータにクエリー値とヘッダーは含まれません。JSON本文の確認には明示的な操作が必要です。同一オリジンへの成功したGET応答のみ対象となり、一時プレビューでは機密キー名を伏せ、保存しません。":"Metadata omits query values and headers. Inspecting a JSON body is a separate explicit action; only same-origin successful GET responses are eligible, and the transient preview redacts sensitive-key names and is never persisted."}</p>
      <ul>{networkEvidence.map(item => <li key={item.index}><code>{item.method}</code> <code>{item.status_code ?? "pending"}</code> <code>{item.url}</code>
        {item.method === "GET" && item.status_code !== null && item.status_code !== undefined && item.status_code >= 200 && item.status_code < 300 &&
          <button disabled={busy} onClick={() => void inspectNetworkJson(item.index)}>{ja?"JSONレスポンスを確認":"Review JSON response"}</button>}</li>)}</ul>
      {networkJsonEvidence && <div role="group" aria-label={ja?"確認済みJSON応答":"Reviewed JSON response"}><h4>{ja?"応答":"Response"} {networkJsonEvidence.request_index} · {networkJsonEvidence.content_type}</h4>
        <p>{networkJsonEvidence.url} · transient preview{networkJsonEvidence.truncated ? " (redacted or truncated)" : ""}. Treat all values as untrusted page data.</p>
        <pre>{JSON.stringify(networkJsonEvidence.preview, null, 2)}</pre>
        <section aria-label={ja?"JSONレシピのマッピングを作成":"Create JSON Recipe mapping"}>
          <h4>{ja?"この応答をレシピにマッピング":"Map this response into a Recipe"}</h4>
          <p>{ja?"応答本文は保存されません。レシピに保存するのは同一オリジンのパスと指定したJSON Pointerのみです。実行のたびにPlaywrightで応答を再取得し、通常のプレビュー・検証を行います。":"The response body is not saved. The Recipe stores only this same-origin path and the JSON Pointers you specify; each run re-reads the response through Playwright and applies the normal Preview & validate gate."}</p>
          <label>{ja?"JSONレシピ名":"JSON Recipe name"}<input aria-label={ja?"JSONレシピ名":"JSON Recipe name"} value={recipeName} onChange={event => setRecipeName(event.target.value)} maxLength={120} /></label>
          <label>{ja?"レコードのJSON Pointer":"Records JSON Pointer"}<input aria-label={ja?"レコードのJSON Pointer":"Records JSON Pointer"} value={jsonRecordsPointer} onChange={event => setJsonRecordsPointer(event.target.value)} maxLength={512} placeholder="/data/items" /></label>
          <label>{ja?"項目名":"Field name"}<input aria-label={ja?"JSONレシピの項目名":"JSON Recipe field name"} value={jsonFieldName} onChange={event => setJsonFieldName(event.target.value)} maxLength={120} /></label>
          <label>{ja?"項目のJSON Pointer":"Field JSON Pointer"}<input aria-label={ja?"項目のJSON Pointer":"Field JSON Pointer"} value={jsonFieldPointer} onChange={event => setJsonFieldPointer(event.target.value)} maxLength={512} placeholder="/title" /></label>
          <button disabled={busy || !jsonFieldName.trim() || jsonFieldMappings.some(field => field.name === jsonFieldName.trim()) || jsonFieldMappings.length >= 50}
            onClick={() => { setJsonFieldMappings(current => [...current, { name: jsonFieldName.trim(), pointer: jsonFieldPointer }]); setJsonFieldName(""); setJsonFieldPointer(""); }}>Add JSON field</button>
          {jsonFieldMappings.length > 0 && <ul aria-label={ja?"JSONレシピの項目":"JSON Recipe fields"}>{jsonFieldMappings.map(field => <li key={field.name}><code>{field.name}</code> ← <code>{field.pointer || "(record value)"}</code>
            <button aria-label={ja?`JSON項目 ${field.name} を削除`:`Remove JSON field ${field.name}`} onClick={() => setJsonFieldMappings(current => current.filter(item => item.name !== field.name))}>Remove</button></li>)}</ul>}
          <label>{ja?"最大レコード数":"Maximum records"}<input aria-label={ja?"JSONレシピの最大レコード数":"JSON Recipe maximum records"} type="number" min={1} max={100000} value={maxRecords} onChange={event => setMaxRecords(Number(event.target.value))} /></label>
          <label>{ja?"出力形式":"Output format"}<select aria-label={ja?"JSONレシピの出力形式":"JSON Recipe output format"} value={outputFormat} onChange={event => setOutputFormat(event.target.value)}><option value="csv">CSV</option><option value="json">JSON</option><option value="jsonl">JSONL</option></select></label>
          <button disabled={busy || !page?.url || !jsonFieldMappings.length} onClick={() => void saveNetworkJsonRecipe()}>{ja?"JSONレシピ案を保存":"Save JSON Recipe draft"}</button>
        </section>
      </div>}
    </section>}
    <p className="scrape-status" role="status">{status}</p>
    {page && (embedded ? <details className="scrape-evidence scrape-evidence-details">
      <summary>{ja?"ページのスナップショットと診断":"Page snapshot and diagnostics"}</summary>
      <div><small>{page.url}</small><h3>{page.title || "Page snapshot"}</h3></div>
      <pre>{page.clean_markdown || "No accessible page content was returned."}</pre>
      {page.warnings?.map(warning => <p className="muted" key={warning}>{warning}</p>)}
    </details> : <article className="scrape-evidence">
      <div><small>{page.url}</small><h3>{page.title || "Page snapshot"}</h3></div>
      <pre>{page.clean_markdown || "No accessible page content was returned."}</pre>
      {page.warnings?.map(warning => <p className="muted" key={warning}>{warning}</p>)}
    </article>)}
  </section>;
}
