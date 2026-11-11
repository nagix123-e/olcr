"""Task-scoped continuation policy, independent of resource and capability policy."""
import re
from copy import deepcopy

from .coding_tasks import (_current_task_text, _architecture_clauses, _phase_task, _merge_texts,
                           PHASE_EXECUTION_MODES, suppress_unrequested_verification)


def task_continuation_policy(goal: str, requirements: dict) -> dict:
    preference_text = _current_task_text(goal)
    text = "\n".join(clause for clause, negative in _architecture_clauses(goal) if not negative)
    patterns = {
        "new_product": r"(?:build|create|implement|構築|作成).{0,100}(?:website|homepage|application|site|サイト|アプリ)|(?:サイト|アプリ).{0,30}(?:作成|構築)",
        "dependencies": r"npm|install|dependencies|依存|導入",
        "motion": r"anime\.js|animation|アニメーション",
        "verification": r"playwright|typecheck|verification|\btests?\b|検証|テスト",
        "multiple_deliverables": r"(?:six|6|multiple).{0,30}(?:sections|deliverables)|複数.{0,20}(?:機能|画面)",
        "inspection": r"inspect|repository evidence|repo evidence|リポジトリ.*確認",
    }
    signals = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
    if {"frontend", "backend"} <= set(requirements.get("required_capabilities", [])):
        signals.append("multiple_deliverables")
    if len(requirements.get("required_mcps", [])) >= 2:
        signals.append("multiple_required_mcps")
    large = len(signals) >= 4 and ("new_product" in signals or "multiple_deliverables" in signals)
    size = "LARGE" if large else "MEDIUM" if len(signals) >= 2 else "SMALL"
    stops = re.search(r"大きいタスクは区切って止めて|各大きな工程ごとに止めて|フェーズごとに続行確認して|pause (?:after|between) (?:each )?(?:major )?phases?", preference_text, re.I)
    continuous = re.search(r"止めずに|中断せず|確認なしで最後まで|uninterrupted|without (?:pausing|stopping)|do not pause", preference_text, re.I)
    policy = "CONTINUOUS" if continuous else "MAJOR_CHECKPOINTS" if stops or large else "CONTINUOUS"
    return {"task_size": size, "task_size_signals": list(dict.fromkeys(signals)), "continuation_policy": policy,
            "preference": "CONTINUOUS" if continuous else "MAJOR_CHECKPOINTS" if stops else "DEFAULT"}


_STAGES = {
    "inspection": r"\binspect\w*|\bpreflight\b|repo(?:sitory)? (?:check|analysis)|MCP (?:consultation|query)|リポジトリ確認|事前検証",
    "installation": r"\binstall\w*|\bscaffold\w*|依存.*導入|足場",
    "implementation": r"\bimplement\w*|\bcreate\w*|\bdevelop\w*|実装|構築|作成",
    "verification": r"\bverif\w*|\btest\w*|playwright|browser|検証|テスト",
    "finalization": r"\bfinaliz\w*|final report|最終報告",
}


def phase_lifecycle_stages(phase: dict) -> list[str]:
    value = " ".join([phase.get("goal", ""), *phase.get("done", []), *phase.get("verify", [])])
    return [stage for stage, pattern in _STAGES.items() if re.search(pattern, value, re.I)]


def _clauses(value: str) -> list[str]:
    return [part.strip(" .、") for part in re.split(r"[;\n]|\s+(?:then|and then)\s+|\s+→\s+", value, flags=re.I) if part.strip()]


def _stage(value: str) -> str:
    if re.search(r"playwright|browser|responsive verification|accessibility verification|ブラウザ", value, re.I):
        return "verification"
    if re.search(_STAGES["implementation"] + "|" + _STAGES["installation"], value, re.I):
        return "implementation"
    if re.search(_STAGES["inspection"], value, re.I):
        return "blueprint"
    if re.search(_STAGES["finalization"], value, re.I):
        return "report"
    return "implementation"


def major_phase_plan(plan: dict, policy: dict, verification_requested: bool = False) -> dict:
    """Expose the persisted planning deliverable; add verification only on request.

    The blueprint is completed by the orchestrator after actual preflight and
    validated model planning, never dispatched as fictitious filesystem work.
    The default major-phase graph contains no verification-only stage.
    """
    if policy["task_size"] != "LARGE":
        return plan
    if not verification_requested:
        plan, _ = suppress_unrequested_verification(plan)
    if any(p.get("kind") == "ORCHESTRATOR_BLUEPRINT" for p in plan.get("phases", [])):
        return plan
    phases = deepcopy(plan.get("phases", []))
    if not phases:
        return plan
    source_tasks = {t.get("phase_id"): t for t in plan.get("tasks", [])}
    normalized = []
    normalized_sources: list[list[str]] = []
    final_checks = []
    final_check_sources: list[str] = []
    blueprint_criteria = []
    for phase in phases:
        source_task = source_tasks.get(phase["id"], {})
        phase["required_mcp"] = _merge_texts([phase.get("required_mcp"), source_task.get("required_mcp")])
        phase["selected_mcp"] = list(source_task.get("selected_mcp", []))
        phase["graph_context"] = {key: deepcopy(source_task.get(key, [])) for key in ("required_context", "change_scope")}
        stages = phase_lifecycle_stages(phase)
        # Typed execution intent is authoritative.  A verification criterion
        # may legitimately mention implementation output (for example,
        # "implementation-caused console errors are absent"); that wording
        # must never turn a read-only phase into a write-capable work phase.
        if (phase.get("execution_mode") == "VERIFICATION_ONLY"
                or (_stage(phase["goal"]) == "verification"
                    and "implementation" not in stages and "installation" not in stages)):
            phase["execution_mode"] = "VERIFICATION_ONLY"
            phase["required_mcp"] = [m for m in phase["required_mcp"] if m == "playwright"]
            phase["selected_mcp"] = [m for m in phase["selected_mcp"] if m == "playwright"]
            # Acceptance contracts such as MARKETING_STRUCTURE are owned by
            # the implementation phase.  Rechecking them in the browser
            # phase duplicates ownership and can make a read-only phase
            # impossible to satisfy.
            phase.pop("acceptance_contract", None)
            final_checks.append(phase)
            if phase.get("id"):
                final_check_sources.append(str(phase["id"]))
            continue
        routed = {key: {"blueprint": [], "report": [], "verification": [], "implementation": []} for key in ("goal", "done", "verify")}
        for key in routed:
            for value in ([phase["goal"]] if key == "goal" else phase.get(key, [])):
                for clause in _clauses(value):
                    routed[key][_stage(clause)].append(clause)
        blueprint_criteria.extend(routed["done"]["blueprint"] + routed["verify"]["blueprint"])
        check_done = routed["done"]["verification"]
        check_verify = routed["verify"]["verification"]
        if verification_requested and (check_done or check_verify or "playwright" in phase["required_mcp"]):
            check_phase = {**phase, "goal": "Consolidated browser verification and focused corrections",
                           "done": check_done or ["Requested browser checks pass"],
                           "verify": check_verify or check_done or ["Requested browser checks pass"],
                           "execution_mode": "VERIFICATION_ONLY",
                           "required_mcp": [m for m in phase["required_mcp"] if m == "playwright"],
                           "selected_mcp": [m for m in phase["selected_mcp"] if m == "playwright"]}
            check_phase.pop("acceptance_contract", None)
            for key in (
                "file_manifest", "requires_repo_mutation", "requires_write_operation",
                "requires_created_artifact", "requires_dependency_installation",
                "requires_migration", "requires_mcp_mutation", "requires_external_mutation",
                "requires_authorization_sensitive_side_effect", "package_json_config_mutation",
                "package_manager_executor", "authorized_package_requirements",
                "dependency_requirements",
            ):
                check_phase.pop(key, None)
            final_checks.append(check_phase)
        work = {**phase, "goal": " / ".join(routed["goal"]["implementation"]) or "Implement approved deliverables",
                "done": routed["done"]["implementation"] or ["Approved deliverables implemented"],
                "verify": (routed["verify"]["implementation"] or ["Focused implementation checks pass"])
                          if verification_requested else [],
                "required_mcp": [m for m in phase["required_mcp"] if m != "playwright"]}
        if work.get("execution_mode") not in PHASE_EXECUTION_MODES:
            work["execution_mode"] = "IMPLEMENTATION"
        # Mixed prose may be indivisible. Its original contract remains in the
        # blueprint; dispatch gets an explicit lifecycle boundary instead.
        if "inspection" in phase_lifecycle_stages(work) or "finalization" in phase_lifecycle_stages(work):
            work["goal"] = "Implement the approved blueprint deliverables with focused checks"
            for key in ("done", "verify"):
                work[key] = [s for s in work[key] if not re.search(_STAGES["inspection"] + "|" + _STAGES["finalization"], s, re.I)]
            work["done"] = work["done"] or ["Approved blueprint deliverables implemented"]
            if verification_requested:
                work["verify"] = work["verify"] or ["Focused implementation checks pass"]
        normalized.append(work)
        if phase.get("id"):
            normalized_sources.append([str(phase["id"])])
        else:
            normalized_sources.append([])

    def merge(rows):
        result = {**rows[0], "goal": " / ".join(row["goal"] for row in rows)}
        for key in ("done", "verify", "risks", "required_mcp", "selected_mcp"):
            result[key] = _merge_texts([row.get(key) for row in rows])
        if rows[0].get("execution_mode") != "VERIFICATION_ONLY":
            result["acceptance_contract"] = [
                item for item in _merge_texts([row.get("acceptance_contract") for row in rows])
                if item.lower() not in {"none", "null"}
            ]
        else:
            result.pop("acceptance_contract", None)
        result["graph_context"] = {key: _merge_texts([row.get("graph_context", {}).get(key) for row in rows]) for key in ("required_context", "change_scope")}
        modes = {row.get("execution_mode") for row in rows if row.get("execution_mode") is not None}
        if len(modes) == 1 and all(row.get("execution_mode") in PHASE_EXECUTION_MODES for row in rows):
            result["execution_mode"] = next(iter(modes))
        elif any(row.get("execution_mode") in {"IMPLEMENTATION_AND_VERIFICATION"} for row in rows):
            result["execution_mode"] = "IMPLEMENTATION_AND_VERIFICATION"
        else:
            # Merged work rows are mutation-capable by construction.  Keep a
            # valid explicit mode when older planner rows omitted it.
            result["execution_mode"] = "IMPLEMENTATION"
        return result

    # Retain coherent deliverables. Only coalesce adjacent work when necessary
    # to keep the whole task within five major boundaries.
    while len(normalized) > 3:
        normalized[-2:] = [merge(normalized[-2:])]
        normalized_sources[-2:] = [normalized_sources[-2] + normalized_sources[-1]]
    if not normalized:
        return plan
    final = merge(final_checks) if final_checks else {
        "goal": "Consolidated verification of the approved deliverables and focused corrections",
        "done": _merge_texts([p.get("done") for p in normalized]),
        "verify": _merge_texts([p.get("verify") for p in normalized]), "risks": [], "required_mcp": []}
    phases = normalized
    if verification_requested:
        final["execution_mode"] = "VERIFICATION_ONLY"
        final["required_mcp"] = [m for m in _merge_texts([final.get("required_mcp")]) if m == "playwright"]
        phases = normalized + [final]
    blueprint = {"id": "major-blueprint", "kind": "ORCHESTRATOR_BLUEPRINT",
                 "goal": "Repository context, required MCP evidence and implementation blueprint",
                 "done": ["Implementation blueprint persisted with preflight evidence"],
                 "verify": [], "dependencies": [], "status": "pending", "risks": [],
                 "execution_mode": "IMPLEMENTATION"}
    result = [blueprint]
    for index, phase in enumerate(phases, 1):
        result.append({**phase, "id": f"major-{index}", "dependencies": [result[-1]["id"]], "status": "pending"})
    tasks = []
    for phase in result:
        tasks.append(_phase_task(phase, phase["id"] + "-task", [tasks[-1]["task_id"]] if tasks else [], phase.get("required_mcp", [])))
        task = tasks[-1]
        task.update(phase.get("graph_context", {}))
        task["selected_mcp"] = phase.get("selected_mcp", [])
    phase_replacements: dict[str, str] = {}
    task_replacements: dict[str, str] = {}
    for index, source_ids in enumerate(normalized_sources, 1):
        replacement_phase = f"major-{index}"
        replacement_task = f"{replacement_phase}-task"
        for source_id in source_ids:
            phase_replacements[source_id] = replacement_phase
            source_task = source_tasks.get(source_id) or {}
            if source_task.get("task_id"):
                task_replacements[str(source_task["task_id"])] = replacement_task
    if verification_requested and final_checks:
        replacement_phase = f"major-{len(normalized) + 1}"
        replacement_task = f"{replacement_phase}-task"
        for source_id in final_check_sources:
            # A source phase can contain both implementation and verification
            # criteria. Writable artifacts belong to its implementation
            # successor; never replace that mapping with the read-only phase.
            phase_replacements.setdefault(source_id, replacement_phase)
            source_task = source_tasks.get(source_id) or {}
            if source_task.get("task_id"):
                task_replacements.setdefault(str(source_task["task_id"]), replacement_task)
    result_plan = {**plan, "phases": result, "tasks": tasks,
                   "blueprint": {"source_phases": deepcopy(plan["phases"]), "inspection_criteria": blueprint_criteria},
                   "phase_complexity": {"status": "PASS", "before": len(plan["phases"]), "after": len(result),
                                        "mixed_phases": [p["id"] for p in plan["phases"] if len(phase_lifecycle_stages(p)) >= 4]}}
    if phase_replacements or task_replacements:
        result_plan["graph_replacement_map"] = {"phase_ids": phase_replacements,
                                                 "task_ids": task_replacements}
    return result_plan
