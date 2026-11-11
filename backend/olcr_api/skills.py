"""Native locally installed Skills and managed remote Skill intake.

Installed Skills are discovered and loaded directly from their local source.
Remote intake retains its separate quarantine and activation workflow.
Discovery reads instructions only; it never executes a Skill script.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class SkillRecord:
    name: str
    version: str
    source: str
    content_hash: str
    state: str
    enabled: bool
    trusted: bool
    permission: str
    description: str = ""
    scope: str = "project"
    revision: str = ""
    scripts: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    requested_permissions: tuple[str, ...] = ()
    validation_state: str = "UNVALIDATED"
    blocked: bool = False
    update_state: str = "CURRENT"


class SkillPolicyError(ValueError):
    pass


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    if not name or len(name) > 96:
        raise SkillPolicyError("INVALID_SKILL_NAME")
    return name


def content_hash(content: str | bytes) -> str:
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SkillManager:
    """Unified managed registry and native local Skill discovery."""
    def __init__(self, root: str | Path, *, global_root: str | Path | None = None,
                 quarantine_root: str | Path | None = None,
                 skills_cli_root: str | Path | None = None):
        self.root = Path(root).resolve()
        self._unified_layout = global_root is not None or quarantine_root is not None
        self.global_root = Path(global_root).expanduser().resolve() if global_root else None
        self.quarantine = (Path(quarantine_root).expanduser().resolve() if quarantine_root else self.root / "quarantine")
        self.skills_cli_root = Path(skills_cli_root).expanduser().resolve() if skills_cli_root else None
        self.active = self.root if self._unified_layout else self.root / "active"
        self.registry_path = self.root / "registry.json"
        self.global_registry_path = self.global_root / "registry.json" if self.global_root else None
        self.lock_path = self.root.parent / "skills.lock" if self._unified_layout else self.root / "skills.lock"

    def _read_cli_source(self, directory_name: str) -> tuple[Path, str]:
        if (not self.skills_cli_root or not directory_name or directory_name in {".", ".."}
                or Path(directory_name).name != directory_name):
            raise SkillPolicyError("SKILL_CLI_SOURCE_UNSAFE")
        try:
            # A symlink is a normal local installation too. Resolve its base so
            # references and bundled scripts remain relative to the real Skill.
            source = (self.skills_cli_root / directory_name / "SKILL.md").resolve(strict=True)
            if not source.is_file():
                raise SkillPolicyError("SKILL_CLI_SOURCE_NOT_FOUND")
            if source.stat().st_size > 500_000:
                raise SkillPolicyError("SKILL_CLI_SOURCE_TOO_LARGE")
            raw = source.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise SkillPolicyError("SKILL_CLI_SOURCE_NOT_FOUND") from exc
        if not raw or len(raw) > 500_000:
            raise SkillPolicyError("SKILL_CLI_SOURCE_EMPTY_OR_TOO_LARGE")
        try:
            return source, raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillPolicyError("SKILL_CLI_SOURCE_ENCODING") from exc

    def cli_inventory(self) -> list[dict[str, Any]]:
        """Installed local Skills are immediately usable without OLCR intake."""
        root = self.skills_cli_root
        if not root or not root.is_dir():
            return []
        candidates: dict[str, dict[str, Any]] = {}
        for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not directory.is_dir():
                continue
            try:
                source, content = self._read_cli_source(directory.name)
                metadata = self._skill_frontmatter(content)
                name = _safe_name(metadata.get("name") or directory.name)
            except SkillPolicyError:
                continue
            digest = content_hash(content)
            candidates.setdefault(name, {
                "name": name,
                "description": metadata.get("description", ""),
                "version": metadata.get("version", "0"),
                "source": "skills-cli-local",
                "source_directory": directory.name,
                "base_directory": str(source.parent),
                "scope": "global",
                "revision": digest,
                "content_hash": digest,
                "state": "ACTIVE",
                "enabled": True,
                "trusted": True,
                "permission": "local_installation",
                "native": True,
            })
        return list(candidates.values())

    def intake_cli_skill(self, name: str, *, trusted: bool = False,
                         expected_hash: str | None = None) -> dict[str, Any]:
        """Copy an installed Skills CLI item into quarantine for OLCR validation."""
        reviewed = self.review_cli_skill(name)
        if expected_hash and reviewed["content_hash"] != expected_hash:
            raise SkillPolicyError("SKILL_CLI_SOURCE_CHANGED")
        metadata = reviewed["metadata"]
        return self.intake(
            name=reviewed["name"],
            version=metadata.get("version", reviewed.get("version", "0")),
            source=f"skills-cli-local:{reviewed['name']}",
            content=reviewed["content"],
            trusted=trusted,
            description=metadata.get("description", reviewed.get("description", "")),
            scope="global",
            revision=reviewed["content_hash"],
        )

    def review_cli_skill(self, name: str) -> dict[str, Any]:
        """Return bounded, read-only review evidence for one local CLI Skill."""
        safe = _safe_name(name)
        candidate = next((row for row in self.cli_inventory() if row["name"] == safe), None)
        if not candidate or not self.skills_cli_root:
            raise SkillPolicyError("SKILL_CLI_SOURCE_NOT_FOUND")
        source, content = self._read_cli_source(str(candidate.get("source_directory") or ""))
        digest = content_hash(content)
        if digest != candidate["content_hash"]:
            raise SkillPolicyError("SKILL_CLI_SOURCE_CHANGED")
        metadata = self._skill_frontmatter(content)
        scan = self.static_scan(content)
        return {
            "name": safe,
            "version": metadata.get("version", candidate.get("version", "0")),
            "description": metadata.get("description", candidate.get("description", "")),
            "content_hash": digest,
            "content": content,
            "metadata": metadata,
            "static_scan": scan,
            "inventory": self._inventory(content),
            "requested_permissions": self._permissions(content),
        }

    @staticmethod
    def _skill_frontmatter(content: str) -> dict[str, str]:
        parts = content.split("---", 2)
        if len(parts) < 3 or parts[0].strip():
            return {}
        metadata: dict[str, str] = {}
        lines = parts[1].splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
            if match:
                key, value = match.group(1).casefold(), match.group(2).strip()
                if value in {">", "|", ">-", "|-"}:
                    folded = value.startswith(">")
                    chunks: list[str] = []
                    index += 1
                    while index < len(lines):
                        continuation = lines[index]
                        if continuation.strip() and not continuation[0].isspace():
                            break
                        if continuation.strip():
                            chunks.append(continuation.strip())
                        elif not folded:
                            chunks.append("")
                        index += 1
                    metadata[key] = (" " if folded else "\n").join(chunks).strip()
                    continue
                metadata[key] = value.strip('"\'')
            index += 1
        return metadata

    def _active_root(self, row: dict[str, Any]) -> Path:
        if row.get("scope") == "global" and self.global_root:
            return self.global_root
        return self.active

    def _active_artifact_valid(self, row: dict[str, Any]) -> bool:
        path = self._active_root(row) / f"{row['name']}.skill"
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        return bool(raw and len(raw) <= 500_000 and content_hash(raw) == row.get("content_hash"))

    def _registry_for_scope(self, scope: str) -> Path:
        if scope == "global" and self.global_registry_path:
            return self.global_registry_path
        return self.registry_path

    def _ensure_roots(self) -> None:
        self.quarantine.mkdir(parents=True, exist_ok=True)
        self.active.mkdir(parents=True, exist_ok=True)
        if self.global_root:
            self.global_root.mkdir(parents=True, exist_ok=True)

    def intake(self, *, name: str, version: str, source: str, content: str,
               trusted: bool = False, enabled: bool = False, description: str = "",
               scope: str = "project", revision: str = "") -> dict[str, Any]:
        self._ensure_roots()
        safe = _safe_name(name)
        digest = content_hash(content)
        path = self.quarantine / f"{safe}-{digest[:12]}.skill"
        path.write_text(content, encoding="utf-8")
        scan = self.static_scan(content)
        inventory = self._inventory(content)
        permissions = self._permissions(content)
        record = SkillRecord(safe, str(version or "0"), str(source or "unknown"), digest,
                             "QUARANTINED", False, bool(trusted), "REVIEW_REQUIRED",
                             str(description or ""), str(scope or "project"), str(revision or ""),
                             tuple(inventory["scripts"]), tuple(inventory["references"]),
                             tuple(permissions), "PASS" if scan["status"] == "PASS" else "FAILED",
                             False, "CURRENT")
        self._write_registry(record)
        return {"status": "QUARANTINED", "skill": asdict(record), "path": str(path),
                "static_scan": scan, "inventory": inventory, "requested_permissions": permissions}

    def validate(self, name: str, *, expected_hash: str | None = None) -> dict[str, Any]:
        row = self._find(name)
        if not row:
            raise SkillPolicyError("SKILL_NOT_FOUND")
        path = self.quarantine / f"{row['name']}-{row['content_hash'][:12]}.skill"
        if not path.is_file():
            raise SkillPolicyError("SKILL_QUARANTINE_MISSING")
        actual = content_hash(path.read_bytes())
        if actual != row["content_hash"] or (expected_hash and actual != expected_hash):
            raise SkillPolicyError("SKILL_HASH_MISMATCH")
        if not path.read_text(encoding="utf-8").strip():
            raise SkillPolicyError("SKILL_EMPTY")
        content = path.read_text(encoding="utf-8")
        scan = self.static_scan(content)
        if scan["status"] != "PASS":
            raise SkillPolicyError("SKILL_STATIC_SCAN_FAILED")
        return {"status": "VALID", "skill": row, "content_hash": actual, "static_scan": scan,
                "inventory": self._inventory(content), "requested_permissions": self._permissions(content)}

    def activate(self, name: str, *, permission: str = "explicit") -> dict[str, Any]:
        validation = self.validate(name)
        row = dict(validation["skill"])
        if not row.get("trusted"):
            raise SkillPolicyError("SKILL_TRUST_REQUIRED")
        if row.get("blocked"):
            raise SkillPolicyError("SKILL_BLOCKED")
        if permission not in {"explicit", "approved"}:
            raise SkillPolicyError("SKILL_PERMISSION_REQUIRED")
        source = self.quarantine / f"{row['name']}-{row['content_hash'][:12]}.skill"
        target = self._active_root(row) / f"{row['name']}.skill"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        row.update({"state": "ACTIVE", "enabled": True, "permission": permission})
        self._write_registry(SkillRecord(**row))
        return {"status": "ACTIVE", "skill": row, "path": str(target)}

    def registry(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for path in (self.registry_path, self.global_registry_path):
            if not path or not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, list):
                rows.update({str(row.get("name")): row for row in value if isinstance(row, dict) and row.get("name")})
        return sorted(rows.values(), key=lambda row: str(row.get("name")))

    def list(self) -> list[dict[str, Any]]:
        """Effective installed inventory, with local source taking precedence."""
        rows = {row["name"]: row for row in self.registry()}
        rows.update({row["name"]: row for row in self.cli_inventory()})
        return sorted(rows.values(), key=lambda row: row["name"])

    def install(self, name: str, *, permission: str = "explicit") -> dict[str, Any]:
        """Install only after quarantine validation and explicit approval."""
        return self.activate(name, permission=permission)

    def check_updates(self) -> dict[str, Any]:
        return self.updates()

    def stage_update(self, name: str, *, content: str, revision: str = "") -> dict[str, Any]:
        """Stage replacement content in quarantine; never activates automatically."""
        row = self._find(name)
        if not row:
            raise SkillPolicyError("SKILL_NOT_FOUND")
        staged = self.intake(name=name, version=str(row.get("version", "0")),
                             source=str(row.get("source", "unknown")), content=content,
                             trusted=bool(row.get("trusted")), scope=str(row.get("scope", "project")),
                             revision=revision or str(row.get("revision", "")))
        staged["status"] = "STAGED_UPDATE"
        staged["skill"]["update_state"] = "STAGED"
        self._write_registry(SkillRecord(**staged["skill"]))
        return staged

    def enable(self, name: str) -> dict[str, Any]:
        return self.set_enabled(name, True)

    def disable(self, name: str) -> dict[str, Any]:
        return self.set_enabled(name, False)

    def load(self, name: str) -> dict[str, Any]:
        """Lazy-load an installed local source or a managed active Skill."""
        safe = _safe_name(name)
        local = next((row for row in self.cli_inventory() if row["name"] == safe), None)
        if local:
            source, content = self._read_cli_source(local["source_directory"])
            if content_hash(content) != local["content_hash"]:
                raise SkillPolicyError("SKILL_CLI_SOURCE_CHANGED")
            return {"skill": local,
                    "content": f"Skill base directory: {source.parent}\nResolve relative references and scripts from this directory.\n\n{content}",
                    "base_directory": str(source.parent)}
        row = self._find(name)
        if not row:
            raise SkillPolicyError("SKILL_NOT_FOUND")
        if row.get("state") != "ACTIVE" or not row.get("enabled"):
            raise SkillPolicyError("SKILL_NOT_INSTALLED_OR_ENABLED")
        if row.get("blocked"):
            raise SkillPolicyError("SKILL_BLOCKED")
        path = self._active_root(row) / f"{row['name']}.skill"
        if not path.is_file():
            raise SkillPolicyError("SKILL_ACTIVE_MISSING")
        return {"skill": row, "content": path.read_text(encoding="utf-8")}

    def lock(self) -> dict[str, Any]:
        rows = self.search()
        payload = {"version": 1, "skills": sorted(rows, key=lambda row: row["name"])}
        payload["lock_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    @staticmethod
    def static_scan(content: str) -> dict[str, Any]:
        """Conservative source scan; approval is still required to activate."""
        forbidden = [r"curl\s+[^\n|]+\|\s*(?:sh|bash)", r"\brm\s+-rf\b",
                     r"(?:os\.system|subprocess\.(?:run|Popen|call)|eval\s*\()",
                     r"(?:requests\.(?:get|post)|urllib\.request|socket\.)"]
        hits = [pattern for pattern in forbidden if re.search(pattern, content, re.I)]
        return {"status": "FAIL" if hits else "PASS", "forbidden_pattern_count": len(hits),
                "patterns": hits, "network_or_shell": bool(hits)}

    @staticmethod
    def _inventory(content: str) -> dict[str, list[str]]:
        paths = sorted(set(re.findall(r"(?:scripts?|references?)/[A-Za-z0-9_./-]+", content, re.I)))
        return {"scripts": [item for item in paths if item.lower().startswith("script")],
                "references": [item for item in paths if item.lower().startswith("reference")]}

    @staticmethod
    def _permissions(content: str) -> list[str]:
        permissions: list[str] = []
        if re.search(r"\b(?:shell|command|terminal|subprocess)\b", content, re.I):
            permissions.append("shell")
        if re.search(r"\b(?:network|http|internet|download|web)\b", content, re.I):
            permissions.append("network")
        if re.search(r"\b(?:secret|credential|token|api[_ -]?key)\b", content, re.I):
            permissions.append("secrets")
        if re.search(r"\b(?:delete|remove|destructive|write|modify)\b", content, re.I):
            permissions.append("filesystem_write")
        return sorted(set(permissions))

    def search(self, query: str = "") -> list[dict[str, Any]]:
        value = str(query or "").casefold()
        return [row for row in self.list() if row.get("state") == "ACTIVE" and row.get("enabled")
                and (row.get("native") or self._active_artifact_valid(row))
                and (not value or value in str(row.get("name", "")).casefold()
                     or value in str(row.get("source", "")).casefold())]

    def inspect(self, name: str) -> dict[str, Any]:
        local = next((row for row in self.cli_inventory() if row["name"] == _safe_name(name)), None)
        if local:
            return {"skill": local, "content_hash": local["content_hash"],
                    "active_path": str(Path(local["base_directory"]) / "SKILL.md"),
                    "quarantine_path": None}
        row = self._find(name)
        if not row:
            raise SkillPolicyError("SKILL_NOT_FOUND")
        return {"skill": row, "content_hash": row.get("content_hash"),
                "active_path": str(self._active_root(row) / f"{row['name']}.skill"),
                "quarantine_path": str(self.quarantine / f"{row['name']}-{row['content_hash'][:12]}.skill")}

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        row = self._find(name)
        if not row or row.get("state") != "ACTIVE":
            raise SkillPolicyError("SKILL_NOT_ACTIVE")
        if enabled and not self._active_artifact_valid(row):
            raise SkillPolicyError("SKILL_ACTIVE_MISSING")
        updated_data = dict(row)
        updated_data.update({"state": "ACTIVE", "enabled": bool(enabled)})
        updated = SkillRecord(**updated_data)
        self._write_registry(updated)
        return asdict(updated)

    def remove(self, name: str) -> dict[str, Any]:
        row = self._find(name)
        if not row:
            raise SkillPolicyError("SKILL_NOT_FOUND")
        for path in (self._active_root(row) / f"{row['name']}.skill", self.quarantine / f"{row['name']}-{row['content_hash'][:12]}.skill"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        path = self._registry_for_scope(str(row.get("scope") or "project"))
        if path.is_file():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows = []
            path.write_text(json.dumps([item for item in rows if item.get("name") != row["name"]], ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "REMOVED", "name": row["name"]}

    def updates(self) -> dict[str, Any]:
        return {"status": "DISABLED", "auto_update": False, "network": False, "updates": []}

    def _find(self, name: str) -> dict[str, Any] | None:
        safe = _safe_name(name)
        return next((row for row in self.registry() if row.get("name") == safe), None)

    def _write_registry(self, record: SkillRecord) -> None:
        path = self._registry_for_scope(record.scope)
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = []
        else:
            current = []
        rows = [row for row in current if isinstance(row, dict) and row.get("name") != record.name]
        rows.append(asdict(record))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(rows, key=lambda row: row["name"]), ensure_ascii=False, indent=2), encoding="utf-8")
