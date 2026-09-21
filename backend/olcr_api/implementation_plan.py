"""Planning artifacts and safe preflight scaffolding for Coding Tasks.

The helpers in this module are deliberately filesystem boring: every manifest
entry is resolved under one canonical project root, every entry is checked
before the first write, and only tiny syntax-valid placeholders are created.
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Iterable

from .retrieval import PathGuard
from .coding_tasks import canonical_dependency_requirements, dependency_requirement_specs, dependency_requirement_names


_ACTIONS = {"create", "modify", "delete"}
_PATH_TOKEN = re.compile(r"(?<![\w.-])([\w./-]+\.(?:html?|css|scss|js|mjs|ts|tsx|jsx|py|json|vue|svelte))(?![\w.-])", re.I)

# Planner prose sometimes compresses an entire selected stack into one
# slash-separated value (for example ``Vite/React/TS/Tailwind/shadcn/Anime.js``).
# It is a requirement description, not a path.  Keep this classifier narrow:
# ordinary repository paths such as ``src/index.css`` and ``vite.config.ts``
# must remain filesystem-artifact candidates.
_SEMANTIC_STACK_PARTS = {
    "vite", "react", "ts", "typescript", "tailwind", "tailwindcss",
    "shadcn", "shadcnui", "anime", "animejs", "playwright",
}

_COMPONENT_SECTION_NAMES = ("Hero", "Philosophy", "HowItWorks", "API", "MCP", "CTA", "FinalCTA")


def _planned_component_paths(text: str) -> list[str]:
    """Derive only explicitly named component sections for manifest readiness.

    Exact-file manifests are the mutation authorization boundary.  When a
    substantial frontend phase names multiple conventional UI components but
    omits their paths, fail closed and require the Planner to enumerate them;
    this helper never grants a directory or workspace wildcard.
    """
    value = str(text or "")
    names = [name for name in _COMPONENT_SECTION_NAMES
             if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", value, re.I)]
    component_signal = bool(re.search(r"\bcomponents?\b|コンポーネント|componentized|component-based|\bsections?\b|セクション|homepage architecture", value, re.I))
    # A single semantic section name is not enough to authorize a file.  The
    # normalized architecture must establish a componentized/sectioned set;
    # otherwise only an explicit concrete path (for example
    # ``src/components/Hero.tsx``) is trusted.
    if len(names) < 2 or not component_signal:
        return []
    return [f"src/components/{name}.tsx" for name in dict.fromkeys(names)]


def _confirmed_vite_react_profile(requirements: dict[str, Any] | None = None) -> bool:
    requirements = requirements or {}
    required = {str(item).lower() for item in (canonical_stack_contract(requirements).get("required") or [])}
    return (str(requirements.get("task_profile") or "") == "FRONTEND_ONLY_MARKETING_SITE"
            or {"vite", "react"} <= required)


def _canonical_artifact_path(path: str, requirements: dict[str, Any] | None = None,
                             explicit_paths: set[str] | None = None) -> str:
    """Normalize only the confirmed Vite/React bootstrap alias."""
    normalized = Path(str(path).replace("\\", "/")).as_posix().lstrip("./")
    explicit_paths = explicit_paths or set()
    if (normalized == "App.tsx" and _confirmed_vite_react_profile(requirements)
            and "src/App.tsx" not in explicit_paths
            and not any(value.startswith("app/") for value in explicit_paths)):
        return "src/App.tsx"
    return normalized


def normalize_plan_artifact_paths(plan: dict[str, Any], requirements: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Canonicalize concrete manifest/task paths before coverage is evaluated."""
    if not isinstance(plan, dict) or not _confirmed_vite_react_profile(requirements):
        return plan, []
    entries = plan.get("file_manifest") if isinstance(plan.get("file_manifest"), list) else []
    explicit = {Path(str(item.get("path")).replace("\\", "/")).as_posix().lstrip("./")
                for item in entries if isinstance(item, dict) and isinstance(item.get("path"), str)}
    changes: list[dict[str, str]] = []
    normalized_entries: list[Any] = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            normalized_entries.append(item)
            continue
        path = _canonical_artifact_path(item["path"], requirements, explicit)
        if path != item["path"]:
            changes.append({"from": str(item["path"]), "to": path, "source": "VITE_REACT_CANONICAL_PROFILE"})
        normalized_entries.append({**item, "path": path})
    if not changes:
        return plan, []
    bound = {**plan, "file_manifest": normalized_entries}
    scope = dict(bound.get("scope") or {})
    for key in ("allowed", "forbidden"):
        values = scope.get(key)
        if isinstance(values, list):
            scope[key] = [_canonical_artifact_path(value, requirements, explicit) if isinstance(value, str) else value
                          for value in values]
    if scope:
        bound["scope"] = scope
    tasks = []
    for task in bound.get("tasks") or []:
        if not isinstance(task, dict):
            tasks.append(task)
            continue
        tasks.append({**task, "change_scope": [
            _canonical_artifact_path(value, requirements, explicit) if isinstance(value, str) else value
            for value in (task.get("change_scope") or [])
        ]})
    if "tasks" in bound:
        bound["tasks"] = tasks
    return bound, changes


def concrete_artifact_contract(plan: dict[str, Any], requirements: dict[str, Any] | None = None,
                               workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the one trusted concrete-artifact model used by coverage/repair."""
    requirements = requirements or {}
    workspace = workspace or {}
    entries = plan.get("file_manifest") if isinstance(plan, dict) else None
    entries = entries if isinstance(entries, list) else []
    explicit_paths = {Path(str(item.get("path")).replace("\\", "/")).as_posix().lstrip("./")
                      for item in entries if isinstance(item, dict) and isinstance(item.get("path"), str)}
    artifacts: dict[str, dict[str, Any]] = {}

    def add(path: str, provenance: str, intended_operation: str = "create") -> None:
        normalized = _canonical_artifact_path(path, requirements, explicit_paths)
        if not normalized or normalized in {".", ""} or Path(normalized).is_absolute() or ".." in Path(normalized).parts:
            return
        current = artifacts.setdefault(normalized, {"provenance": [], "intended_operation": intended_operation})
        if provenance not in current["provenance"]:
            current["provenance"].append(provenance)
        if current.get("intended_operation") != "modify" and intended_operation == "modify":
            current["intended_operation"] = intended_operation

    for item in entries:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower().replace("-", "_")
        action = str(item.get("action") or item.get("operation") or "").lower()
        if item.get("read_only") or role in {"read_only", "readonly", "evidence", "inspection", "verification"} or action in {"read", "inspect", "verify"}:
            continue
        path = _path_from_entry(item)
        if path:
            add(path, "PLANNER_EXPLICIT_ARTIFACT", action or "create")

    text_parts: list[str] = []
    phases = plan.get("phases") if isinstance(plan, dict) else []
    for phase in phases or []:
        if not isinstance(phase, dict) or phase.get("execution_mode") == "VERIFICATION_ONLY":
            continue
        text_parts.extend(str(phase.get(key) or "") for key in ("goal", "done", "verify"))
        for task in plan.get("tasks") or []:
            if isinstance(task, dict) and task.get("phase_id") == phase.get("id"):
                text_parts.extend(str(task.get(key) or "") for key in ("goal", "change_scope", "done_condition", "verification"))
    text = " ".join(text_parts)
    semantic_requirements: set[str] = set()
    for token in _PATH_TOKEN.findall(text):
        if _is_semantic_requirement_token(token):
            semantic_requirements.add(token)
            continue
        if not _derived_semantic_path(token, Path(workspace.get("root") or ".")):
            add(token, "NORMALIZED_PHASE_ARTIFACT")
    for path in _planned_component_paths(text):
        add(path, "NORMALIZED_PHASE_ARTIFACT")

    greenfield = bool(workspace.get("greenfield"))
    profile = str(requirements.get("task_profile") or "")
    stack_terms = {term for term in ("react", "vite", "tailwind", "anime", "shadcn", "typescript")
                   if re.search(rf"\b{re.escape(term)}(?:\.js)?\b", text, re.I)}
    frontend = bool(re.search(r"\b(?:react|vite|tailwind|anime(?:\.js)?|shadcn)\b|フロントエンド", text, re.I))
    implementation = bool([p for p in phases or [] if isinstance(p, dict) and p.get("execution_mode") != "VERIFICATION_ONLY"])
    broad_frontend_contract = frontend and (len(stack_terms) >= 2 or bool(re.search(r"\b(?:hero|philosophy|workflow|cta|footer|marketing|section|ページ|セクション)\b", text, re.I)))
    if greenfield and broad_frontend_contract and implementation:
        for path in ("index.html", "src/main.tsx", "src/App.tsx", "src/index.css"):
            add(path, "STACK_PROFILE_ARTIFACT")
    canonical_dependency_records = canonical_dependency_requirements(requirements)
    canonical_dependency_specs = dependency_requirement_specs(canonical_dependency_records)
    dependency_installation_requested = any(
        isinstance(phase, dict) and phase.get("requires_dependency_installation") is True
        for phase in (phases or [])) or bool(re.search(
        r"(?:install|add|configure|initialize|setup|set\s*up).{0,80}(?:dependenc|package|react|tailwind|anime|shadcn)|"
        r"(?:依存(?:関係|パッケージ)?|パッケージ).{0,40}(?:インストール|追加|導入|設定)", text, re.I))
    # Older callers that do not persist normalized requirements retain a
    # coverage-only compatibility path.  Managed execution reads the
    # structured records above and therefore never authorizes a package from
    # arbitrary phase prose.
    dependency_map = {"react": "react", "vite": "vite", "typescript": "typescript",
                      "anime": "animejs", "tailwind": "tailwindcss"}
    dependency_requirements = canonical_dependency_specs or sorted({dependency_map[item] for item in stack_terms if item in dependency_map})
    package_manager_artifact_required = bool(
        dependency_installation_requested and dependency_requirements and implementation and
        (greenfield or profile == "FRONTEND_ONLY_MARKETING_SITE"))
    if package_manager_artifact_required:
        add("package.json", "DETERMINISTIC_PROJECT_ARTIFACT")
    return {"artifacts": artifacts, "semantic_requirements": sorted(semantic_requirements),
            "stack_terms": sorted(stack_terms), "dependency_requirements": dependency_requirements,
            "dependency_requirement_records": canonical_dependency_records,
            "configuration_requirements": ["vite.config.ts"] if "vite" in stack_terms else [],
            "dependency_installation_requested": dependency_installation_requested,
            "package_manager_artifact_required": package_manager_artifact_required}


def _semantic_stack_part(value: str) -> str:
    part = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if part in {"animejs", "anime"}:
        return "animejs"
    if part in {"shadcnui", "shadcn"}:
        return "shadcn"
    if part in {"typescript", "ts"}:
        return "typescript"
    if part in {"tailwindcss", "tailwind"}:
        return "tailwind"
    return part


def _is_semantic_requirement_token(value: str) -> bool:
    """Return true only for a known technology/dependency composite.

    This is intentionally not a generic "looks like prose" heuristic.  A
    token is semantic only when it contains multiple slash-separated known
    stack identifiers (or is a single known identifier with a JS extension),
    leaving concrete paths available for coverage validation.
    """
    raw = str(value or "").strip()
    parts = [part for part in raw.split("/") if part]
    if not parts:
        return False
    canonical = []
    for part in parts:
        # ``Anime.js`` is a technology identifier while ``src/index.css`` is
        # a concrete path.  Remove only the extensions used by known stack
        # names before comparing.
        stem = re.sub(r"\.(?:js|mjs|ts|tsx|jsx)$", "", part, flags=re.I)
        canonical.append(_semantic_stack_part(stem))
    known = [item in _SEMANTIC_STACK_PARTS or item in {"animejs", "shadcn", "typescript", "tailwind"}
             for item in canonical]
    if len(parts) > 1:
        return all(known)
    return bool(known and known[0] and re.search(r"\.(?:js|mjs|ts|tsx|jsx)$", parts[0], re.I))


class PlanPathValidationError(ValueError):
    """Fail-closed plan validation error with safe repair diagnostics."""

    def __init__(self, message: str, diagnostics: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


class PlanManifestCoverageError(ValueError):
    """The mutable manifest cannot cover the implementation contract."""

    def __init__(self, message: str, missing: list[str], diagnostics: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.missing = missing
        self.diagnostics = diagnostics or []


class PlanStackConformanceError(ValueError):
    """The plan contradicts the persisted canonical application stack."""

    def __init__(self, message: str, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics


def canonical_stack_contract(requirements: dict[str, Any] | None = None) -> dict[str, list[str]]:
    requirements = requirements or {}
    required = list(requirements.get("required_stack") or [])
    forbidden = list(requirements.get("forbidden_stack") or [])
    if not required and requirements.get("task_profile") == "FRONTEND_ONLY_MARKETING_SITE":
        required = ["Vite", "React", "TypeScript", "Tailwind", "shadcn", "Anime.js v4"]
    if not forbidden and required:
        forbidden = ["Next.js", "GSAP", "Framer Motion", "Three.js"]
    return {"required": required, "forbidden": forbidden}


def stack_conformance(plan: dict[str, Any], requirements: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check material framework contradictions using typed plan evidence.

    A directory named ``app`` is harmless by itself.  A Next-specific
    dependency/configuration or an app-router pair without the required Vite
    bootstrap is material evidence of a hybrid/contradictory plan.
    """
    contract = canonical_stack_contract(requirements)
    required = contract["required"]
    forbidden = contract["forbidden"]
    if not required:
        return {"valid": True, "expected_stack": [], "forbidden_stack": [],
                "observed_stack_signals": [], "stack_conflicts": []}
    entries = [item for item in (plan.get("file_manifest") or []) if isinstance(item, dict)]
    paths = {Path(_path_from_entry(item)).as_posix() for item in entries if _path_from_entry(item)}
    evidence_parts: list[str] = []
    evidence_parts.extend(str(item.get("path") or "") for item in entries)
    # A planner can carry dependency/configuration details alongside a
    # package/config manifest entry.  Include that structured evidence so a
    # Next dependency cannot evade the stack gate simply because the path is
    # the generic ``package.json``.
    evidence_parts.extend(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in entries)
    for phase in plan.get("phases") or []:
        if isinstance(phase, dict):
            evidence_parts.extend(str(phase.get(key) or "") for key in ("goal", "done", "verify"))
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            evidence_parts.extend(str(task.get(key) or "") for key in ("goal", "change_scope", "verification", "done_condition"))
    evidence_parts.extend(str(item) for item in ((plan.get("scope") or {}).get("allowed") or []))
    text = " ".join(evidence_parts)
    lower = text.lower()
    observed: list[str] = []
    conflicts: list[str] = []
    forbidden_matches: list[tuple[str, str]] = [
        ("Next.js", r"\bnext\.js\b|next/config|next\.config\.|[\"']next[\"']\s*:"),
        ("GSAP", r"\bgsap\b"),
        ("Framer Motion", r"\bframer[\s-]+motion\b"),
        ("Three.js", r"\bthree\.js\b|[\"']three[\"']\s*:"),
    ]
    for technology, pattern in forbidden_matches:
        if technology in forbidden and re.search(pattern, lower):
            signal = re.sub(r"[^A-Z0-9]+", "_", technology.upper()).strip("_") + "_SIGNAL"
            observed.append(signal)
            conflicts.append(
                "forbidden framework Next.js appears in plan evidence"
                if technology == "Next.js" else
                f"forbidden technology {technology} appears in plan evidence")
    if any(path.startswith("next.config.") for path in paths):
        observed.append("NEXT_CONFIG_PATH")
        if "forbidden framework Next.js appears in plan evidence" not in conflicts:
            conflicts.append("Next.js configuration path is present")
    has_vite_bootstrap = bool({"index.html", "src/main.tsx", "src/App.tsx", "src/index.css"} <= paths)
    has_app_router_pair = {"app/page.tsx", "app/layout.tsx"} <= paths
    if has_app_router_pair:
        observed.append("APP_ROUTER_LAYOUT_PAIR")
        if not has_vite_bootstrap:
            conflicts.append("app-router layout lacks the required Vite bootstrap")
    if any(path.startswith("app/") for path in paths):
        observed.append("APP_DIRECTORY")
    if any(path.startswith("src/") for path in paths):
        observed.append("VITE_SOURCE_DIRECTORY")
    if any(path == "vite.config.ts" or path.startswith("vite.config.") for path in paths):
        observed.append("VITE_CONFIG")
    return {"valid": not conflicts, "expected_stack": required,
            "forbidden_stack": forbidden, "observed_stack_signals": sorted(set(observed)),
            "stack_conflicts": conflicts}


def stack_conformance_message(diagnostics: dict[str, Any]) -> str:
    return ("Plannerの技術スタックと正規のVite/React構成が一致しないため、安全のため実装を開始していません。"
            "計画の再生成が必要です。")


def deterministic_manifest_completion_diagnostics(plan: dict[str, Any], coverage: dict[str, Any],
                                                  target_root: str | Path,
                                                  workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explain every deterministic-completion eligibility decision."""
    workspace = workspace or {}
    missing = sorted(set(coverage.get("missing_paths") or []))
    diagnostics = {"evaluated": True, "eligible": False, "skip_reason": "NONE",
                   "added_paths": [], "missing_paths": missing,
                   "candidate_paths": missing, "candidate_diagnostics": []}
    if not missing:
        diagnostics["skip_reason"] = "NO_MISSING_ARTIFACTS"
        return diagnostics
    if not workspace.get("new_project_intent"):
        diagnostics["skip_reason"] = "NEW_PROJECT_INTENT_FALSE"
        return diagnostics
    if workspace.get("workspace_state") not in {None, "EMPTY_GREENFIELD", "PARTIALLY_INITIALIZED"}:
        diagnostics["skip_reason"] = "ESTABLISHED_WORKSPACE"
        return diagnostics
    host_authorized = workspace.get("host_workspace_authorized")
    if host_authorized is None:
        # Backward-compatible direct helper callers may only provide the
        # trusted mutation contract.  Production preflight always supplies the
        # explicit host flag and canonical root.
        host_authorized = bool(workspace.get("authorized_mutation") or workspace.get("allowed_mutation"))
    expected = set((coverage.get("requirement_types") or {}).get("FILESYSTEM_ARTIFACT") or [])
    if not set(missing) <= expected:
        diagnostics["skip_reason"] = "ARTIFACT_NOT_DERIVED_FROM_CANONICAL_CONTRACT"
        return diagnostics
    # Eligibility means the workspace and canonical artifact contract allow a
    # deterministic completion attempt.  Individual candidates can still be
    # rejected below; retaining eligibility makes an eligible/zero-additions
    # result explainable instead of silently collapsing to ``[]``.
    diagnostics["eligible"] = True
    root = Path(target_root).expanduser().resolve()
    try:
        guard = PathGuard([str(root)])
    except Exception:
        diagnostics["skip_reason"] = "PATHGUARD_ROOT_UNAVAILABLE"
        return diagnostics
    entries = plan.get("file_manifest") if isinstance(plan, dict) else []
    existing: dict[str, dict[str, Any]] = {}
    for item in entries if isinstance(entries, list) else []:
        if isinstance(item, dict):
            path = _path_from_entry(item)
            if path:
                existing[Path(path).as_posix()] = item
    provenance = (coverage.get("requirement_types") or {}).get("ARTIFACT_PROVENANCE") or {}
    current_task_provenance = workspace.get("current_task_provenance") or {}
    authorized_mutation = workspace.get("authorized_mutation") or workspace.get("allowed_mutation")
    if isinstance(authorized_mutation, str):
        authorized_mutation = [authorized_mutation]
    allowed = [str(item).replace("\\", "/").lstrip("./") for item in (authorized_mutation or []) if str(item).strip()]
    all_candidates_safe = True
    for path in missing:
        candidate_info = {"path": path,
                          "requirement_provenance": list(provenance.get(path) or []),
                          "workspace_exists": False,
                          "intended_operation": "create",
                          "authorization_result": "PENDING",
                          "rejection_reason": "NONE"}
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            candidate_info.update({"authorization_result": "REJECTED", "rejection_reason": "PATH_NOT_REPOSITORY_RELATIVE"})
            diagnostics["candidate_diagnostics"].append(candidate_info)
            all_candidates_safe = False
            continue
        if path in existing:
            candidate_info.update({"authorization_result": "REJECTED", "rejection_reason": "EXPLICIT_CONFLICTING_OPERATION"})
            diagnostics["candidate_diagnostics"].append(candidate_info)
            all_candidates_safe = False
            continue
        try:
            target = guard.resolve(str(root / candidate))
        except (OSError, ValueError, PermissionError):
            candidate_info.update({"authorization_result": "REJECTED", "rejection_reason": "PATH_OUTSIDE_CANONICAL_ROOT"})
            diagnostics["candidate_diagnostics"].append(candidate_info)
            all_candidates_safe = False
            continue
        exists = target.exists()
        candidate_info["workspace_exists"] = exists
        if allowed and not any(path == item.rstrip("/") or path.startswith(item.rstrip("/") + "/") for item in allowed):
            candidate_info.update({"authorization_result": "REJECTED", "rejection_reason": "ARTIFACT_OUTSIDE_AUTHORIZED_MUTATION_SCOPE"})
            diagnostics["candidate_diagnostics"].append(candidate_info)
            all_candidates_safe = False
            continue
        if exists:
            provenance_kind = str(current_task_provenance.get(path) or "")
            if provenance_kind not in {"CREATED_BY_CURRENT_TASK_SCAFFOLD", "CREATED_BY_CURRENT_TASK_IMPLEMENTATION"}:
                candidate_info.update({"intended_operation": "modify", "authorization_result": "REJECTED",
                                       "rejection_reason": "ESTABLISHED_ARTIFACT_WITHOUT_ACTIVE_TASK_PROVENANCE"})
                diagnostics["candidate_diagnostics"].append(candidate_info)
                all_candidates_safe = False
                continue
            candidate_info.update({"intended_operation": "modify", "authorization_result": "AUTHORIZED"})
        elif not (workspace.get("host_workspace_authorized") or allowed):
            candidate_info.update({"authorization_result": "REJECTED", "rejection_reason": "HOST_WORKSPACE_NOT_AUTHORIZED"})
            diagnostics["candidate_diagnostics"].append(candidate_info)
            all_candidates_safe = False
            continue
        else:
            candidate_info["authorization_result"] = "AUTHORIZED"
        diagnostics["candidate_diagnostics"].append(candidate_info)
    if not all_candidates_safe:
        diagnostics["skip_reason"] = next((item["rejection_reason"] for item in diagnostics["candidate_diagnostics"]
                                            if item.get("authorization_result") == "REJECTED"), "CANDIDATE_REJECTED")
        return diagnostics
    return diagnostics


def manifest_repair_progress(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Classify a repaired plan by coverage and meaningful manifest change."""
    before_missing = sorted(set(before.get("missing_paths") or []))
    after_missing = sorted(set(after.get("missing_paths") or []))
    before_manifest = sorted(set(before.get("manifest_paths") or []))
    after_manifest = sorted(set(after.get("manifest_paths") or []))
    before_set, after_set = set(before_missing), set(after_missing)
    manifest_changed = before_manifest != after_manifest
    missing_reduced = after_set < before_set or len(after_set) < len(before_set)
    if after.get("valid"):
        classification = "REPAIR_VALID"
    elif missing_reduced:
        classification = "REPAIR_IMPROVED"
    elif after_set > before_set:
        classification = "REPAIR_REGRESSED"
    elif after_set == before_set:
        # Adding an unrelated or semantic manifest entry is not progress when
        # the concrete coverage defect remains unchanged.
        classification = "REPAIR_NO_CHANGE"
    else:
        classification = "REPAIR_REGRESSED"
    return {"classification": classification,
            "missing_artifacts_before": before_missing,
            "missing_artifacts_after": after_missing,
            "manifest_paths_before": before_manifest,
            "manifest_paths_after": after_manifest,
            "manifest_changed": manifest_changed,
            "missing_reduced": missing_reduced}


def deterministic_manifest_completion(plan: dict[str, Any], coverage: dict[str, Any],
                                      target_root: str | Path,
                                      workspace: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    """Complete only already-authorized, concrete greenfield artifacts.

    The workspace contract is deliberately required.  Without explicit
    authorized mutation paths this helper declines, leaving the bounded
    Planner repair path responsible for ambiguous manifests.
    """
    workspace = workspace or {}
    decision = deterministic_manifest_completion_diagnostics(plan, coverage, target_root, workspace)
    if not decision["eligible"]:
        return None, []
    if any(item.get("authorization_result") != "AUTHORIZED"
           for item in (decision.get("candidate_diagnostics") or [])):
        return None, []
    expected = set((coverage.get("requirement_types") or {}).get("FILESYSTEM_ARTIFACT") or [])
    missing = sorted(set(coverage.get("missing_paths") or []))
    if not missing or not set(missing) <= expected:
        return None, []
    root = Path(target_root).expanduser().resolve()
    guard = PathGuard([str(root)])
    raw = plan.get("file_manifest") if isinstance(plan, dict) else None
    entries = raw if isinstance(raw, list) else []
    existing_operations: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = _path_from_entry(item)
        if path:
            existing_operations[Path(path).as_posix()] = item

    additions: list[str] = []
    for path in missing:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None, []
        existing = existing_operations.get(candidate.as_posix())
        if existing is not None:
            # A read-only/evidence entry is an explicit conflict, not an
            # invitation to silently widen its mutation role.
            return None, []
        try:
            target = guard.resolve(str(root / candidate))
        except (OSError, ValueError, PermissionError):
            return None, []
        if target.exists():
            provenance = (workspace.get("current_task_provenance") or {}).get(path)
            if provenance not in {"CREATED_BY_CURRENT_TASK_SCAFFOLD", "CREATED_BY_CURRENT_TASK_IMPLEMENTATION"}:
                return None, []
        additions.append(candidate.as_posix())
    if not additions:
        return None, []
    owner_phase = next((str(phase.get("id")) for phase in (plan.get("phases") or [])
                        if isinstance(phase, dict) and phase.get("execution_mode") != "VERIFICATION_ONLY"), "")
    manifest = list(entries)
    manifest.extend({"path": path, "action": "create", "role": "implementation",
                     "required": True, "owner_phase": owner_phase} for path in additions)
    return {**plan, "file_manifest": manifest}, additions


def path_validation_code(reason: str | None) -> str:
    """Map a path preflight reason to the stable control-plane taxonomy."""
    value = str(reason or "").lower()
    if "modify target does not exist" in value:
        return "MISSING_MODIFY_TARGET"
    if any(term in value for term in ("outside allowed roots", "outside authorized", "outside project root", "outside canonical")):
        return "OUTSIDE_AUTHORIZED_ROOT"
    if any(term in value for term in ("read-only", "read only", "readonly", "verification target")):
        return "READ_ONLY_SCOPE_VIOLATION"
    return "INVALID_PATH"


def path_validation_message(reason_or_code: str | None, *, replan: bool = False) -> str:
    """Translate a typed path failure into a user-facing recovery message."""
    code = str(reason_or_code or "")
    if code not in {"MISSING_MODIFY_TARGET", "OUTSIDE_AUTHORIZED_ROOT",
                    "INVALID_PATH", "READ_ONLY_SCOPE_VIOLATION"}:
        code = path_validation_code(code)
    if code == "MISSING_MODIFY_TARGET":
        return ("計画が存在しないファイルを変更対象として指定したため、安全のため実装を開始しませんでした。"
                + ("再計画します。" if replan else "再計画が必要です。"))
    if code == "OUTSIDE_AUTHORIZED_ROOT":
        return "計画にプロジェクト外のファイルが含まれるため、安全のため実装を開始しませんでした。"
    if code == "READ_ONLY_SCOPE_VIOLATION":
        return "読み取り専用の証拠を変更対象にできないため、安全のため実装を開始しませんでした。"
    return "実装計画のファイルパスを検証できないため、安全のため実装を開始していません。"


def manifest_coverage_message(*, replan: bool = True) -> str:
    """Translate an incomplete artifact contract without implying PathGuard ran."""
    return ("実装計画に必要な成果物が不足しているため、安全のため実装を開始していません。"
            + ("計画を再生成します。" if replan else "計画の再生成が必要です。"))


def manifest_repair_no_progress_message() -> str:
    """Explain a bounded repair that reproduced the same coverage defect."""
    return ("実装計画の成果物不足を自動修復できなかったため、安全のため実装を開始せず、"
            "タスクを一時停止しました。元の計画と不足成果物を確認してから再開してください。")


def workspace_state(target_root: str | Path) -> dict[str, Any]:
    """Return a bounded, deterministic existence snapshot for Planner context.

    Internal metadata and dependency/build caches do not make a workspace an
    established source tree.  The greenfield flag is true only when no
    meaningful user file or directory exists, so a single missing target can
    never justify MODIFY -> CREATE on its own.
    """
    root = Path(target_root).expanduser().resolve()
    ignored = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".benchmark_metadata.json"}
    entries: list[str] = []
    files: list[str] = []
    try:
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root).as_posix()
            if any(part in ignored or part.startswith(".") for part in item.relative_to(root).parts):
                continue
            entries.append(relative + ("/" if item.is_dir() else ""))
            if item.is_file():
                files.append(relative)
    except OSError:
        entries = []
        files = []
    source_files = [path for path in files if Path(path).suffix.lower() in {
        ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".vue", ".svelte", ".html", ".css", ".scss"
    }]
    project_state = ("EMPTY_GREENFIELD" if not entries else
                     "ESTABLISHED" if source_files else "PARTIALLY_INITIALIZED")
    return {
        "source": "filesystem_snapshot",
        "root": str(root),
        "greenfield": not entries,
        "project_state": project_state,
        "file_count": len(files),
        "entries": entries[:200],
        "files": files[:200],
    }


def _safe_stub(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".tsx", ".jsx"}:
        return "export default function PlannedComponent() { return null; }\n"
    if suffix in {".ts", ".js", ".mjs", ".py"}:
        return "export {};\n" if suffix != ".py" else "\"\"\"Planned implementation stub.\"\"\"\n"
    if suffix in {".html", ".htm"}:
        return "<!doctype html>\n<html><body></body></html>\n"
    if suffix in {".json"}:
        return "{}\n"
    if suffix in {".css", ".scss"}:
        return "/* Planned implementation stub. */\n"
    if suffix in {".vue", ".svelte"}:
        return "<script>\n</script>\n"
    return "Planned implementation stub.\n"


def _path_from_entry(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        value = entry.get("path")
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def _raw_manifest(plan: dict[str, Any]) -> tuple[list[Any], str]:
    explicit = plan.get("file_manifest")
    if isinstance(explicit, list):
        return explicit, "PLANNER"
    # Compatibility fallback for older planners.  It remains machine-readable
    # and is intentionally empty when the old plan did not name concrete files.
    source: list[str] = []
    for value in ((plan.get("scope") or {}).get("allowed") or []):
        if isinstance(value, str):
            source.extend(token for token in _PATH_TOKEN.findall(value)
                          if not _is_semantic_requirement_token(token))
    for phase in plan.get("phases") or []:
        for key in ("goal", "done", "verify"):
            values = phase.get(key) if isinstance(phase, dict) else None
            values = values if isinstance(values, list) else [values]
            for value in values:
                if isinstance(value, str):
                    source.extend(token for token in _PATH_TOKEN.findall(value)
                                  if not _is_semantic_requirement_token(token))
    seen: set[str] = set()
    return [{"path": value, "action": "modify"} for value in source if not (value in seen or seen.add(value))], "DERIVED"


def manifest_coverage(plan: dict[str, Any], requirements: dict[str, Any] | None = None,
                      workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check that planned implementation phases have writable artifacts.

    The expected set is derived from the selected stack and the phase/task
    contract.  Verification and read-only evidence never contributes to the
    mutable set.  This catches a deceptively valid two-file manifest before
    scaffolding can create an incomplete greenfield project.
    """
    requirements = requirements or {}
    workspace = workspace or {}
    raw = plan.get("file_manifest") if isinstance(plan, dict) else None
    # Legacy plans have no explicit manifest.  Their concrete path references
    # are still coverage candidates, while semantic stack tokens have already
    # been removed by _raw_manifest.
    coverage_raw = raw if isinstance(raw, list) else _raw_manifest(plan)[0]
    mutable: set[str] = set()
    if isinstance(coverage_raw, list):
        for item in coverage_raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower().replace("-", "_")
            action = str(item.get("action") or item.get("operation") or "").lower()
            if item.get("read_only") or role in {"read_only", "readonly", "evidence", "inspection", "verification"} or action in {"read", "inspect", "verify"}:
                continue
            path = _path_from_entry(item)
            if path:
                mutable.add(_canonical_artifact_path(path, requirements))
    phases = plan.get("phases") if isinstance(plan, dict) else []
    artifact_model = concrete_artifact_contract(plan, requirements, workspace)
    artifacts = artifact_model["artifacts"]
    expected = set(artifacts)
    semantic_requirements = set(artifact_model["semantic_requirements"])
    stack_terms = set(artifact_model["stack_terms"])
    dependency_requirements = list(artifact_model["dependency_requirements"])
    dependency_requirement_records = list(artifact_model.get("dependency_requirement_records") or [])
    configuration_requirements = list(artifact_model["configuration_requirements"])
    package_manager_artifact_required = bool(artifact_model["package_manager_artifact_required"])
    missing = sorted(path for path in expected if path not in mutable)
    technology_requirements = sorted(stack_terms)
    reference_requirements = sorted({str(item) for item in (requirements.get("required_mcps") or requirements.get("required_mcp") or requirements.get("selected_mcps") or []) if item})
    dependency_validation: dict[str, Any] = {"status": "NOT_ASSERTED", "declared": [], "missing": dependency_requirements}
    configuration_validation: dict[str, Any] = {"status": "NOT_ASSERTED", "present": [], "missing": configuration_requirements}
    root_value = workspace.get("root")
    if root_value:
        package_path = Path(root_value) / "package.json"
        if package_path.is_file():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                sections = [package.get(section) for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")]
                declared = {str(name).lower() for section in sections if isinstance(section, dict) for name in section}
                declaration_names = dependency_requirement_names(dependency_requirement_records or dependency_requirements)
                missing_dependencies = [name for name in declaration_names if name.lower() not in declared]
                dependency_validation = {"status": "SATISFIED" if not missing_dependencies else "MISSING_DECLARATION",
                                         "declared": [name for name in declaration_names if name.lower() in declared],
                                         "missing": missing_dependencies}
            except (OSError, ValueError):
                dependency_validation = {"status": "UNREADABLE", "declared": [], "missing": dependency_requirements}
        elif "package.json" in mutable:
            dependency_validation = {"status": "PENDING_DECLARATION", "declared": [], "missing": dependency_requirements}
        present_configuration = [path for path in configuration_requirements if (Path(root_value) / path).is_file()]
        configuration_validation = {"status": ("NOT_APPLICABLE" if not configuration_requirements else
                                                "SATISFIED" if len(present_configuration) == len(configuration_requirements) else "MISSING_CONFIGURATION"),
                                    "present": present_configuration,
                                    "missing": [path for path in configuration_requirements if path not in present_configuration]}
    requirement_types = {
        "FILESYSTEM_ARTIFACT": sorted(expected),
        "DEPENDENCY_REQUIREMENT": dependency_requirements,
        "DEPENDENCY_REQUIREMENT_PROVENANCE": dependency_requirement_records,
        "TECHNOLOGY_REQUIREMENT": technology_requirements + sorted(semantic_requirements),
        "CONFIGURATION_REQUIREMENT": configuration_requirements,
        "REFERENCE_OR_MCP_REQUIREMENT": reference_requirements,
        "UNKNOWN_REQUIREMENT": [],
        "ARTIFACT_PROVENANCE": {path: list(value.get("provenance") or []) for path, value in sorted(artifacts.items())},
    }
    phase_artifacts = {path for path, value in artifacts.items()
                       if "NORMALIZED_PHASE_ARTIFACT" in (value.get("provenance") or [])}
    return {"valid": not missing, "expected_paths": sorted(expected), "manifest_paths": sorted(mutable),
            "missing_paths": missing, "definition": "implementation-phase concrete file references plus detected-stack entry/style artifacts",
            "phase_mutation_scope_complete": not bool(missing),
            "missing_implementation_paths": sorted(set(missing) & phase_artifacts),
            "requirement_types": requirement_types, "semantic_requirements": sorted(semantic_requirements),
            "artifact_contract": artifacts,
            "dependency_requirement_records": dependency_requirement_records,
            "dependency_validation": dependency_validation, "configuration_validation": configuration_validation,
            "dependency_installation_required": package_manager_artifact_required,
            "dependency_artifacts": ["package.json"] if package_manager_artifact_required else [],
            "dependency_execution_contract": ({"operation": "PACKAGE_INSTALL",
                                                "generated_artifacts": ["package.json", "lockfile"],
                                                "package_json_edit_is_not_installation": True}
                                               if package_manager_artifact_required else None),
            "validation_sequence": "plan_validation -> manifest_coverage_validation -> path_validation -> scaffold -> implementation"}


def _derived_semantic_path(path_value: str, root: Path) -> bool:
    """Reject prose technology identifiers from the legacy derived manifest.

    Older planners did not provide ``file_manifest`` and the compatibility
    extractor had to look for filename-shaped tokens in prose.  A bare,
    missing, capitalized JavaScript token such as ``Anime.js`` is much more
    likely to be a technology/proper noun than a project target.  This filter
    applies only to that lossy fallback.  Explicit manifests remain fully
    validated below, and an existing file always wins over the heuristic.
    """
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute() or len(candidate.parts) != 1:
        return False
    try:
        if (root / candidate).resolve().exists():
            return False
    except OSError:
        return False
    if candidate.suffix.lower() not in {".js", ".mjs", ".ts", ".tsx", ".jsx"}:
        return False
    stem = candidate.stem
    # Conventional project paths with directories are retained above.  For a
    # missing bare token, a leading capital is the generic proper-noun signal;
    # no technology-specific allow/deny list is needed.
    return bool(re.search(r"[A-Z]", stem))


def prepare_implementation_plan(task_id: str, plan: dict[str, Any], target_root: str | Path,
                               requirements: dict[str, Any] | None = None,
                               workspace: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a plan manifest, scaffold CREATE entries, and return artifact.

    Validation is complete before any file is written.  Existing CREATE files
    are never overwritten; empty files are counted as skipped and non-empty
    conflicts fail closed so the planner can revise its manifest.
    """
    root = Path(target_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("canonical target root must be an existing directory")
    plan, canonicalization_changes = normalize_plan_artifact_paths(plan, requirements)
    conformance = stack_conformance(plan, requirements)
    if not conformance["valid"]:
        raise PlanStackConformanceError(
            "plan conflicts with canonical application stack: " + "; ".join(conformance["stack_conflicts"]),
            conformance)
    coverage = manifest_coverage(plan, requirements, workspace)
    if not coverage["valid"]:
        diagnostics = [{"offending_plan_path": path, "offending_plan_field": "file_manifest",
                        "offending_phase": "IMPLEMENTATION",
                        "manifest_coverage_reason": "manifest does not cover implementation contract",
                        "manifest_coverage_code": "INCOMPLETE_MANIFEST_COVERAGE"} for path in coverage["missing_paths"]]
        raise PlanManifestCoverageError(
            "file manifest does not cover implementation artifacts: " + ", ".join(coverage["missing_paths"]),
            coverage["missing_paths"], diagnostics)
    guard = PathGuard([str(root)])
    raw, source = _raw_manifest(plan)
    read_only_manifest: list[dict[str, Any]] = []
    mutable_raw: list[Any] = []
    for item in raw:
        if isinstance(item, dict):
            role = str(item.get("role") or "").strip().lower().replace("-", "_")
            action = str(item.get("action") or item.get("operation") or "").strip().lower()
            if bool(item.get("read_only")) or role in {"read_only", "readonly", "evidence", "inspection", "verification"} or action in {"read", "inspect", "verify"}:
                read_only_manifest.append(dict(item))
                continue
        mutable_raw.append(item)
    raw = mutable_raw
    normalized: list[dict[str, Any]] = []
    normalized_fields: dict[str, str] = {}
    seen: set[str] = set()
    errors: list[str] = []
    diagnostics: list[dict[str, str]] = []

    greenfield = bool((workspace or {}).get("greenfield"))
    profile = str((requirements or {}).get("task_profile") or "")
    new_project = bool((workspace or {}).get("new_project_intent") or
                       (greenfield and profile in {"FRONTEND_ONLY_MARKETING_SITE", "GENERAL_CODING"}) or
                       (greenfield and str((workspace or {}).get("project_type") or "").lower() in {"frontend", "website", "new_project"}))
    normalized_create_from_modify: list[str] = []
    replan_reconciliation = bool((workspace or {}).get("replan_reconciliation"))
    current_task_created_paths = {
        Path(str(path).replace("\\", "/")).as_posix().lstrip("./")
        for path in ((workspace or {}).get("current_task_created_paths") or [])
        if isinstance(path, str) and path.strip()
    }
    current_task_provenance = {
        Path(str(path).replace("\\", "/")).as_posix().lstrip("./"): str(kind)
        for path, kind in (((workspace or {}).get("current_task_provenance") or {}).items())
        if isinstance(path, str) and path.strip()
    }
    reconciled_create_to_modify: list[str] = []

    def record_error(message: str, *, path: str = "UNKNOWN", field: str = "UNKNOWN",
                     phase: str = "UNKNOWN", reason: str | None = None) -> None:
        code = path_validation_code(reason or message)
        errors.append(message)
        diagnostics.append({
            "offending_plan_path": path or "UNKNOWN",
            "offending_plan_field": field or "UNKNOWN",
            "offending_phase": phase or "UNKNOWN",
            "path_validation_reason": reason or message,
            "path_validation_code": code,
        })

    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"path": item, "action": "modify"}
        if not isinstance(item, dict):
            record_error(f"manifest[{index}] must be an object", field=f"file_manifest[{index}]",
                         reason="manifest entry must be an object")
            continue
        path_value = _path_from_entry(item)
        action = str(item.get("action") or item.get("operation") or "modify").lower()
        if action == "add":
            action = "create"
        field = f"file_manifest[{index}].path"
        phase = str(item.get("owner_phase") or "UNKNOWN")
        if not path_value:
            record_error(f"manifest[{index}] path is required", field=field, phase=phase,
                         reason="path is required")
            continue
        if source == "DERIVED" and _derived_semantic_path(path_value, root):
            # The token came from prose rather than an explicit mutable
            # manifest.  Keep it as semantic context, never as a write target.
            continue
        if action not in _ACTIONS:
            record_error(f"manifest[{index}] has unsupported action {action}", path=path_value,
                         field=field, phase=phase, reason=f"unsupported action {action}")
            continue
        try:
            requested = Path(path_value).expanduser()
            target = (requested if requested.is_absolute() else root / requested).resolve()
            target = guard.resolve(str(target))
            relative = target.relative_to(root).as_posix()
            if relative in {"", "."}:
                raise PermissionError("manifest cannot target the project root")
        except (OSError, ValueError, PermissionError) as exc:
            record_error(f"manifest[{index}] path rejected: {exc}", path=path_value, field=field,
                         phase=phase, reason=str(exc))
            continue
        if relative in seen:
            record_error(f"manifest contains duplicate path: {relative}", path=path_value, field=field,
                         phase=phase, reason=f"duplicate normalized path {relative}")
            continue
        seen.add(relative)
        normalized_fields[relative] = field
        # A greenfield conversion is only valid when the complete workspace
        # snapshot proves that this is a new-project task.  Existing projects
        # and callers without explicit state retain strict MODIFY semantics.
        target_exists = target.exists()
        if (action == "create" and target_exists and replan_reconciliation and
                relative in current_task_created_paths):
            # A current-task artifact is now an existing mutable target.  A
            # replan may continue editing it, while a pre-task CREATE collision
            # remains a hard validation error below.
            action = "modify"
            reconciled_create_to_modify.append(relative)
        if action == "modify" and not target_exists and greenfield and new_project:
            action = "create"
            normalized_create_from_modify.append(relative)
        normalized.append({"path": relative, "action": action,
                           "role": str(item.get("role") or "implementation"),
                           "required": bool(item.get("required", True)),
                           "owner_phase": str(item.get("owner_phase") or "")})

    # Inspect all targets before the first mutation.
    for item in normalized:
        target = root / item["path"]
        exists = target.exists()
        item["exists_before"] = exists
        if item["action"] == "modify" and not exists:
            record_error(f"modify target does not exist: {item['path']}", path=item["path"],
                         field=normalized_fields.get(item["path"], "file_manifest[].path"), phase=item.get("owner_phase") or "UNKNOWN",
                         reason="modify target does not exist")
        elif item["action"] == "delete" and not exists:
            record_error(f"delete target does not exist: {item['path']}", path=item["path"],
                         field=normalized_fields.get(item["path"], "file_manifest[].path"), phase=item.get("owner_phase") or "UNKNOWN",
                         reason="delete target does not exist")
        elif item["action"] == "create" and exists:
            try:
                non_empty = target.is_dir() or (target.stat().st_size > 0 and target.read_text(encoding="utf-8") != _safe_stub(target))
            except (OSError, UnicodeError):
                non_empty = True
            if non_empty:
                record_error(f"create target already exists: {item['path']}", path=item["path"],
                             field=normalized_fields.get(item["path"], "file_manifest[].path"), phase=item.get("owner_phase") or "UNKNOWN",
                             reason="create target already exists")

    if errors:
        raise PlanPathValidationError("; ".join(errors), diagnostics)

    created = 0
    skipped_existing = 0
    scaffold_created_paths: list[str] = []
    for item in normalized:
        if item["action"] != "create":
            continue
        target = root / item["path"]
        if target.exists():
            skipped_existing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_safe_stub(target), encoding="utf-8")
        created += 1
        scaffold_created_paths.append(item["path"])

    phases = plan.get("phases") or []
    artifact = {
        "artifact_version": 1,
        "task_id": task_id,
        "plan_revision": 0,
        "target_root": str(root),
        "task_profile": (requirements or {}).get("task_profile"),
        "requirements_hash": (requirements or {}).get("requirements_hash"),
        "file_manifest": normalized,
        "dependencies": [{"phase_id": p.get("id"), "depends_on": list(p.get("dependencies") or [])}
                          for p in phases if isinstance(p, dict)],
        "verification_targets": [{"phase_id": p.get("id"), "verify": list(p.get("verify") or [])}
                                  for p in phases if isinstance(p, dict)],
        "manifest_source": source,
        "read_only_manifest": read_only_manifest,
        "path_validation": "PASS",
        "scaffold_created_count": created,
        "scaffold_skipped_existing_count": skipped_existing,
        "scaffold_created_paths": scaffold_created_paths,
        "current_task_created_paths": sorted(current_task_created_paths | set(scaffold_created_paths)),
        "current_task_provenance": {
            **current_task_provenance,
            **{path: "CREATED_BY_CURRENT_TASK_SCAFFOLD" for path in scaffold_created_paths},
        },
        "reconciled_create_to_modify": reconciled_create_to_modify,
        "normalized_create_from_modify": normalized_create_from_modify,
        "workspace_state": dict(workspace or {}),
        "manifest_coverage": coverage,
        "artifact_provenance": (coverage.get("requirement_types") or {}).get("ARTIFACT_PROVENANCE") or {},
        "canonicalization_changes": canonicalization_changes,
        "stack_conformance": conformance,
    }
    artifact["file_manifest_count"] = len(normalized)
    artifact["file_manifest_create_count"] = sum(x["action"] == "create" for x in normalized)
    artifact["file_manifest_modify_count"] = sum(x["action"] == "modify" for x in normalized)
    artifact["file_manifest_delete_count"] = sum(x["action"] == "delete" for x in normalized)
    return {**plan, "file_manifest": normalized, "read_only_manifest": read_only_manifest,
            "implementation_plan_artifact": artifact}, artifact
