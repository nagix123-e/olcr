"""Serial, persisted coding-task planning primitives for the desktop API."""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path
import threading
import uuid
from typing import Any, Mapping

from .capability_scope import capability_evidence


_SLOT = threading.Lock()
MAX_RETRIES_PER_PHASE = 2
MAX_SUBSTANTIAL_REPLANS_PER_TASK = 2
MAX_TASKS_PER_PHASE = 15
MAX_CORRECTIVE_RETRIES = 3

_STRUCTURAL_SIGNALS = {
    "new_application": r"(?:新しい|new).{0,24}(?:アプリ|application|feature)",
    "database": r"(?<![a-z0-9_])(?:crud|database|sqlite|db)(?![a-z0-9_])|認証|データベース",
    "frontend": r"(?<![a-z0-9_])(?:frontend|react|ui)(?![a-z0-9_])|フロントエンド",
    "backend": r"(?<![a-z0-9_])(?:backend|fastapi|apis?|server endpoints?)(?![a-z0-9_])|バックエンド",
    "full_stack": r"full[ -]?stack|フルスタック",
    "dependencies": r"(?<![a-z0-9_])(?:npm|framework|dependenc(?:y|ies)|package|anime(?:\.js)?)(?![a-z0-9_])|依存|フレームワーク",
}

_NON_CURRENT_SECTIONS = re.compile(r"(?:future|later|example|reference|regression|verification|failure.?handling|reporting|fix.?benchmark|product.?description|architecture.?description|previous|conversation|history|assistant|将来|後で|例|参照|回帰|検証|失敗時|報告|製品説明|アーキテクチャ説明|履歴|会話)", re.I)
_CURRENT_SECTIONS = re.compile(r"(?:current.?task|required|implementation|scope|technology|mcp|do.?not.?implement|exclusion|現在のタスク|要件|実装|対象|技術|必須|除外)", re.I)

def _current_task_text(text: str) -> str:
    """Exclude labelled future/reference material from execution control data."""
    current=[]; ignored=False
    for raw in (text or "").splitlines():
        heading=re.sub(r"^[\s#>*\d.、)（）、-]+", "", raw).strip().rstrip(":：")
        if raw.lstrip().startswith("#") or (len(heading) < 80 and raw.rstrip().endswith((":", "："))):
            if _NON_CURRENT_SECTIONS.search(heading):
                ignored=True; continue
            if _CURRENT_SECTIONS.search(heading):
                ignored=False
        if not ignored: current.append(raw)
    return "\n".join(current)


def _architecture_clauses(text: str) -> list[tuple[str, bool]]:
    """Legacy clause view for static-plan compaction and retry instructions.

    Canonical capabilities use capability_evidence instead; these existing
    consumers retain their pre-existing clause interpretation.
    """
    clauses = []
    excluded_section = False
    for line in _current_task_text(text).lower().splitlines():
        heading = re.sub(r"^[\s#*\d.、)（）、-]+", "", line).strip().rstrip(":：")
        if re.fullmatch(r"実装しないもの|対象外|除外(?:事項)?|non[- ]?goals|exclusions|out of scope|do not implement", heading):
            excluded_section = True
            continue
        if line.rstrip().endswith((":", "：")) and re.fullmatch(r"frontend|backend|database|api|authentication|docker|cms|analytics", heading):
            continue
        if re.match(r"^\s*#{1,6}\s", line) or re.fullmatch(r"実装するもの|要件|検証|完了条件|requirements|verification|required", heading):
            excluded_section = False
        for sentence in re.split(r"[。;；!\n]|\.(?=\s|$)|(?<!\w)but\s+|ただし|一方", line):
            inherited_negative = bool(re.match(r"^\s*[-*]?\s*(?:no|without|do not|don't)\b", sentence) or re.search(r"(?:は|を)?(?:不要|なし|使わない|使用しない)\s*$", sentence))
            for clause in re.split(r"[,、]|\band\b|(?:ですが|だが)", sentence):
                positive_request = bool(re.search(r"\b(?:use|build|implement|require|add)\b|(?:使う|使用する|実装する|必要です)", clause))
                clause_negative = bool(re.search(r"\b(?:no|without|not|never|exclude|excluding)\b|不要|なし|使わない|使用しない|実装しない|導入しない|含めない|対象外", clause))
                if re.search(r"(?:不要(?:な)?|unnecessary|unrelated|関係ない|無関係).{0,32}(?:dependenc|依存|frontend|フロント)", clause, re.I):
                    continue
                if positive_request and not clause_negative:
                    inherited_negative = False
                clauses.append((clause, excluded_section or inherited_negative or clause_negative))
    return clauses


def execution_mode_diagnostics(text: str) -> dict[str, Any]:
    positive, negated = set(), set()
    for item in capability_evidence(text, _STRUCTURAL_SIGNALS):
        if item["active_for_control"] and item["category"] == "capability":
            (negated if item["polarity"] == "forbidden" else positive).add(item["capability"])
    full_stack = "full_stack" in positive or {"frontend", "backend"} <= positive or {"frontend", "database"} <= positive
    broad = len(positive & {"new_application", "database", "frontend", "backend"})
    heavy = full_stack or broad >= 3
    return {"HEAVY_SIGNALS_DETECTED": sorted(positive | negated),
            "POSITIVE_SIGNALS": sorted(positive), "NEGATED_SIGNALS": sorted(negated),
            "FINAL_CLASSIFICATION_REASON": "POSITIVE_FULL_STACK" if full_stack else "POSITIVE_BROAD_ARCHITECTURE" if heavy else "NO_POSITIVE_HEAVY_ARCHITECTURE",
            "EXECUTION_MODE": "HEAVY_BATCHED" if heavy else "NORMAL"}


def classify_execution_mode(text: str) -> str:
    return execution_mode_diagnostics(text)["EXECUTION_MODE"]


def task_profile(text: str) -> dict[str, Any]:
    """Classify the bounded orchestration shape from requested capabilities.

    This deliberately uses the polarity-aware diagnostics rather than raw
    keyword occurrence.  A marketing site that names its explicitly excluded
    backend/database must never acquire a full-stack execution profile.
    """
    diagnostics = execution_mode_diagnostics(text)
    required = set(diagnostics["POSITIVE_SIGNALS"])
    forbidden = set(diagnostics["NEGATED_SIGNALS"])
    value = _current_task_text(text).lower()
    frontend_stack = bool(required & {"frontend", "dependencies"}) and bool(re.search(
        r"(?<![a-z0-9_])(?:react|typescript|vite|tailwind|shadcn)(?![a-z0-9_])|マーケティング|landing\s+page|ランディング", value))
    frontend_only = frontend_stack and not required.intersection({"backend", "database", "full_stack"})
    profile = "FRONTEND_ONLY_MARKETING_SITE" if frontend_only else "GENERAL_CODING"
    mode = "NORMAL" if frontend_only else diagnostics["EXECUTION_MODE"]
    return {**diagnostics, "TASK_PROFILE": profile, "EXECUTION_MODE": mode,
            "REQUIRED_CAPABILITIES": sorted(required), "FORBIDDEN_CAPABILITIES": sorted(forbidden)}


def required_mcp_contract(text: str) -> list[str]:
    """Return only MCPs the user explicitly required, in stable order.

    The contract is intentionally line-oriented: an explicit opt-out must win
    even when an earlier line asked for the same server.  This keeps a copied
    requirement template from silently re-enabling a tool the user excluded.
    """
    value = _current_task_text(text)
    patterns = {
        "animejs": (
            r"(?:use|consult|required?\s+to\s+use)\s+(?:the\s+)?anime(?:\.js)?\s*mcp",
            r"anime(?:\.js)?\s*mcp\s*(?:を)?\s*(?:実際に)?(?:使用|使)",
            r"anime\.js\s*(?:を)?\s*(?:使用|使)",
        ),
        "shadcn": (
            r"(?:use|consult|required?\s+to\s+use)\s+(?:the\s+)?shadcn(?:/ui)?\s*mcp",
            r"shadcn(?:/ui)?\s*mcp\s*(?:を)?\s*(?:実際に)?(?:使用|使)",
        ),
        "playwright": (
            r"(?:use|consult|required?\s+to\s+use)\s+(?:the\s+)?playwright\s*mcp",
            r"playwright\s*mcp\s*(?:を)?\s*(?:実際に)?(?:使用|使)",
            r"playwright(?:\s*mcp)?\s*(?:for|で)?.{0,80}(?:browser\s*)?(?:verify|verification|検証|確認)",
        ),
    }
    required: list[str] = []
    listed_mcp_use=bool(re.search(r"(?:anime(?:\.js)?|shadcn|playwright).{0,120}(?:mcp).{0,120}(?:使用|使|use)", value, re.I))
    for name in ("animejs", "shadcn", "playwright"):
        token = r"shadcn(?:/ui)?" if name == "shadcn" else (r"anime(?:\.js)?" if name == "animejs" else r"playwright")
        opt_out = rf"(?:do\s+not(?:\s+use)?|don't(?:\s+use)?|without|no|never|使用しない|使わない|不要|禁止)\s*(?:the\s+)?{token}(?:\s*mcp)?|{token}(?:\s*mcp)?\s*(?:を)?\s*(?:使用しない|使わない|不要|禁止)"
        if re.search(opt_out, value, re.I):
            continue
        if (listed_mcp_use and re.search(token, value, re.I)) or any(re.search(pattern, value, re.I) for pattern in patterns[name]):
            required.append(name)
    return required


def animejs_auto_selected(text: str) -> bool:
    """Select the offline reference helper for material frontend motion only."""
    value = text or ""
    opt_out = r"(?:anime(?:\.js)?\s*(?:を)?\s*(?:使用しない|使わない)|アニメーション不要|do\s+not\s+use\s+anime(?:\.js)?|no\s+animation)"
    if re.search(opt_out, value, re.I):
        return False
    frontend = bool(re.search(r"react|typescript|vite|frontend|front.?end|ui|ページ|画面|hero|landing", value, re.I))
    motion = bool(re.search(r"(?:animation|motion|animated\s+hero|timeline|stagger|svg\s+animation|scroll\s+animation|text\s+animation|interactive\s+motion|drag\s+animation|アニメーション|モーション|スクロール|スタガー)", value, re.I))
    return frontend and motion


def animejs_project_version(workspace_root: str | None) -> dict[str, str]:
    """Return deterministic local dependency evidence; never infer a major."""
    unknown = {"ANIMEJS_PROJECT_DEPENDENCY_PRESENT": "UNKNOWN", "ANIMEJS_PROJECT_DECLARED_VERSION": "UNKNOWN",
               "ANIMEJS_PROJECT_VERSION": "UNKNOWN", "ANIMEJS_PROJECT_MAJOR_VERSION": "UNKNOWN"}
    if not workspace_root:
        return unknown
    root = Path(workspace_root)
    package = root / "package.json"
    declared = None
    try:
        value = json.loads(package.read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            candidate = value.get(section, {}).get("animejs") if isinstance(value.get(section), dict) else None
            if isinstance(candidate, str):
                declared = candidate
                break
    except (OSError, ValueError):
        return unknown
    if declared is None:
        # A lockfile or installed package is authoritative only when the
        # manifest is unavailable/indirect; never synthesize a version.
        observed = None
        for lock_name in ("package-lock.json", "npm-shrinkwrap.json"):
            try:
                lock = json.loads((root / lock_name).read_text(encoding="utf-8"))
                observed = ((lock.get("packages") or {}).get("node_modules/animejs") or {}).get("version")
                observed = observed or ((lock.get("dependencies") or {}).get("animejs") or {}).get("version")
                if isinstance(observed, str):
                    break
            except (OSError, ValueError):
                pass
        if not isinstance(observed, str):
            try:
                observed = json.loads((root / "node_modules" / "animejs" / "package.json").read_text(encoding="utf-8")).get("version")
            except (OSError, ValueError):
                observed = None
        if not isinstance(observed, str):
            return {"ANIMEJS_PROJECT_DEPENDENCY_PRESENT": "NO", "ANIMEJS_PROJECT_DECLARED_VERSION": "NONE",
                    "ANIMEJS_PROJECT_VERSION": "NONE", "ANIMEJS_PROJECT_MAJOR_VERSION": "NONE"}
        declared = observed
    major = re.search(r"(?:^|[^0-9])([0-9]+)\.", declared)
    return {"ANIMEJS_PROJECT_DEPENDENCY_PRESENT": "YES", "ANIMEJS_PROJECT_DECLARED_VERSION": declared,
            "ANIMEJS_PROJECT_VERSION": declared, "ANIMEJS_PROJECT_MAJOR_VERSION": major.group(1) if major else "UNKNOWN"}


def animejs_version_compatibility(evidence: dict[str, str], goal: str = "") -> str:
    """Gate the v4 corpus; v3 is never silently treated as v4."""
    major = evidence.get("ANIMEJS_PROJECT_MAJOR_VERSION")
    if major == "4":
        return "PASS"
    if major == "3":
        return "MIGRATION_REQUESTED" if re.search(r"Anime\.js\s*v?4\s*(?:へ|に)?移行|migrat(?:e|ion).{0,30}anime", goal or "", re.I) else "CONFLICT_V3_V4"
    if major == "NONE":
        return "NO_DEPENDENCY"
    return "UNKNOWN"


def required_verification_contract(text: str, required_mcps: list[str] | None = None) -> list[str]:
    """Persist only verification commands explicitly named by the request."""
    value = text or ""
    required: list[str] = []
    if re.search(r"(?:npm\s+run\s+)?typecheck|型(?:チェック|検査)を(?:実行|検証)", value, re.I):
        required.append("typecheck")
    if re.search(r"npm\s+run\s+build|(?:run|verify|check|perform)\s+(?:the\s+)?build\b|build\s+(?:verification|check)|ビルド(?:を)?(?:実行|検証|確認)", value, re.I):
        required.append("build")
    if "playwright" in (required_mcps or []) or re.search(r"(?:browser|ブラウザ).{0,40}(?:verify|verification|検証|確認)", value, re.I):
        required.append("browser")
    return required


def canonical_coding_requirements(text: str) -> dict[str, Any]:
    """Produce the single persisted control-plane interpretation of a request."""
    profile = task_profile(text)
    required_mcps = required_mcp_contract(text)
    mutation_mode, fix_reason = classify_mutation_mode(text)
    scoped_text = _current_task_text(text)
    dependency_policy = ("MINIMAL" if re.search(
        r"(?:unnecessary|unrelated|不要(?:な)?|関係ない|無関係).{0,32}(?:dependenc|依存)",
        scoped_text, re.I) else "UNSPECIFIED")
    return {"required_capabilities": profile["REQUIRED_CAPABILITIES"],
            "forbidden_capabilities": profile["FORBIDDEN_CAPABILITIES"],
            "task_profile": profile["TASK_PROFILE"],
            "execution_mode": profile["EXECUTION_MODE"],
            # ``required_mcp`` remains for stored-task compatibility.  The
            # plural key is the canonical contract for new consumers.
            "required_mcp": required_mcps,
            "required_mcps": required_mcps,
            "selected_mcps": (["animejs"] if "animejs" not in required_mcps and animejs_auto_selected(text) else []),
            "required_verification": required_verification_contract(text, required_mcps),
            "dependency_policy": dependency_policy,
            "forbidden_technologies": sorted({item["capability"] for item in capability_evidence(text, _STRUCTURAL_SIGNALS)
                if item["category"] == "technology" and item["active_for_control"] and item["polarity"] == "forbidden"}),
            "mutation_mode": mutation_mode,
            "fix_reason": fix_reason,
            "preflight": {}}


def _capability_provenance(text: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain active and rejected candidates with structural scope evidence."""
    return capability_evidence(text, _STRUCTURAL_SIGNALS)


def normalize_coding_requirements(raw_request: str | Mapping[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Single authoritative entry point for focused and production paths.

    ``current_user_text`` is the only prose interpreted by the control plane.
    Typed project metadata and model context are carried for diagnostics but
    are never concatenated into that text.
    """
    request_object: Mapping[str, Any]
    if isinstance(raw_request, Mapping):
        request_object = raw_request
        raw = str(request_object.get("current_user_text") or request_object.get("message") or "")
        typed_context = request_object.get("project_metadata") or request_object.get("typed_project_metadata") or {}
        model_context = request_object.get("model_context") or {}
    else:
        raw = str(raw_request or "")
        typed_context = {}
        model_context = {}
    if context:
        # Backward-compatible callers may provide typed context separately;
        # this remains metadata and is intentionally excluded from ``raw``.
        typed_context = {**(typed_context if isinstance(typed_context, Mapping) else {}), **context}
        if isinstance(context.get("model_context"), Mapping):
            model_context = context["model_context"]
    result = canonical_coding_requirements(raw)
    scoped = _current_task_text(raw)
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True)
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    model_context_json = json.dumps(model_context, ensure_ascii=False, sort_keys=True, default=str)
    provenance = _capability_provenance(raw, result)
    result["normalization_diagnostics"] = {
        "raw_request_hash": raw_hash,
        "current_user_text_hash": raw_hash,
        "normalizer_control_input_hash": raw_hash,
        "model_context_hash": hashlib.sha256(model_context_json.encode("utf-8")).hexdigest(),
        "normalizer_input_char_count": len(raw),
        "current_user_text_char_count": len(raw),
        "scoped_input_hash": hashlib.sha256(scoped.encode("utf-8")).hexdigest(),
        "canonical_requirements_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "canonical_requirements_valid": not bool(set(result["required_capabilities"]) & set(result["forbidden_capabilities"])),
        "capability_provenance": provenance,
        "typed_project_metadata_present": bool(typed_context),
        "project_context_added_before_normalization": False,
        "conversation_history_added_before_normalization": False,
        "assistant_history_added_before_normalization": False,
    }
    return result


def phase_has_observable_deliverable(phase: dict[str, Any]) -> bool:
    """Reject orchestration-only rows before they reach the implementation executor."""
    if _is_orchestration_preflight_phase(phase) or _is_final_report_phase(phase):
        return False
    return bool(str(phase.get("goal") or "").strip() or phase.get("done") or phase.get("verify"))


def _phase_text(phase: dict[str, Any]) -> str:
    return " ".join([str(phase.get("goal") or ""), *map(str, phase.get("done") or []), *map(str, phase.get("verify") or [])]).lower()


def _is_final_report_phase(phase: dict[str, Any]) -> bool:
    value = _phase_text(phase)
    report = bool(re.search(r"final\s*report|completion\s*report|(?:最終|完了)\s*(?:報告|レポート)|報告書|summary", value, re.I))
    work = bool(re.search(r"implement|create|build|write|edit|fix|browser|verify|test|実装|作成|構築|ブラウザ|検証|テスト", value, re.I))
    return report and not work


def _is_browser_verification_phase(phase: dict[str, Any]) -> bool:
    goal = str(phase.get("goal") or "")
    browser = bool(re.search(r"browser|playwright|ブラウザ", goal, re.I))
    verification = bool(re.search(r"verify|verification|test|check|検証|確認|テスト", goal, re.I))
    implementation = bool(re.search(r"implement|create|build|write|edit|fix|実装|作成|構築|編集|修正", goal, re.I))
    return browser or (verification and not implementation)


def _is_orchestration_preflight_phase(phase: dict[str, Any]) -> bool:
    """Recognize phase rows whose work was already completed by OLCR.

    They are never valid inputs to the filesystem implementation executor.
    A phase which also names a concrete implementation deliverable remains a
    normal implementation phase.
    """
    value = _phase_text(phase)
    preflight = bool(re.search(r"repo(?:sitory)?\s*(?:inspection|check)|repository\s*structure|mcp\s*(?:consultation|use|check)|shadcn\s*mcp|planning|analysis|(?:リポジトリ|repo)\s*(?:構成|確認)|mcp\s*(?:利用|確認)|計画|分析", value, re.I))
    deliverable = bool(re.search(r"implement|create|build|write|edit|fix|add|config|source|ui\s*(?:design|implementation)|実装|作成|構築|編集|修正|追加|設定|ソース|ui\s*(?:デザイン|実装)", value, re.I))
    return preflight and not deliverable


def _phase_task(phase: dict[str, Any], task_id: str, depends_on: list[str], required_mcp: list[str]) -> dict[str, Any]:
    return {"task_id": task_id, "phase_id": str(phase["id"]), "goal": str(phase.get("goal") or ""),
            "domain": "verification" if _is_browser_verification_phase(phase) else "implementation",
            "depends_on": depends_on, "required_context": [], "required_mcp": required_mcp,
            "change_scope": [], "verification": list(phase.get("verify") or []),
            "done_condition": list(phase.get("done") or []), "retry_state": {}}


def normalize_task_graph(plan: dict[str, Any], profile: str, required_mcp: list[str]) -> dict[str, Any]:
    """Make bounded profiles deterministic after model planning.

    A final report belongs to the orchestrator, not the task graph.  For the
    frontend benchmark all implementation work is one phase and browser work
    is one consolidated final phase, so repeated semantic phases cannot grow
    with planner wording.
    """
    phases = [dict(item) for item in (plan.get("phases") or []) if isinstance(item, dict)
              and not _is_final_report_phase(item)
              and not _is_orchestration_preflight_phase(item)]
    if profile != "FRONTEND_ONLY_MARKETING_SITE" or not phases:
        normalized = {**plan, "phases": phases}
        if len(phases) != len(plan.get("phases") or []):
            # A generated task graph refers to the removed report phase.  Let
            # plan validation derive a fresh graph from the remaining phases.
            normalized.pop("tasks", None)
        return normalized
    verification = [item for item in phases if _is_browser_verification_phase(item)]
    implementation = [item for item in phases if item not in verification]
    if not implementation:
        implementation, verification = phases[:1], phases[1:]
    merged = _merge_static_phases(implementation)
    merged["id"] = "p1"
    merged["dependencies"] = []
    merged["acceptance_contract"] = ["FRONTEND_STACK", "MARKETING_STRUCTURE"]
    normalized = [merged]
    if verification:
        final = _merge_static_phases(verification)
        final["id"] = "p2"
        final["goal"] = "Consolidated browser verification: desktop, mobile, navigation, responsive, console, and accessibility smoke"
        final["dependencies"] = [merged["id"]]
        normalized.append(final)
    elif "playwright" in required_mcp:
        normalized.append({"id": "p2", "goal": "Consolidated browser verification: desktop, mobile, navigation, responsive, console, and accessibility smoke",
                           "status": "pending", "done": ["Playwright browser verification completed"],
                           "verify": ["desktop, mobile, navigation, responsive, console, and accessibility smoke pass"],
                           "dependencies": [merged["id"]], "risks": ["browser verification may expose a bounded correction"]})
    tasks = []
    for index, phase in enumerate(normalized):
        phase_mcp = [name for name in required_mcp if (name == "playwright") == (index == len(normalized) - 1 and _is_browser_verification_phase(phase))]
        if index == 0:
            phase_mcp = [name for name in required_mcp if name != "playwright"]
        phase["required_mcp"] = phase_mcp
        tasks.append(_phase_task(phase, f"{phase['id']}-task", [tasks[-1]["task_id"]] if tasks else [], phase_mcp))
    return {**plan, "phases": normalized, "tasks": tasks}


def frontend_stack_acceptance(workspace_root: str | None) -> bool:
    """Check requested React/TS/Vite/Tailwind/shadcn artifacts, not prose."""
    if not workspace_root:
        return False
    root = Path(workspace_root)
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    names = set(deps)
    react = "react" in names and ("react-dom" in names or "@vitejs/plugin-react" in names)
    tailwind = "tailwindcss" in names
    shadcn = "@/components/ui" in " ".join(str(item) for item in package.values()) or (root / "components.json").is_file()
    typescript = "typescript" in names and any(root.glob("tsconfig*.json"))
    vite = "vite" in names and any(root.glob("vite.config.*"))
    return react and typescript and vite and tailwind and shadcn


def marketing_design_structure_acceptance(workspace_root: str | None) -> bool:
    """Require observable requested section labels without subjective scoring."""
    if not workspace_root:
        return False
    root = Path(workspace_root)
    text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in root.rglob("*.*")
                     if path.suffix.lower() in {".tsx", ".ts", ".jsx", ".js", ".html"} and path.is_file())
    lower = text.lower()
    required = ("header", "hero", "feature", "workflow", "development tools", "cta", "footer", "links")
    return all(item in lower for item in required) and ("product" in lower or "visual" in lower)


def _simple_static_site(goal: str) -> bool:
    value = (goal or "").lower()
    static_signal = bool(re.search(r"static\s+(?:html\s+)?(?:site|website)|静的(?:な)?(?:サイト|webサイト)|\.html|html\s*[/+と&]\s*css", value))
    positive = set(execution_mode_diagnostics(goal)["POSITIVE_SIGNALS"])
    return static_signal and not positive.intersection({"backend", "database", "full_stack", "dependencies"}) and not any(
        not negative and re.search(r"(?<![a-z0-9_])react(?![a-z0-9_])", clause) for clause, negative in _architecture_clauses(goal))


def resumable_continuation_eligible(task: dict[str, Any]) -> bool:
    """Persisted resumability is authoritative; it never grants authorization."""
    return task.get("status") == "RESUMABLE" and not task.get("pending_authorization") and not task.get("archived")


def workspace_mutation_count(report: dict[str, Any]) -> int:
    return sum(op.get("tool") in {"workspace_write", "workspace_write_normalized", "workspace_patch", "workspace_delete", "workspace_remove"}
               for op in _successful_typed_operations(report))


def zero_mutation_retry_instruction(phase: dict[str, Any], report: dict[str, Any],
                                    prior_reports: list[dict[str, Any]] | None = None,
                                    workspace_root: str | None = None) -> str:
    phase_work = " ".join([str(phase.get("goal") or ""), *map(str, phase.get("done") or [])])
    # Creating a prose report or plan is not a repository edit. Keep explicit
    # report.md (and other named file) creation eligible like any file change.
    phase_work = re.sub(r"(?:write|create|build)\s+(?:(?:a|the|final)\s+)*(?:report|plan)(?![\w.])|(?:報告|レポート|計画)(?:書)?を(?:作成|追加)", "", phase_work, flags=re.I)
    phase_work = " ".join(clause for clause, negative in _architecture_clauses(phase_work) if not negative)
    requires_mutation = bool(re.search(r"\b(?:implement|create|build|write|edit|modify|update|fix|delete|remove|add)\b|(?:実装|作成|変更|修正|追加|削除)(?!内容|結果|済み|後|した|された|の確認|の検証|を確認)", phase_work, re.I))
    if requires_mutation and workspace_mutation_count(report) == 0 and evaluate_phase(phase, report, prior_reports, workspace_root)["done_unmet"]:
        instruction = ("The previous attempt produced no authoritative workspace mutation. "
                       "Do not only explain the change. Perform the required edits in the authorized repository scope "
                       "and then verify the observable result.")
        typed = report.get("typed_execution_summary") or {}
        if "patch precondition failed" in str(typed.get("error") or "").lower():
            instruction += (" The previous patch precondition failed. Re-read existing targets before patching; "
                            "for missing files use a write operation with complete content.")
        return instruction
    return ""


def _merge_texts(values: list[Any]) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in value if isinstance(value, list) else [value]:
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def _merge_static_phases(phases: list[dict[str, Any]]) -> dict[str, Any]:
    first = phases[0]
    return {
        **first,
        "goal": " / ".join(str(phase.get("goal") or "").strip() for phase in phases if str(phase.get("goal") or "").strip()),
        "status": "pending",
        "done": _merge_texts([phase.get("done") for phase in phases]),
        "verify": _merge_texts([phase.get("verify") for phase in phases]),
        "dependencies": [],
        "risks": _merge_texts([phase.get("risks") for phase in phases]),
    }


def compact_normal_plan(plan: dict[str, Any], goal: str) -> dict[str, Any]:
    """Merge only clearly redundant micro-phases in a small static-site plan.

    This is deliberately post-validation and deterministic: it never discards a
    Done/Verify/Risk requirement, and does not affect complex NORMAL tasks.
    """
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not _simple_static_site(goal) or not isinstance(phases, list) or len(phases) <= 3:
        return plan
    phase_rows = [phase for phase in phases if isinstance(phase, dict)]
    if len(phase_rows) != len(phases) or not phase_rows:
        return plan
    implementation_pattern = re.compile(r"(?:implement|create|build|write|style|responsive|accessib|html|css|page|実装|作成|構築|スタイル|レスポンシブ|アクセシビリティ|ページ|ファイル)", re.I)
    verification_pattern = re.compile(r"(?:verify|verification|browser|test|check|確認|検証|ブラウザ|テスト)", re.I)
    last = phase_rows[-1]
    last_text = " ".join([str(last.get("goal") or ""), *[str(x) for x in last.get("done") or []], *[str(x) for x in last.get("verify") or []]])
    final_verification = bool(verification_pattern.search(last_text) and not implementation_pattern.search(str(last.get("goal") or "")))
    implementation_rows = phase_rows[:-1] if final_verification else phase_rows
    if not implementation_rows:
        return plan
    merged = _merge_static_phases(implementation_rows)
    compacted_phases = [merged]
    if final_verification:
        compacted_phases.append({**last, "status": "pending", "dependencies": [merged["id"]]})

    compacted = {**plan, "phases": compacted_phases}
    tasks = plan.get("tasks")
    if isinstance(tasks, list):
        by_phase = {str(task.get("phase_id")): task for task in tasks if isinstance(task, dict)}
        source_tasks = [by_phase.get(str(phase.get("id"))) for phase in implementation_rows]
        if any(task is None for task in source_tasks):
            return plan
        first_task = source_tasks[0]
        merged_task = {
            **first_task,
            "task_id": str(first_task.get("task_id") or merged["id"]),
            "phase_id": merged["id"],
            "goal": merged["goal"],
            "depends_on": [],
            "required_context": _merge_texts([task.get("required_context") for task in source_tasks]),
            "required_mcp": _merge_texts([task.get("required_mcp") for task in source_tasks]),
            "change_scope": _merge_texts([task.get("change_scope") for task in source_tasks]),
            "verification": _merge_texts([task.get("verification") for task in source_tasks]),
            "done_condition": _merge_texts([task.get("done_condition") for task in source_tasks]),
        }
        compacted_tasks = [merged_task]
        if final_verification:
            final_task = by_phase.get(str(last.get("id")))
            if final_task is None:
                return plan
            compacted_tasks.append({**final_task, "depends_on": [merged_task["task_id"]]})
        compacted["tasks"] = compacted_tasks
    return compacted


_EXPLICIT_NON_CODING = re.compile(
    r"(?:これは\s*(?:コーディング|coding)\s*タスクではありません|(?:コーディング|実装)はしないでください|コード(?:を)?変更は不要です|計画だけ(?:を)?(?:作って|作成|してください)|設計だけ(?:を)?(?:して|してください)|do\s+not\s+implement(?!\s*:)|do\s+not\s+modify\s+code|planning\s+only|design\s+only)",
    re.I,
)
_PLANNING_INTENT = re.compile(
    r"(?:実装計画|実装方針|実装設計|実装方法|計画(?:を)?(?:作(?:って|成)|策定|立案)|設計(?:を)?(?:して|してください|レビュー)|ui\s*(?:案|設計)|画面構成|ボタン構成|アーキテクチャ(?:設計|を整理)|データモデル(?:設計)?|api(?:設計|design)|implementation\s*(?:plan|approach|design)|how\s+should\s+(?:this|it)\s+be\s+implemented|(?:ui|ux)\s*(?:design|proposal)|architecture\s*(?:plan|design)|technical\s*specification|code\s*review|技術相談|レビュー)",
    re.I,
)
_TECHNICAL_NOUNS = re.compile(
    r"(?:repo|repository|workspace|project|コード|ファイル|実装|修正|変更|追加|削除|react|fastapi|database|api|css|html|ui|button|architecture|code)",
    re.I,
)


def _mutation_intent(text: str, planning_intent: bool) -> bool:
    """Recognize an instruction to mutate a repository or product.

    A plan may contain words such as ``実装`` and ``作成``.  When a planning
    scope is present, only a second, concrete execution instruction can turn
    it into a Coding Task.
    """
    value = re.sub(r"\bdo\s+not\s+(?:implement|modify\s+code)\b", "", (text or "").strip(), flags=re.I)
    direct = re.compile(
        r"(?:実装(?:して|してください|しろ|する|を開始)|修正(?:して|してください|しろ|する)|変更(?:して|してください|しろ|する)|追加(?:して|してください|しろ|する)|削除(?:して|してください|しろ|する)|書き換え(?:て|てください|る)|コードを書いて(?:ください)?|ソースコードを作成|置き換え(?:て|てください|る)|ファイルを(?:作成|変更|書き換え)|repo(?:に|の).{0,80}(?:実装|修正|変更|追加|削除)|この計画を(?:実装|実行)(?:して|してください)?|(?:implement|create|build|fix|modify|update|rewrite|delete|add)\s+(?:this|the|a|an|our|my)?\s*(?:repo(?:sitory)?|project|app|application|feature|code|file|plan)|(?:implement|fix|modify|update|rewrite|delete|add)\s+(?:this|the)\s+(?:plan|code|file|feature)|execute\s+(?:the\s+)?(?:implementation|plan)|change\s+this|edit\s+this)",
        re.I,
    )
    if direct.search(value):
        return True
    if planning_intent:
        return False
    # Short imperative application requests remain valid mutation intent even
    # without an explicit repository noun (for example "テトリスを作って").
    return bool(re.search(r"(?:\S+を)?(?:作成(?:して|してください|する)|作って)|(?:create|build)\b.{0,120}\b(?:app|application|website|site|feature)\b", value, re.I))


def coding_classification_diagnostics(text: str, *, project_scoped: bool = False,
                                      attachment_present: bool = False) -> dict[str, str | bool]:
    """Return concise, deterministic routing facts without model inference."""
    value = (text or "").strip()
    planning = bool(_PLANNING_INTENT.search(value))
    explicit_non_coding = bool(_EXPLICIT_NON_CODING.search(value))
    mutation = _mutation_intent(value, planning)
    # A screenshot/report tied to an existing workspace can express a repair
    # request without naming a file or verb such as 「修正して」.  Treat this
    # as execution only when the request is project-scoped; generic support
    # questions remain normal Brain turns.
    contextual_repair = bool(
        project_scoped and attachment_present and not planning and
        re.search(r"(?:問題|不具合|バグ|エラー).{0,48}(?:解決|直(?:して|す)|修正(?:して|する)|fix|resolve)|(?:解決|直(?:して|す)|修正(?:して|する)|fix|resolve).{0,48}(?:問題|不具合|バグ|エラー)", value, re.I)
    )
    mutation = mutation or contextual_repair
    if not value:
        classification, reason = "NON_CODING", "EMPTY_REQUEST"
    elif explicit_non_coding and mutation:
        classification, reason = "AMBIGUOUS", "CONTRADICTORY_MUTATION_AND_NON_CODING_SCOPE"
    elif explicit_non_coding:
        classification, reason = "NON_CODING", "EXPLICIT_NON_CODING_SCOPE"
    elif planning and not mutation:
        classification, reason = "NON_CODING", "DESIGN_PLANNING"
    elif mutation:
        classification, reason = ("CODING", "PROJECT_SCOPED_REPAIR_REQUEST") if contextual_repair else ("CODING", "EXPLICIT_REPOSITORY_MUTATION")
    elif _TECHNICAL_NOUNS.search(value):
        classification, reason = "AMBIGUOUS", "TECHNICAL_CONTEXT_WITHOUT_MUTATION"
    else:
        classification, reason = "NON_CODING", "NO_MUTATION_INTENT"
    return {"classification": classification, "mutation_intent": mutation,
            "planning_intent": planning, "explicit_non_coding": explicit_non_coding,
            "reason": reason}


def classify_coding_request(text: str) -> str:
    """Classify only concrete repository mutation requests as Coding Tasks."""
    return str(coding_classification_diagnostics(text)["classification"])


def classify_mutation_mode(text: str) -> tuple[str, str]:
    """Classify the shape of an already-authorized repository mutation."""
    value=_current_task_text(text).strip()
    explicit_new=bool(re.search(r"(?:新機能|新しい).{0,32}(?:追加|実装|作成)|(?:追加|実装|作成).{0,32}(?:新機能|新しい)|修正ではなく.*(?:新機能|実装)|(?:initial|current).{0,32}(?:implementation|implement).{0,32}(?:not|rather than).{0,16}fix", value, re.I))
    repair=bool(re.search(r"修正|直して|直す|バグ|不具合|壊れて|動かない|反応しない|表示されない|開始しない|保存できない|エラー", value, re.I))
    existing=bool(re.search(r"既存|現在|今実装中|この|ボタン|画面|ゲーム|アニメーション|機能|挙動", value, re.I))
    if explicit_new:
        return "IMPLEMENTATION", "EXPLICIT_NEW_BEHAVIOR"
    if repair and existing:
        return "FIX", "EXISTING_BEHAVIOR_REPAIR"
    if repair:
        return "AMBIGUOUS_MUTATION_MODE", "REPAIR_TARGET_UNSPECIFIED"
    return "IMPLEMENTATION", "NEW_OR_GENERAL_MUTATION"


def validate_task_graph(tasks: Any, max_tasks: int = MAX_TASKS_PER_PHASE) -> list[str]:
    """Validate the bounded, sequential Task Graph representation."""
    if not isinstance(tasks, list) or not tasks:
        return ["tasks must be a non-empty array"]
    if len(tasks) > max_tasks:
        return [f"task count exceeds maximum of {max_tasks}"]
    ids: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            return ["task must be an object"]
        required = {"task_id", "goal", "domain", "depends_on", "required_context", "required_mcp", "change_scope", "verification", "done_condition", "retry_state"}
        if not required <= task.keys():
            return ["task is missing required fields"]
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip() or task_id in ids:
            return ["task ids must be unique"]
        ids.append(task_id)
        if not isinstance(task.get("depends_on"), list) or any(not isinstance(dep, str) for dep in task["depends_on"]):
            return ["invalid task dependencies"]
        if task_id in task["depends_on"]:
            return ["task cannot depend on itself"]
    known = set(ids)
    for task in tasks:
        if any(dep not in known for dep in task["depends_on"]):
            return ["unknown task dependency"]
    visiting: set[str] = set(); visited: set[str] = set(); by_id = {t["task_id"]: t for t in tasks}
    def visit(node: str) -> bool:
        if node in visiting: return False
        if node in visited: return True
        visiting.add(node)
        if any(not visit(dep) for dep in by_id[node]["depends_on"]): return False
        visiting.remove(node); visited.add(node); return True
    if any(not visit(task_id) for task_id in ids):
        return ["task dependency cycle"]
    return []


def classify_waiting_input(text: str) -> str:
    """Classify user input received while a task is WAITING_FOR_USER.

    This is deliberately deterministic.  A question or explanation request
    must not be mistaken for plan-revision guidance, because revision would
    mutate the persisted plan and enqueue a new model run.
    """
    value = (text or "").strip()
    if re.fullmatch(r"(?:再開(?:して)?|resume|続行(?:して)?|continue)", value, re.IGNORECASE):
        return "CONTROL"
    if re.fullmatch(r"(?:一時停止|停止|pause|アーカイブ|archive)", value, re.IGNORECASE):
        return "CONTROL"

    # Explicit change verbs take precedence over interrogative wording (for
    # example: "何を変更すればよい？" remains a clarification).
    revision = re.search(
        r"(?:変更|修正|改訂|改め|直し|直して|変えて|置き換え|移して|追加して|削除して|修正して|変更して|にして|に変更|に修正|change|revise|modify|update|fix)",
        value,
        re.IGNORECASE,
    )
    question = re.search(
        r"(?:\?|？|とは|どういう|なぜ|何が|何を|どの|どこ|どうして|不足|理由|説明|教えて|確認|どうなって|意味|why|what|how|which|explain)",
        value,
        re.IGNORECASE,
    )
    if revision and not (question and not re.search(r"(?:してください|して下さい|してほしい|して欲しい|〜に|にしてください|please|must)", value, re.IGNORECASE)):
        return "REVISION"
    if question:
        return "CLARIFICATION"
    return "AMBIGUOUS"


def plan_schema() -> dict[str, Any]:
    """Canonical JSON Schema shared by validation prompts and Ollama format mode."""
    phase={"type":"object","properties":{"id":{"type":"string"},"goal":{"type":"string"},"status":{"type":"string","enum":["pending","pass","blocked","waiting"]},"done":{"type":"array","items":{"type":"string"},"minItems":1},"verify":{"type":"array","items":{"type":"string"},"minItems":1},"dependencies":{"type":"array","items":{"type":"string"}},"risks":{"type":"array","items":{"type":"string"},"minItems":1}},"required":["id","goal","status","done","verify","dependencies","risks"],"additionalProperties":False}
    task = {"type":"object", "properties":{"task_id":{"type":"string"},"phase_id":{"type":"string"},"goal":{"type":"string"},"domain":{"type":"string"},"depends_on":{"type":"array","items":{"type":"string"}},"required_context":{"type":"array","items":{"type":"string"}},"required_mcp":{"type":"array","items":{"type":"string"}},"change_scope":{"type":"array","items":{"type":"string"}},"verification":{"type":"array","items":{"type":"string"}},"done_condition":{"type":"array","items":{"type":"string"}},"retry_state":{"type":"object"}}, "required":["task_id","phase_id","goal","domain","depends_on","required_context","required_mcp","change_scope","verification","done_condition","retry_state"], "additionalProperties":False}
    return {"type":"object","properties":{"schema_version":{"type":"integer","const":1},"original_goal":{"type":"string"},"scope":{"type":"object","properties":{"allowed":{"type":"array","items":{"type":"string"}},"forbidden":{"type":"array","items":{"type":"string"}}},"required":["allowed","forbidden"],"additionalProperties":False},"assumptions":{"type":"array","items":{"type":"string"}},"phases":{"type":"array","items":phase,"minItems":1,"maxItems":8},"tasks":{"type":"array","items":task,"maxItems":15},"max_retries_per_phase":{"type":"integer","const":2},"requires_user_approval":{"type":"boolean","const":True}},"required":["schema_version","original_goal","scope","assumptions","phases","max_retries_per_phase","requires_user_approval"],"additionalProperties":False}


def coding_candidate(text: str) -> bool:
    return classify_coding_request(text) == "CODING"


def coding_action_intent(text: str) -> bool:
    """Detect a request to perform a coding/file change, independently of routing.

    Explanations and short examples are deliberately excluded.  This detector
    is a safety boundary for the normal Brain path, not a replacement for the
    Coding Task candidate detector.
    """
    facts = coding_classification_diagnostics(text)
    return bool(facts["mutation_intent"]) and facts["classification"] == "CODING"


def validate_plan(value: Any, goal: str) -> list[str]:
    if not isinstance(value, dict): return ["plan must be an object"]
    required={"schema_version","original_goal","scope","assumptions","phases","max_retries_per_phase","requires_user_approval"}
    missing=required-value.keys()
    if missing:return ["missing: "+", ".join(sorted(missing))]
    if value["schema_version"] != 1:return ["schema_version must be 1"]
    if not isinstance(value["phases"],list) or not 1 <= len(value["phases"]) <= 8:return ["phases must contain 1 to 8 entries"]
    for phase in value["phases"]:
        if not isinstance(phase,dict) or not {"id","goal","status","done","verify","dependencies","risks"} <= phase.keys():return ["invalid phase schema"]
    if value["max_retries_per_phase"] != MAX_RETRIES_PER_PHASE or value["requires_user_approval"] is not True:return ["invalid approval/retry policy"]
    if value["original_goal"] != goal:return ["original goal mismatch"]
    if "tasks" in value:
        graph_errors = validate_task_graph(value["tasks"])
        if graph_errors: return graph_errors
    ids=[]
    for phase in value["phases"]:
        phase_id=phase.get("id")
        if not isinstance(phase_id,str) or not phase_id.strip() or phase_id in ids:return ["phase ids must be unique"]
        ids.append(phase_id)
        if phase.get("status") not in {"pending","pass","blocked","waiting"}: return ["invalid phase status"]
        if not isinstance(phase.get("done"),list) or not phase["done"] or not isinstance(phase.get("verify"),list): return ["phase done must be non-empty and verify must be an array"]
        if not isinstance(phase["dependencies"],list) or any(not isinstance(dep,str) for dep in phase["dependencies"]): return ["invalid dependencies"]
    if any(dep not in ids for phase in value["phases"] for dep in phase["dependencies"]): return ["unknown phase dependency"]
    if "tasks" in value:
        task_phase_ids=[task.get("phase_id") for task in value["tasks"]]
        if any(not isinstance(phase_id,str) or phase_id not in ids for phase_id in task_phase_ids):
            return ["task phase_id must identify a plan phase"]
        if len(task_phase_ids) != len(set(task_phase_ids)) or set(task_phase_ids) != set(ids):
            return ["task graph must contain exactly one task for each plan phase"]
    return []


def extract_plan_json(raw: str) -> tuple[Any | None, str]:
    """Parse only an unambiguous single JSON object, allowing one markdown fence."""
    text=(raw or "").strip()
    if text.startswith("```"):
        lines=text.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            return None, "fenced response is incomplete"
        text="\n".join(lines[1:-1]).strip()
    if not text.startswith("{"):
        return None, "response must contain one top-level JSON object"
    try:
        value,end=json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
    if not isinstance(value,dict) or text[end:].strip():
        return None, "response must contain exactly one JSON object"
    return value, ""


def validate_phase_report(value: Any, phase_id: str, attempt: int) -> list[str]:
    required={"phase_id","attempt","status","implemented","changed_files","test_executed","test_pass","test_fail","build_executed","build_pass","errors","blockers","risks"}
    if not isinstance(value,dict): return ["report must be an object"]
    if required-value.keys(): return ["missing report fields"]
    if value["phase_id"] != phase_id or value["attempt"] != attempt:return ["phase identity mismatch"]
    if value["status"] not in {"PASS","FAIL","BLOCKED","NOT_RUN"}:return ["invalid report status"]
    if not all(isinstance(value[key],list) for key in ("implemented","changed_files","test_executed","test_pass","test_fail","errors","blockers","risks")):return ["report arrays required"]
    if not isinstance(value["build_executed"],str) or not isinstance(value["build_pass"],str):return ["build fields must be typed"]
    return []


def report_has_authoritative_failure(report: dict[str, Any], phase: dict[str, Any]) -> bool:
    """Return whether local typed evidence rules out a manager PASS decision."""
    if report.get("status") != "PASS" or report.get("errors") or report.get("blockers"):
        return True
    if report.get("test_fail"):
        return True
    required=" ".join([*phase.get("done", []), *phase.get("verify", [])]).lower()
    if any(term in required for term in ("typed filesystem", "read-back", "readback")):
        operations=(report.get("typed_execution_summary") or {}).get("operations") or []
        wrote=any(op.get("tool") == "workspace_write" and op.get("status") == "success" for op in operations if isinstance(op,dict))
        read=any(op.get("tool") == "workspace_read" and op.get("status") == "success" for op in operations if isinstance(op,dict))
        if not wrote or not read:
            return True
    if any(word in required for word in ("test", "pytest", "cargo check", "typecheck", "build")):
        if not report.get("test_executed") and report.get("build_executed") == "NOT_RUN":
            return True
        if report.get("build_pass") in {"FAIL", "NOT_RUN"} and any(word in required for word in ("build", "cargo check", "typecheck")):
            return True
    return False


def _successful_typed_operations(report: dict[str, Any]) -> list[dict[str, Any]]:
    typed = report.get("typed_execution_summary") if isinstance(report.get("typed_execution_summary"), dict) else {}
    return [item for item in (typed.get("operations") or [])
            if isinstance(item, dict) and str(item.get("status") or "").lower() in {"success", "completed", "ok"}]


def _criterion_paths(criterion: str, known_paths: set[str]) -> list[str]:
    """Find explicit file-like paths without treating arbitrary prose as evidence."""
    found = []
    for match in re.findall(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:html?|css|js|json|py|rs|tsx?|md|txt)", criterion):
        found.append(match)
    lower = criterion.lower()
    for path in known_paths:
        if Path(path).name.lower() in lower or path.lower() in lower:
            found.append(path)
    return list(dict.fromkeys(found))


def _criterion_satisfied(criterion: str, report: dict[str, Any], typed: dict[str, Any],
                         successful_ops: list[dict[str, Any]], file_state: dict[str, bool]) -> tuple[bool, bool]:
    """Return (satisfied, required_not_run) using only typed/current-state facts."""
    explicit = report.get("criteria_evidence")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if str(key).strip().lower() == criterion.strip().lower():
                return bool(value), False
    explicit = typed.get("criteria") if isinstance(typed, dict) else None
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if str(key).strip().lower() == criterion.strip().lower():
                return bool(value), False

    paths = _criterion_paths(criterion, set(file_state))
    if paths:
        known = set(file_state)
        paths = [path for path in paths if path in known or any(Path(path).name.lower() == Path(item).name.lower() for item in known)]
        if not paths:
            return False, False
        paths = list(dict.fromkeys(next((item for item in known if Path(item).name.lower() == Path(path).name.lower()), path) for path in paths))
        # A successful deletion/removal is authoritative invalidation.
        if any(not file_state.get(path, False) for path in paths):
            return False, False
        if any(file_state.get(path, False) for path in paths):
            return True, False

    lower = criterion.lower()
    if any(token in lower for token in ("test", "pytest", "cargo check", "typecheck", "build")):
        if report.get("test_fail") or report.get("build_pass") in {"FAIL", "NOT_RUN"}:
            return False, report.get("build_pass") == "NOT_RUN"
        if report.get("test_pass") or report.get("build_pass") == "PASS":
            return True, False
        if report.get("status") == "NOT_RUN" or (not report.get("test_executed") and report.get("build_executed") == "NOT_RUN"):
            return False, True

    # A phase report with no typed failure is the existing compatibility path
    # for non-filesystem, human-observable criteria (for example "manual check").
    # Explicit NOT_RUN/FAIL remains authoritative and cannot be promoted.
    if report.get("status") in {"FAIL", "NOT_RUN", "BLOCKED"}:
        return False, report.get("status") == "NOT_RUN"
    return True, False


def evaluate_phase(phase: dict[str, Any], report: dict[str, Any],
                   prior_reports: list[dict[str, Any]] | None = None,
                   workspace_root: str | None = None) -> dict[str, Any]:
    """Derive the allowed manager envelope from typed evidence and state.

    Model prose is deliberately absent from this function.  Prior reports are
    compatible only when they refer to the same phase and plan revision; typed
    successful operations are accumulated, while successful removals invalidate
    the corresponding current-state fact.
    """
    prior_reports = prior_reports or []
    revision = int(report.get("plan_revision") or 0)
    compatible = [item for item in prior_reports
                  if isinstance(item, dict) and item.get("phase_id") == phase.get("id")
                  and int(item.get("plan_revision") or 0) == revision]
    all_reports = [*compatible, report]
    file_state: dict[str, bool] = {}
    successful_ops: list[dict[str, Any]] = []
    authoritative_failure = False
    required_not_run = False
    external_blocker = False
    authorization_blocker = False
    for index, item in enumerate(all_reports):
        successful = _successful_typed_operations(item)
        successful_ops.extend(successful)
        # A prior attempt's failure is recoverable once the current attempt
        # supplies replacement evidence.  Only the current report can veto the
        # current transition; prior reports contribute successful facts.
        if index == len(all_reports) - 1 and (item.get("status") in {"FAIL", "BLOCKED"} or item.get("errors") or item.get("blockers") or item.get("test_fail")):
            authoritative_failure = True
        if index == len(all_reports) - 1 and item.get("status") == "NOT_RUN":
            required_not_run = True
        typed = item.get("typed_execution_summary") if isinstance(item.get("typed_execution_summary"), dict) else {}
        if str(typed.get("state") or "") in {"denied", "forbidden"}:
            external_blocker = True
        for op in successful:
            output = op.get("output") if isinstance(op.get("output"), dict) else {}
            path = str(output.get("path") or "")
            if not path:
                continue
            operation = str(output.get("operation") or op.get("operation") or op.get("op") or "").lower()
            if op.get("tool") in {"workspace_delete", "workspace_remove"} or operation in {"delete", "remove", "unlink"}:
                file_state[path] = False
            elif op.get("tool") in {"workspace_write", "workspace_write_normalized", "workspace_patch", "workspace_read", "workspace_read_normalized"}:
                file_state[path] = True
        for path in item.get("changed_files") or []:
            if isinstance(path, str) and path:
                file_state.setdefault(path, True)
    if workspace_root:
        root = Path(workspace_root)
        for path in list(file_state):
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root / candidate
            # Current workspace state wins over an earlier operation claim.
            file_state[path] = candidate.exists() and candidate.is_file()

    done = [str(item) for item in (phase.get("done") or [])]
    verify = [str(item) for item in (phase.get("verify") or [])]
    required_mcp = [str(item) for item in (phase.get("required_mcp") or [])]
    mcp_evidence = [item for source in all_reports for item in (source.get("mcp_evidence") or [])
                    if isinstance(item, dict)]
    # Initialize and tools/list establish availability only.  A required MCP
    # is "used" once an allowlisted task tool has a persisted PASS result.
    used_mcp = {str(item.get("mcp_name")) for item in mcp_evidence
                if item.get("status") == "PASS"
                and str(item.get("tool_name") or "") not in {"", "initialize", "tools/list"}}
    aggregate_typed = {"operations": successful_ops, "state": report.get("typed_execution_summary", {}).get("state") if isinstance(report.get("typed_execution_summary"), dict) else ""}
    done_flags = [_criterion_satisfied(item, report, aggregate_typed, successful_ops, file_state) for item in done]
    verify_flags = [_criterion_satisfied(item, report, aggregate_typed, successful_ops, file_state) for item in verify]
    done_satisfied = [item for item, (ok, _) in zip(done, done_flags) if ok]
    verify_satisfied = [item for item, (ok, _) in zip(verify, verify_flags) if ok]
    done_unmet = [item for item, (ok, _) in zip(done, done_flags) if not ok]
    verify_unmet = [item for item, (ok, _) in zip(verify, verify_flags) if not ok]
    missing_mcp = [name for name in required_mcp if name not in used_mcp]
    if missing_mcp:
        done_unmet.extend(f"required MCP used: {name}" for name in missing_mcp)
    acceptance = [str(item) for item in (phase.get("acceptance_contract") or [])]
    acceptance_results = {
        "FRONTEND_STACK": frontend_stack_acceptance(workspace_root),
        "MARKETING_STRUCTURE": marketing_design_structure_acceptance(workspace_root),
    }
    missing_acceptance = [name for name in acceptance if not acceptance_results.get(name, False)]
    if missing_acceptance:
        done_unmet.extend(f"acceptance contract: {name}" for name in missing_acceptance)
    required_not_run = required_not_run or any(flag[1] for flag in [*done_flags, *verify_flags])
    # Permission/safety denials are external blockers; ordinary internal
    # errors remain recoverable and are never promoted to BLOCKED here.
    authorization_blocker = any(any(term in str(value).lower() for term in ("authorization", "permission", "safety"))
                               for item in all_reports for value in (item.get("blockers") or item.get("errors") or []))
    phase_complete = bool(done) and bool(verify) and not done_unmet and not verify_unmet and not authoritative_failure and not required_not_run and not external_blocker and not authorization_blocker
    structural_replan_needed = bool((done_unmet or verify_unmet) and any(
        token in " ".join([*done_unmet, *verify_unmet]).lower()
        for token in ("unobservable", "browser behavior", "visual behavior", "cannot be observed", "unsatisfiable")))
    if phase_complete:
        allowed = ["PASS"]
    elif external_blocker or authorization_blocker:
        allowed = ["BLOCKED"]
    elif structural_replan_needed:
        allowed = ["RETRY", "REPLAN_REQUIRED"]
    elif done_unmet or verify_unmet or authoritative_failure or required_not_run:
        allowed = ["RETRY"]
    else:
        allowed = ["RETRY"]
    return {"done_total": len(done), "done_satisfied": len(done_satisfied), "done_unmet": done_unmet,
            "verify_total": len(verify), "verify_satisfied": len(verify_satisfied), "verify_unmet": verify_unmet,
            "authoritative_failure_present": authoritative_failure, "required_not_run_present": required_not_run,
            "external_blocker_present": external_blocker, "authorization_blocker_present": authorization_blocker,
            "phase_complete": phase_complete, "structural_replan_needed": structural_replan_needed,
            "allowed_decisions": allowed, "file_state": file_state,
            "successful_typed_operation_count": len(successful_ops), "required_mcp": required_mcp,
            "used_mcp": sorted(used_mcp), "missing_mcp": missing_mcp,
            "acceptance": acceptance_results, "missing_acceptance": missing_acceptance}


# Repository-compatible aliases for callers/tests that prefer noun or verb form.
phase_evaluation = evaluate_phase
evaluate_phase_evidence = evaluate_phase


def validate_manager_decision(value: Any) -> list[str]:
    if not isinstance(value,dict):
        return ["invalid manager decision"]
    decision=value.get("decision")
    if not isinstance(decision,str) or decision not in {"PASS","RETRY","REPLAN_REQUIRED","NEED_USER","BLOCKED"}:
        return ["invalid manager decision"]
    return []


_DIAGNOSIS_LIST_LIMIT = 8
_DIAGNOSIS_ITEM_LIMIT = 240
_DIAGNOSIS_INSTRUCTION_LIMIT = 500


def _bounded_diagnosis_items(value: Any) -> list[str]:
    """Keep model-supplied diagnosis bounded and deterministic."""
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:_DIAGNOSIS_ITEM_LIMIT] for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()][:_DIAGNOSIS_LIST_LIMIT]


def normalize_manager_decision(value: Any, phase: dict[str, Any], report: dict[str, Any], typed_summary: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a review response into a bounded diagnostic contract.

    Older/local models often return only ``decision`` and ``reason``.  Their
    response remains usable, while missing diagnosis fields are derived from
    the authoritative typed report rather than invented model claims.
    """
    if validate_manager_decision(value):
        return None
    if not isinstance(phase, dict) or not isinstance(report, dict):
        return None
    typed_summary = typed_summary if isinstance(typed_summary, dict) else {}
    decision = str(value["decision"])
    reason = str(value.get("reason") or "manager decision")[:_DIAGNOSIS_INSTRUCTION_LIMIT]
    supplied = value.get("diagnosis") if isinstance(value.get("diagnosis"), dict) else {}
    unmet_done = _bounded_diagnosis_items(supplied.get("unmet_done"))
    unmet_verify = _bounded_diagnosis_items(supplied.get("unmet_verify"))
    evidence = _bounded_diagnosis_items(supplied.get("evidence"))
    if decision != "PASS":
        if not unmet_done and (report.get("status") != "PASS" or report.get("errors") or report.get("blockers")):
            unmet_done = [str(item)[:_DIAGNOSIS_ITEM_LIMIT] for item in phase.get("done", [])[:_DIAGNOSIS_LIST_LIMIT]]
        if not unmet_verify:
            if report.get("test_fail"):
                unmet_verify = [str(item)[:_DIAGNOSIS_ITEM_LIMIT] for item in phase.get("verify", [])[:_DIAGNOSIS_LIST_LIMIT]]
            elif report.get("build_executed") == "NOT_RUN" and any("build" in str(item).lower() for item in phase.get("verify", [])):
                unmet_verify = [str(item)[:_DIAGNOSIS_ITEM_LIMIT] for item in phase.get("verify", [])[:_DIAGNOSIS_LIST_LIMIT]]
        if not evidence:
            evidence = [str(report.get("status") or "UNKNOWN")]
            evidence.extend(str(error)[:_DIAGNOSIS_ITEM_LIMIT] for error in (report.get("errors") or [])[:_DIAGNOSIS_LIST_LIMIT])
            evidence.extend(str(item.get("tool") or "") + ":" + str(item.get("status") or "")
                            for item in (typed_summary.get("operations") or [])[:_DIAGNOSIS_LIST_LIMIT]
                            if isinstance(item, dict))
            evidence = [item[:_DIAGNOSIS_ITEM_LIMIT] for item in evidence if item][: _DIAGNOSIS_LIST_LIMIT]
    retry_instruction = str(supplied.get("retry_instruction") or "")[:_DIAGNOSIS_INSTRUCTION_LIMIT]
    replan_instruction = str(supplied.get("replan_instruction") or "")[:_DIAGNOSIS_INSTRUCTION_LIMIT]
    verification_instruction = str(supplied.get("verification_instruction") or "")[:_DIAGNOSIS_INSTRUCTION_LIMIT]
    replan_reason = str(supplied.get("replan_reason") or "")[:_DIAGNOSIS_INSTRUCTION_LIMIT]
    failure_class = str(supplied.get("failure_class") or "RECOVERABLE_INTERNAL")[:80]
    retry_instruction_source = "model" if retry_instruction else "derived"
    replan_instruction_source = "model" if replan_instruction else "derived"
    if decision == "RETRY" and not retry_instruction:
        retry_instruction = "Address the unmet criteria using the typed evidence, then verify them again."
    if decision == "REPLAN_REQUIRED" and not replan_instruction:
        replan_instruction = "Redesign only the unfinished phase so its Done and Verify criteria are satisfiable."
    if decision != "PASS" and not verification_instruction:
        verification_instruction = "Re-run every listed Verify criterion with observable typed evidence."
    diagnosis = {
        "phase_id": str(phase.get("id") or "")[:_DIAGNOSIS_ITEM_LIMIT],
        "plan_revision": int(report.get("plan_revision") or 0),
        "attempt": int(report.get("attempt") or 0),
        "unmet_done": unmet_done,
        "unmet_verify": unmet_verify,
        "evidence": evidence,
        "evidence_used": evidence,
        "failure_class": failure_class,
        "retry_instruction": retry_instruction,
        "specific_fix": retry_instruction,
        "verification_instruction": verification_instruction,
        "replan_instruction": replan_instruction,
        "replan_reason": replan_reason or (replan_instruction if decision == "REPLAN_REQUIRED" else ""),
        "replan_reason_source": "model" if replan_reason else "derived",
        "retry_instruction_source": retry_instruction_source,
        "replan_instruction_source": replan_instruction_source,
    }
    return {"decision": decision, "reason": reason, "diagnosis": diagnosis}


def plan_prompt(goal: str, execution_mode: str = "NORMAL", mutation_mode: str = "IMPLEMENTATION") -> str:
    mode_instruction=("This is a FIX task: first obtain a concrete repo/runtime/test clue, then make the smallest patch using existing patterns and run focused verification. Use exactly one phase unless independently observable work requires 2-3; do not add inspection, planning, review, or final-report-only phases. Avoid refactors and new architecture. "
                      if mutation_mode == "FIX" else
                      "Use 3-5 coherent batches for this substantial task. "
                      if execution_mode == "HEAVY_BATCHED" else
                      "For a simple static site or small fix, use 1-3 coherent phases. Repo inspection is context, not a phase; combine scaffold, related pages, styling, accessibility and responsiveness into one implementation phase. Include at most one final verification phase. ")
    return ("Return JSON only for a read-only coding plan. Do not edit files. "
            "Output exactly one JSON object: no markdown fences, preamble, or trailing explanation. Canonical skeleton: "
            '{"schema_version":1,"original_goal":"<goal>","scope":{"allowed":["..."],"forbidden":["..."]},"assumptions":[],"phases":[{"id":"p1","goal":"...","status":"pending","done":["..."],"verify":["..."],"dependencies":[],"risks":[]}],"max_retries_per_phase":2,"requires_user_approval":true}. '
            "Schema: {schema_version:1,original_goal:string,scope:{allowed:[string],forbidden:[string]},assumptions:[string],"
            "phases:[{id:string,goal:string,status:'pending',done:[string],verify:[string],dependencies:[string],risks:[string]}],"
            "max_retries_per_phase:2,requires_user_approval:true}. " + mode_instruction + "Include a tasks array with exactly one task per phase. "
            "Every task must have a unique task_id and phase_id exactly matching its phase id; task dependencies must mirror the phase dependencies. Goal: "+goal)


def plan_repair_prompt(goal: str, invalid: str, errors: list[str] | None = None) -> str:
    return ("Return only corrected JSON for the existing read-only coding plan. Do not add work, edit files, "
            "or change the original goal. Correct schema and formatting only. JSON object only: no markdown, fence, "
            "preamble, or trailing explanation. Required schema is exactly: "
            '{"schema_version":1,"original_goal":"<same goal>","scope":{"allowed":["..."],"forbidden":["..."]},'
            '"assumptions":["..."],"phases":[{"id":"p1","goal":"...","status":"pending",'
            '"done":["..."],"verify":["..."],"dependencies":[],"risks":[]}],'
            '"max_retries_per_phase":2,"requires_user_approval":true}. If tasks are present, include exactly one task per phase and set each task.phase_id to its phase id. '
            "Keep 1-8 unique phases, preserve intended scope and phase meaning. Original goal: " + goal +
            "\nValidator errors: " + json.dumps(errors or [], ensure_ascii=False) +
            "\nInvalid draft: " + invalid[:12000])


def phase_report_prompt(phase: dict[str, Any], attempt: int, response: str, typed_summary: dict[str, Any]) -> str:
    return ("Return JSON only for a phase report. Do not execute tools or claim tests/build that did not run. "
            "Schema fields: phase_id,attempt,status(PASS|FAIL|BLOCKED|NOT_RUN),implemented,changed_files,"
            "test_executed,test_pass,test_fail,build_executed,build_pass,errors,blockers,risks. "
            "The typed execution facts are authoritative and must be preserved. "
            f"Phase={json.dumps(phase, ensure_ascii=False)} Attempt={attempt} Typed={json.dumps(typed_summary, ensure_ascii=False)} Response={response[:4000]}")


def phase_report_repair_prompt(phase_id: str, attempt: int, invalid: str, typed_summary: dict[str, Any]) -> str:
    return ("Return only a corrected JSON phase report for schema/format repair. Do not execute anything, add files, "
            "or change typed evidence. In particular do not turn FAIL or NOT_RUN into PASS. "
            f"phase_id={phase_id}; attempt={attempt}; typed={json.dumps(typed_summary, ensure_ascii=False)}; invalid={invalid[:12000]}")


def manager_review_prompt(goal: str, phase: dict[str, Any], report: dict[str, Any], typed_summary: dict[str, Any], completed: list[dict[str, Any]], retry_count: int, approved_scopes: list[Any], pending_authorization: Any) -> str:
    return ("Return JSON only: {\"decision\":\"PASS|RETRY|REPLAN_REQUIRED|NEED_USER|BLOCKED\",\"reason\":\"short\","
            "\"diagnosis\":{\"phase_id\":\"...\",\"plan_revision\":0,\"attempt\":0,"
            "\"unmet_done\":[],\"unmet_verify\":[],\"evidence_used\":[],"
            "\"retry_instruction\":\"...\",\"verification_instruction\":\"...\","
            "\"replan_reason\":\"...\"}}. "
            "PASS requires each phase Done and Verify supported by typed evidence. For non-PASS, identify every unmet "
            "Done/Verify criterion and give a concise fix and verification instruction. evidence_used must come only "
            "from the supplied typed execution summary and report; never provide chain-of-thought or unsupported facts. "
            "Do not override typed failures, missing required verification, or pending authorization. "
            + json.dumps({"original_goal":goal,"current_phase":phase,"current_brain_report":report,
                            "typed_execution_summary":typed_summary,"completed_phase_summaries":completed[-8:],
                            "retry_count":retry_count,"max_retries":MAX_RETRIES_PER_PHASE,
                            "approved_scopes":approved_scopes,"pending_user_authorization":pending_authorization}, ensure_ascii=False))


def completion_prompt(goal: str, plan: dict[str, Any], reports: list[dict[str, Any],], pending_authorization: Any,
                      current_plan_revision: int | None = None) -> str:
    return ("Return JSON only: {\"decision\":\"PASS|BLOCKED\",\"reason\":\"short\"}. PASS only if every required "
            "phase has typed verified PASS evidence and there is no pending authorization. "
            + json.dumps({"original_goal":goal,"plan":plan,"current_plan_revision":current_plan_revision,
                          "reports":reports[-16:],"pending_authorization":pending_authorization}, ensure_ascii=False))


def final_report_prompt(goal: str, plan: dict[str, Any], reports: list[dict[str, Any]]) -> str:
    return ("Produce a concise read-only final report. Do not execute tools, edit files, change dependencies, or publish. "
            "Use exactly these headings: Implemented, Changed files, Tests, Build, Not run, Risks, TODO. "
            + json.dumps({"original_goal":goal,"plan":plan,"reports":reports[-16:]}, ensure_ascii=False))


def new_id() -> str: return str(uuid.uuid4())


def model_slot(): return _SLOT
