"""Narrow, task-scoped package-manager execution for authorized workspaces.

This module deliberately exposes one typed operation (PACKAGE_INSTALL).  It is
not a shell runner: commands are assembled from fixed argv templates, package
specifications are validated against canonical authorization, and the child
process always runs with the selected workspace as its exact cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping

from .retrieval import PathGuard


PACKAGE_MANAGER_OPERATION = "PACKAGE_INSTALL"
SUPPORTED_PACKAGE_MANAGERS = ("npm", "pnpm", "yarn")
DEFAULT_PACKAGE_MANAGER = "npm"  # Explicit OLCR frontend workspace policy.
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_PACKAGES = 24
MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_SUMMARY_CHARS = 2_000

_PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", re.I)
_VERSION = re.compile(r"^(?:[v=]?\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]+)?|[~^*<>=| 0-9A-Za-z.*+\-]+)$")
_FORBIDDEN_SPEC_CHARS = re.compile(r"[\s;&|`$()<>\\\"']")


class PackageManagerError(RuntimeError):
    """Typed package-manager rejection or process failure."""

    def __init__(self, reason: str, *, failure_class: str = "PACKAGE_MANAGER_ERROR", **details: Any):
        super().__init__(reason)
        self.failure_class = failure_class
        self.details = details


def _package_name(spec: str) -> str:
    value = str(spec).strip()
    if value.startswith("@"):
        slash = value.find("/")
        at = value.find("@", 1)
        return value[:at] if at > slash > 0 else value
    return value.split("@", 1)[0]


def _validate_spec(spec: Any) -> tuple[str, str]:
    if not isinstance(spec, str) or not spec.strip():
        raise PackageManagerError("package specification must be a non-empty string",
                                  failure_class="PACKAGE_SPEC_INVALID")
    value = spec.strip()
    if len(value) > 160 or _FORBIDDEN_SPEC_CHARS.search(value):
        raise PackageManagerError("package specification contains forbidden shell syntax",
                                  failure_class="PACKAGE_SPEC_INVALID")
    name = _package_name(value)
    if not _PACKAGE_NAME.fullmatch(name):
        raise PackageManagerError("package name is invalid", failure_class="PACKAGE_SPEC_INVALID")
    if value != name:
        version = value[len(name):]
        if not version.startswith("@") or not _VERSION.fullmatch(version[1:]):
            raise PackageManagerError("package version constraint is invalid",
                                      failure_class="PACKAGE_SPEC_INVALID")
    return value, name.lower()


def _canonical_authorized_packages(value: Any) -> dict[str, set[str]]:
    """Normalize a canonical package contract into name -> exact specs.

    A list of names authorizes those exact names.  A mapping can additionally
    authorize exact versioned specs, which lets a planner express a bounded
    major/version requirement without accepting arbitrary model input.
    """
    result: dict[str, set[str]] = {}
    if isinstance(value, Mapping):
        entries = []
        for name, specs in value.items():
            if isinstance(specs, (list, tuple, set)):
                entries.extend([str(spec) for spec in specs])
            elif specs is not None:
                name_text = str(name).strip()
                spec_text = str(specs).strip()
                # Mapping keys are canonical package names.  Scoped names
                # already contain '@', so checking for that character would
                # incorrectly discard the requested version suffix.
                entries.append(spec_text if spec_text == name_text or spec_text.startswith(f"{name_text}@")
                               else f"{name_text}@{spec_text}")
            else:
                entries.append(str(name))
    elif isinstance(value, (list, tuple, set)):
        entries = [str(item) for item in value]
    else:
        entries = []
    for entry in entries:
        try:
            spec, name = _validate_spec(entry)
        except PackageManagerError:
            continue
        result.setdefault(name, set()).add(spec)
    return result


def package_manager_from_project(root: str | Path, requested: str | None = None,
                                 *, allow_explicit_default: bool = False) -> dict[str, Any]:
    """Resolve a package manager from typed request or project metadata."""
    workspace = Path(root).expanduser().resolve()
    evidence: dict[str, Any] | None = None
    package_json = workspace / "package.json"
    if package_json.is_file():
        try:
            metadata = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise PackageManagerError("package.json is unreadable", failure_class="PACKAGE_MANAGER_METADATA_INVALID") from exc
        declared = metadata.get("packageManager")
        if isinstance(declared, str) and declared:
            manager = declared.split("@", 1)[0].lower()
            if manager not in SUPPORTED_PACKAGE_MANAGERS:
                raise PackageManagerError("package.json declares an unsupported package manager",
                                          failure_class="PACKAGE_MANAGER_UNSUPPORTED", package_manager=manager)
            evidence = {"package_manager": manager, "source": "PACKAGE_JSON_PACKAGE_MANAGER"}
    if evidence is None:
        for manager, lockfiles in (("npm", ("package-lock.json", "npm-shrinkwrap.json")),
                                   ("pnpm", ("pnpm-lock.yaml",)), ("yarn", ("yarn.lock",))):
            if any((workspace / filename).is_file() for filename in lockfiles):
                evidence = {"package_manager": manager, "source": "LOCKFILE"}
                break
    if requested is not None:
        manager = str(requested).strip().lower()
        if manager not in SUPPORTED_PACKAGE_MANAGERS:
            raise PackageManagerError("unsupported package manager", failure_class="PACKAGE_MANAGER_UNSUPPORTED",
                                      package_manager=manager)
        if evidence is not None and evidence["package_manager"] != manager:
            raise PackageManagerError("structured package manager conflicts with project evidence",
                                      failure_class="PACKAGE_MANAGER_CONFLICT",
                                      requested=manager, detected=evidence["package_manager"])
        return {"package_manager": manager, "source": "STRUCTURED_OPERATION"}
    if evidence is not None:
        return evidence
    if allow_explicit_default:
        return {"package_manager": DEFAULT_PACKAGE_MANAGER, "source": "EXPLICIT_OLCR_DEFAULT"}
    raise PackageManagerError("no package-manager evidence in selected workspace",
                              failure_class="PACKAGE_MANAGER_UNRESOLVED")


def _safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "CI", "NPM_CONFIG_CACHE"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _safe_output_summary(value: Any) -> str:
    """Keep a bounded diagnostic without retaining credentials or control codes."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or ""))
    text = re.sub(r"(?im)\b(_authToken|authToken|authorization|password|token)\s*[:=]\s*\S+",
                  lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[-MAX_OUTPUT_SUMMARY_CHARS:]


def _nonzero_failure_class(stdout: str, stderr: str) -> tuple[str, str]:
    """Classify only observable package-manager diagnostics."""
    reason = _safe_output_summary(" ".join((stderr, stdout)))
    lowered = reason.lower()
    if re.search(r"\b(?:e404|404 not found|package not found)\b", lowered):
        return "PACKAGE_NOT_FOUND", reason
    if re.search(r"\b(?:etarget|no matching version|version not found)\b", lowered):
        return "VERSION_NOT_FOUND", reason
    if re.search(r"\b(?:e401|e403|unauthorized|forbidden|authentication)\b", lowered):
        return "AUTH_FAILURE", reason
    if re.search(r"\b(?:enotfound|eai_again|econnrefused|econnreset|network|timed out)\b", lowered):
        return "DNS_OR_NETWORK_FAILURE", reason
    if re.search(r"\b(?:econfig|eexist|eacces|configuration|invalid config|invalid response body|npmrc|cache)\b", lowered):
        return "PACKAGE_MANAGER_CONFIG_FAILURE", reason
    return "PACKAGE_MANAGER_NONZERO_EXIT", reason or "package manager exited without diagnostic output"


def _failure_fingerprint(manager: str, packages: list[str], exit_code: int | None,
                         failure_class: str, stderr_summary: str) -> str:
    payload = {"package_manager": manager, "packages": packages, "exit_code": exit_code,
               "failure_class": failure_class, "stderr_reason": stderr_summary}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _artifact_state(root: Path) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for filename in ("package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"):
        path = root / filename
        if not path.is_file():
            output[filename] = {"exists": False}
            continue
        try:
            data = path.read_bytes()
            output[filename] = {"exists": True, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        except OSError as exc:
            output[filename] = {"exists": True, "read_error": type(exc).__name__}
    return output


class PackageManagerExecutor:
    """Execute one allowlisted package installation in one authorized root."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 popen=subprocess.Popen):
        self.timeout_seconds = float(timeout_seconds)
        self._popen = popen
        self._process: Any = None
        self._lock = threading.Lock()
        self._cancel_requested = False

    def cancel(self) -> bool:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is None or process.poll() is not None:
            return False
        try:
            if hasattr(os, "killpg") and getattr(process, "pid", None):
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    process.terminate()
            else:
                process.terminate()
            return True
        except (OSError, ProcessLookupError):
            return False

    def _argv(self, manager: str, packages: list[str], dependency_kind: str) -> list[str]:
        if manager == "npm":
            args = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
            if dependency_kind == "devDependencies":
                args.append("--save-dev")
            else:
                args.append("--save")
        elif manager == "pnpm":
            args = ["pnpm", "add", "--ignore-scripts"]
            if dependency_kind == "devDependencies":
                args.append("--save-dev")
        else:
            args = ["yarn", "add", "--ignore-scripts"]
            if dependency_kind == "devDependencies":
                args.append("--dev")
        return [*args, *packages]

    def install(self, *, workspace_root: str | Path, packages: Any,
                package_manager: str | None = None,
                dependency_kind: str = "dependencies",
                authorized_packages: Any = None,
                authorized_workspace_root: str | Path | None = None,
                allow_explicit_default: bool = False) -> dict[str, Any]:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise PackageManagerError("selected workspace does not exist", failure_class="WORKSPACE_INVALID")
        if authorized_workspace_root is not None:
            canonical_root = Path(authorized_workspace_root).expanduser().resolve()
            if root != canonical_root:
                raise PackageManagerError("package-manager cwd differs from authorized project root",
                                          failure_class="WORKSPACE_OUTSIDE_ROOT")
        try:
            PathGuard([str(root)]).resolve(str(root))
        except PermissionError as exc:
            raise PackageManagerError("workspace is outside authorized root", failure_class="WORKSPACE_OUTSIDE_ROOT") from exc
        if dependency_kind not in {"dependencies", "devDependencies"}:
            raise PackageManagerError("dependency_kind is not allowed", failure_class="DEPENDENCY_KIND_INVALID")
        if not isinstance(packages, list) or not packages or len(packages) > MAX_PACKAGES:
            raise PackageManagerError("packages must be a bounded non-empty list", failure_class="PACKAGE_LIST_INVALID")
        auth = _canonical_authorized_packages(authorized_packages)
        requested_specs: list[str] = []
        authorized_specs: list[str] = []
        for raw in packages:
            spec, name = _validate_spec(raw)
            allowed = auth.get(name)
            if not allowed or spec not in allowed:
                raise PackageManagerError(f"package is not in canonical authorization: {name}",
                                          failure_class="PACKAGE_NOT_AUTHORIZED", package=name)
            requested_specs.append(spec)
            authorized_specs.append(spec if spec in allowed else name)
        resolved = package_manager_from_project(root, package_manager,
                                                allow_explicit_default=allow_explicit_default)
        manager = resolved["package_manager"]
        argv = self._argv(manager, requested_specs, dependency_kind)
        environment = _safe_environment()
        isolated_cache: Any = None
        # npm's user cache can contain stale file-vs-directory collisions (or
        # have permissions inherited from another process).  Keep this
        # operation deterministic and isolated without changing the selected
        # project's package-manager configuration or exposing cache values.
        if manager == "npm":
            isolated_cache = tempfile.TemporaryDirectory(prefix="olcr-npm-cache-")
            environment["NPM_CONFIG_CACHE"] = isolated_cache.name
        resolved_executable = shutil.which(argv[0], path=environment.get("PATH")) or argv[0]
        before = _artifact_state(root)
        started = time.monotonic()
        process = None
        timeout = False
        with self._lock:
            self._cancel_requested = False
        cancelled = False
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        try:
            process = self._popen(argv, cwd=str(root), env=environment, shell=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  start_new_session=True)
            with self._lock:
                self._process = process
            try:
                out, err = process.communicate(timeout=self.timeout_seconds)
                stdout, stderr = str(out or ""), str(err or "")
                exit_code = process.returncode
            except subprocess.TimeoutExpired as exc:
                timeout = True
                stdout = str(exc.stdout or "")
                stderr = str(exc.stderr or "")
                self.cancel()
                try:
                    out, err = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # A package manager may leave a child alive after
                    # SIGTERM.  Escalate the process group where possible,
                    # then reap the process so a timed-out install cannot
                    # leak into later phases.
                    try:
                        if hasattr(os, "killpg") and getattr(process, "pid", None):
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        else:
                            process.kill()
                    except (OSError, ProcessLookupError):
                        process.kill()
                    out, err = process.communicate(timeout=5)
                stdout += str(out or "")
                stderr += str(err or "")
                exit_code = process.returncode
            with self._lock:
                cancelled = self._cancel_requested
        except FileNotFoundError as exc:
            evidence = {"operation_type": PACKAGE_MANAGER_OPERATION, "package_manager": manager,
                        "package_manager_resolution": resolved, "packages_requested": requested_specs,
                        "packages_authorized": authorized_specs, "workspace_root": str(root), "cwd": str(root),
                        "process_started": False, "exit_code": None, "timeout": False, "cancelled": False,
                        "stdout_summary": "", "stderr_summary": "package-manager executable is unavailable",
                        "resolved_executable": resolved_executable, "argv": argv,
                        "environment_policy": {"mode": "ALLOWLIST", "keys": sorted(environment)},
                        "status": "FAIL", "dependency_requirements_satisfied": False}
            evidence["failure_fingerprint"] = _failure_fingerprint(manager, requested_specs, None,
                                                                       "PACKAGE_MANAGER_UNAVAILABLE", evidence["stderr_summary"])
            raise PackageManagerError("package-manager executable is unavailable",
                                      failure_class="PACKAGE_MANAGER_UNAVAILABLE", evidence=evidence,
                                      package_manager=manager) from exc
        finally:
            with self._lock:
                self._process = None
            if isolated_cache is not None:
                isolated_cache.cleanup()
        after = _artifact_state(root)
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        success = exit_code == 0 and not timeout and not cancelled
        stdout_summary = _safe_output_summary(stdout)
        stderr_summary = _safe_output_summary(stderr)
        failure_class, failure_reason = _nonzero_failure_class(stdout, stderr)
        evidence = {
            "operation_type": PACKAGE_MANAGER_OPERATION,
            "package_manager": manager,
            "package_manager_resolution": resolved,
            "packages_requested": requested_specs,
            "packages_authorized": authorized_specs,
            "workspace_root": str(root),
            "cwd": str(root),
            "process_started": process is not None,
            "exit_code": exit_code,
            "timeout": timeout,
            "cancelled": cancelled,
            "stdout_summary": stdout_summary,
            "stderr_summary": stderr_summary,
            "resolved_executable": resolved_executable,
            "environment_policy": {"mode": "ALLOWLIST", "keys": sorted(environment)},
            "cache_policy": "ISOLATED_TEMP_CACHE" if manager == "npm" else "PACKAGE_MANAGER_DEFAULT",
            "package_json_after": after.get("package.json"),
            "lockfile_after": {key: value for key, value in after.items() if "lock" in key},
            "artifacts_before": before,
            "artifacts_after": after,
            "elapsed_ms": elapsed_ms,
            "lifecycle_scripts": "DISABLED_IGNORE_SCRIPTS",
            "argv": argv,
            "status": "PASS" if success else "FAIL",
            "dependency_requirements_satisfied": bool(success),
        }
        if not success:
            failure_class = ("PACKAGE_MANAGER_TIMEOUT" if timeout else
                             "PACKAGE_MANAGER_CANCELLED" if cancelled else failure_class)
            evidence["failure_class"] = failure_class
            evidence["failure_reason"] = ("package-manager timeout" if timeout else
                                          "package-manager cancellation" if cancelled else failure_reason)
            evidence["failure_fingerprint"] = _failure_fingerprint(
                manager, requested_specs, exit_code, failure_class, stderr_summary)
            raise PackageManagerError("package-manager installation failed", failure_class=failure_class, evidence=evidence)
        return evidence
