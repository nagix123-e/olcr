"""Task-scoped continuation policy, independent of resource and capability policy."""
import re
from copy import deepcopy

from .coding_tasks import (_current_task_text, _architecture_clauses, _phase_task, _merge_texts,
                           PHASE_EXECUTION_MODES)


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


def major_phase_plan(plan: dict, policy: dict) -> dict:
    """Expose the persisted planning deliverable; bound implementation/verification groups.

    The blueprint is completed by the orchestrator after actual preflight and
    validated model planning, never dispatched as fictitious filesystem work.
    """
    if policy["task_size"] != "LARGE":
        return plan
    if any(p.get("kind") == "ORCHESTRATOR_BLUEPRINT" for p in plan.get("phases", [])):
        return plan
    phases = deepcopy(plan.get("phases", []))
    if not phases:
        return plan
    source_tasks = {t.get("phase_id"): t for t in plan.get("tasks", [])}
    normalized = []
    final_checks = []
    blueprint_criteria = []
    for phase in phases:
        source_task = source_tasks.get(phase["id"], {})
        phase["required_mcp"] = _merge_texts([phase.get("required_mcp"), source_task.get("required_mcp")])
        phase["selected_mcp"] = list(source_task.get("selected_mcp", []))
        phase["graph_context"] = {key: deepcopy(source_task.get(key, [])) for key in ("required_context", "change_scope")}
        stages = phase_lifecycle_stages(phase)
        if _stage(phase["goal"]) == "verification" and "implementation" not in stages and "installation" not in stages:
            final_checks.append(phase)
            continue
        routed = {key: {"blueprint": [], "report": [], "verification": [], "implementation": []} for key in ("goal", "done", "verify")}
        for key in routed:
            for value in ([phase["goal"]] if key == "goal" else phase.get(key, [])):
                for clause in _clauses(value):
                    routed[key][_stage(clause)].append(clause)
        blueprint_criteria.extend(routed["done"]["blueprint"] + routed["verify"]["blueprint"])
        check_done = routed["done"]["verification"]
        check_verify = routed["verify"]["verification"]
        if check_done or check_verify or "playwright" in phase["required_mcp"]:
            final_checks.append({**phase, "goal": "Consolidated browser verification and focused corrections",
                                 "done": check_done or ["Requested browser checks pass"], "verify": check_verify})
        work = {**phase, "goal": " / ".join(routed["goal"]["implementation"]) or "Implement approved deliverables",
                "done": routed["done"]["implementation"] or ["Approved deliverables implemented"],
                "verify": routed["verify"]["implementation"] or ["Focused implementation checks pass"],
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
            work["verify"] = work["verify"] or ["Focused implementation checks pass"]
        normalized.append(work)

    def merge(rows):
        result = {**rows[0], "goal": " / ".join(row["goal"] for row in rows)}
        for key in ("done", "verify", "risks", "required_mcp", "selected_mcp", "acceptance_contract"):
            result[key] = _merge_texts([row.get(key) for row in rows])
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
    if not normalized:
        return plan
    final = merge(final_checks) if final_checks else {
        "goal": "Consolidated verification of the approved deliverables and focused corrections",
        "done": _merge_texts([p.get("done") for p in normalized]),
        "verify": _merge_texts([p.get("verify") for p in normalized]), "risks": [], "required_mcp": []}
    final["execution_mode"] = "VERIFICATION_ONLY"
    final["required_mcp"] = [m for m in _merge_texts([final.get("required_mcp")]) if m == "playwright"]
    phases = normalized + [final]
    blueprint = {"id": "major-blueprint", "kind": "ORCHESTRATOR_BLUEPRINT",
                 "goal": "Repository context, required MCP evidence and implementation blueprint",
                 "done": ["Validated implementation blueprint persisted with preflight evidence"],
                 "verify": ["Blueprint and preflight evidence recorded"], "dependencies": [], "status": "pending", "risks": [],
                 "execution_mode": "VERIFICATION_ONLY"}
    result = [blueprint]
    for index, phase in enumerate(phases, 1):
        result.append({**phase, "id": f"major-{index}", "dependencies": [result[-1]["id"]], "status": "pending"})
    tasks = []
    for phase in result:
        tasks.append(_phase_task(phase, phase["id"] + "-task", [tasks[-1]["task_id"]] if tasks else [], phase.get("required_mcp", [])))
        task = tasks[-1]
        task.update(phase.get("graph_context", {}))
        task["selected_mcp"] = phase.get("selected_mcp", [])
    return {**plan, "phases": result, "tasks": tasks,
            "blueprint": {"source_phases": deepcopy(plan["phases"]), "inspection_criteria": blueprint_criteria},
            "phase_complexity": {"status": "PASS", "before": len(plan["phases"]), "after": len(result),
                                 "mixed_phases": [p["id"] for p in plan["phases"] if len(phase_lifecycle_stages(p)) >= 4]}}
