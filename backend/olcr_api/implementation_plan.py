"""Planning artifacts and safe preflight scaffolding for Coding Tasks.

The helpers in this module are deliberately filesystem boring: every manifest
entry is resolved under one canonical project root, every entry is checked
before the first write, and only tiny syntax-valid placeholders are created.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .retrieval import PathGuard


_ACTIONS = {"create", "modify", "delete"}
_PATH_TOKEN = re.compile(r"(?<![\w.-])([\w./-]+\.(?:html?|css|scss|js|mjs|ts|tsx|jsx|py|json|vue|svelte))(?![\w.-])", re.I)


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
            source.extend(_PATH_TOKEN.findall(value))
    for phase in plan.get("phases") or []:
        for key in ("goal", "done", "verify"):
            values = phase.get(key) if isinstance(phase, dict) else None
            values = values if isinstance(values, list) else [values]
            for value in values:
                if isinstance(value, str):
                    source.extend(_PATH_TOKEN.findall(value))
    seen: set[str] = set()
    return [{"path": value, "action": "modify"} for value in source if not (value in seen or seen.add(value))], "DERIVED"


def prepare_implementation_plan(task_id: str, plan: dict[str, Any], target_root: str | Path,
                               requirements: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a plan manifest, scaffold CREATE entries, and return artifact.

    Validation is complete before any file is written.  Existing CREATE files
    are never overwritten; empty files are counted as skipped and non-empty
    conflicts fail closed so the planner can revise its manifest.
    """
    root = Path(target_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("canonical target root must be an existing directory")
    guard = PathGuard([str(root)])
    raw, source = _raw_manifest(plan)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"path": item, "action": "modify"}
        if not isinstance(item, dict):
            errors.append(f"manifest[{index}] must be an object")
            continue
        path_value = _path_from_entry(item)
        action = str(item.get("action") or "modify").lower()
        if not path_value:
            errors.append(f"manifest[{index}] path is required")
            continue
        if action not in _ACTIONS:
            errors.append(f"manifest[{index}] has unsupported action {action}")
            continue
        try:
            requested = Path(path_value).expanduser()
            target = (requested if requested.is_absolute() else root / requested).resolve()
            target = guard.resolve(str(target))
            relative = target.relative_to(root).as_posix()
            if relative in {"", "."}:
                raise PermissionError("manifest cannot target the project root")
        except (OSError, ValueError, PermissionError) as exc:
            errors.append(f"manifest[{index}] path rejected: {exc}")
            continue
        if relative in seen:
            errors.append(f"manifest contains duplicate path: {relative}")
            continue
        seen.add(relative)
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
            errors.append(f"modify target does not exist: {item['path']}")
        elif item["action"] == "delete" and not exists:
            errors.append(f"delete target does not exist: {item['path']}")
        elif item["action"] == "create" and exists:
            try:
                non_empty = target.is_dir() or (target.stat().st_size > 0 and target.read_text(encoding="utf-8") != _safe_stub(target))
            except (OSError, UnicodeError):
                non_empty = True
            if non_empty:
                errors.append(f"create target already exists: {item['path']}")

    if errors:
        raise ValueError("; ".join(errors))

    created = 0
    skipped_existing = 0
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
        "path_validation": "PASS",
        "scaffold_created_count": created,
        "scaffold_skipped_existing_count": skipped_existing,
    }
    artifact["file_manifest_count"] = len(normalized)
    artifact["file_manifest_create_count"] = sum(x["action"] == "create" for x in normalized)
    artifact["file_manifest_modify_count"] = sum(x["action"] == "modify" for x in normalized)
    artifact["file_manifest_delete_count"] = sum(x["action"] == "delete" for x in normalized)
    return {**plan, "file_manifest": normalized, "implementation_plan_artifact": artifact}, artifact
