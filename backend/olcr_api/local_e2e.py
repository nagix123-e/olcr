"""Project-scoped local E2E server ownership and loopback readiness checks."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
_MANIFEST_PATH = Path(".olcr") / "e2e.json"
_PROJECT_PACKAGE_PATH = Path("package.json")


def _available_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _package_script_runner(root: Path, package: dict) -> list[str]:
    declared = str(package.get("packageManager") or "").split("@", 1)[0].strip().lower()
    if declared in {"npm", "pnpm", "yarn"}:
        runner = declared
    elif (root / "pnpm-lock.yaml").is_file():
        runner = "pnpm"
    elif (root / "yarn.lock").is_file():
        runner = "yarn"
    else:
        runner = "npm"
    executable = shutil.which(runner)
    if not executable:
        raise ValueError("LOCAL_E2E_PACKAGE_MANAGER_NOT_FOUND")
    return [executable]


def _package_dev_command(root: Path, expected_origin: str | None,
                         requested_script: str | None = None) -> dict | None:
    package_path = root / _PROJECT_PACKAGE_PATH
    if not package_path.exists():
        return None
    if package_path.is_symlink() or not package_path.is_file() or package_path.stat().st_size > 128_000:
        raise ValueError("LOCAL_E2E_PACKAGE_CONFIG_INVALID")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("LOCAL_E2E_PACKAGE_CONFIG_INVALID") from exc
    if not isinstance(package, dict) or not isinstance(package.get("scripts"), dict):
        return None

    candidates: list[tuple[str, str]] = []
    for name in ("dev", "start", "serve"):
        if requested_script and name != requested_script:
            continue
        script = package["scripts"].get(name)
        if not isinstance(script, str) or len(script) > 2000:
            continue
        try:
            tokens = shlex.split(script)
        except ValueError:
            continue
        if not tokens:
            continue
        if any(character in script for character in "\r\n;&|<>`$"):
            raise ValueError("LOCAL_E2E_PACKAGE_SCRIPT_MUST_BE_A_SINGLE_COMMAND")
        for index, token in enumerate(tokens):
            if token.startswith(("--host=", "--hostname=", "--bind=")):
                bind_host = token.split("=", 1)[1]
            elif token in {"--host", "--hostname", "--bind", "-H"}:
                bind_host = tokens[index + 1] if index + 1 < len(tokens) else ""
            else:
                continue
            if bind_host not in _LOOPBACK_HOSTS:
                raise ValueError("LOCAL_E2E_PACKAGE_SCRIPT_MUST_BIND_LOOPBACK")
        executable = Path(tokens[0]).name.lower()
        if executable in {"vite", "vite.cmd", "vite.js"}:
            candidates.append((name, "vite"))
        elif executable in {"next", "next.cmd", "next.js"} and len(tokens) > 1 and tokens[1] == "dev":
            candidates.append((name, "next"))
        elif executable in {"astro", "astro.cmd"} and len(tokens) > 1 and tokens[1] == "dev":
            candidates.append((name, "astro"))
        elif executable in {"ng", "ng.cmd"} and len(tokens) > 1 and tokens[1] == "serve":
            candidates.append((name, "angular"))
        elif executable in {"webpack", "webpack.cmd"} and len(tokens) > 1 and tokens[1] == "serve":
            candidates.append((name, "webpack"))

    if not candidates:
        return None
    if len(candidates) != 1:
        names = ", ".join(name for name, _ in candidates)
        raise ValueError(f"LOCAL_E2E_SCRIPT_AMBIGUOUS: {names}")

    if expected_origin:
        normalized_origin = origin_from_url(expected_origin)
        if normalized_origin is None:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP")
        normalized = normalize_local_origin(normalized_origin)
        assert normalized is not None
        _scheme, host, port = normalized
    else:
        host, port = "127.0.0.1", _available_loopback_port()
        normalized_origin = f"http://127.0.0.1:{port}"

    script_name, framework = candidates[0]
    runner = _package_script_runner(root, package)
    if runner[0].endswith("npm") or Path(runner[0]).name == "npm":
        command = runner + ["run", script_name, "--", "--host", "127.0.0.1", "--port", str(port)]
    elif Path(runner[0]).name.startswith("yarn"):
        command = runner + ["run", script_name, "--host", "127.0.0.1", "--port", str(port)]
    else:
        command = runner + ["run", script_name, "--", "--host", "127.0.0.1", "--port", str(port)]
    if framework == "next":
        command[-4:] = ["--hostname", "127.0.0.1", "--port", str(port)]
    elif framework == "vite":
        command.append("--strictPort")
    return {"workspace": str(root), "origin": normalized_origin, "host": host, "port": port,
            "command": command, "manifest": None, "source": "package.json", "script": script_name}


def _chat_command_spec(root: Path, command_text: str, expected_origin: str | None) -> dict:
    """Validate a user-supplied, shell-free one-off command for a scoped local E2E server."""
    if not isinstance(command_text, str) or not command_text.strip() or len(command_text) > 2000:
        raise ValueError("LOCAL_E2E_COMMAND_INVALID")
    if any(character in command_text for character in "\r\n;&|<>$`\x00"):
        raise ValueError("LOCAL_E2E_COMMAND_MUST_BE_A_SINGLE_COMMAND")
    try:
        command = shlex.split(command_text, posix=True)
    except ValueError as exc:
        raise ValueError("LOCAL_E2E_COMMAND_INVALID") from exc
    if (not command or len(command) > 32
            or any(not part or len(part) > 300 or "\x00" in part for part in command)
            or sum(len(part) for part in command) > 4096):
        raise ValueError("LOCAL_E2E_COMMAND_INVALID")
    if Path(command[0]).name.lower() in {"sh", "bash", "zsh", "fish", "csh", "tcsh", "osascript"}:
        raise ValueError("LOCAL_E2E_COMMAND_SHELL_NOT_ALLOWED")

    bind_hosts: list[str] = []
    ports: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token.startswith(("--host=", "--hostname=", "--bind=")):
            bind_hosts.append(token.split("=", 1)[1])
        elif token in {"--host", "--hostname", "--bind", "-H"}:
            index += 1
            bind_hosts.append(command[index] if index < len(command) else "")
        elif token.startswith("--port="):
            ports.append(token.split("=", 1)[1])
        elif token == "--port":
            index += 1
            ports.append(command[index] if index < len(command) else "")
        index += 1
    if (len(bind_hosts) != 1 or bind_hosts[0] not in _LOOPBACK_HOSTS
            or len(ports) != 1 or not ports[0].isdigit()):
        raise ValueError("LOCAL_E2E_COMMAND_MUST_DECLARE_LOOPBACK_HOST_AND_PORT")
    port = int(ports[0])
    if not 1 <= port <= 65535:
        raise ValueError("LOCAL_E2E_COMMAND_MUST_DECLARE_LOOPBACK_HOST_AND_PORT")
    if expected_origin:
        requested = origin_from_url(expected_origin)
        normalized = normalize_local_origin(requested or "")
        if normalized is None or normalized[2] != port:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_MATCH_COMMAND_PORT")
        _scheme, host, _port = normalized
        origin = requested
    else:
        host = bind_hosts[0]
        origin = f"http://{host}" + (f":{port}" if port != 80 else "")

    executable = command[0]
    executable_path = Path(executable)
    if executable_path.is_absolute():
        if not executable_path.is_file():
            raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND")
    elif "/" in executable or executable.startswith("."):
        try:
            resolved_executable = (root / executable).resolve(strict=True)
            resolved_executable.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND") from exc
        if not resolved_executable.is_file():
            raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND")
        command[0] = str(resolved_executable)
    elif shutil.which(executable) is None:
        raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND")

    return {"workspace": str(root), "origin": origin, "host": host, "port": port,
            "command": command, "manifest": None, "source": "chat", "script": None}


def normalize_local_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(str(value or ""))
        if (parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        port = parsed.port or 80
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    host = parsed.hostname.lower()
    return ("http", host, port)


def origin_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
        normalized = normalize_local_origin(
            f"{parsed.scheme}://{parsed.netloc}/" if parsed.hostname else "")
    except (TypeError, ValueError):
        return None
    if normalized is None:
        return None
    scheme, host, port = normalized
    default_port = 80 if scheme == "http" else 443
    return f"{scheme}://{host}" + (f":{port}" if port != default_port else "")


def _workspace_in_roots(workspace: str | Path, allowed_roots: tuple[str, ...] | list[str]) -> Path:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("LOCAL_E2E_WORKSPACE_INVALID") from exc
    if not root.is_dir():
        raise ValueError("LOCAL_E2E_WORKSPACE_INVALID")
    for item in allowed_roots:
        try:
            allowed = Path(item).expanduser().resolve(strict=True)
        except OSError:
            continue
        try:
            root.relative_to(allowed)
            return root
        except ValueError:
            continue
    raise ValueError("LOCAL_E2E_WORKSPACE_OUT_OF_SCOPE")


def load_project_e2e_spec(workspace: str | Path, allowed_roots: tuple[str, ...] | list[str],
                          *, expected_origin: str | None = None,
                          requested_script: str | None = None,
                          requested_command: str | None = None) -> dict:
    root = _workspace_in_roots(workspace, allowed_roots)
    if requested_command is not None:
        return _chat_command_spec(root, requested_command, expected_origin)
    config_dir = root / ".olcr"
    manifest = root / _MANIFEST_PATH
    if config_dir.is_symlink() or manifest.is_symlink():
        raise ValueError("LOCAL_E2E_MANIFEST_INVALID")
    if manifest.is_file():
        if manifest.stat().st_size > 16_384:
            raise ValueError("LOCAL_E2E_MANIFEST_TOO_LARGE")
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("LOCAL_E2E_MANIFEST_INVALID") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("LOCAL_E2E_MANIFEST_INVALID")
        raw_origin = value.get("origin")
        normalized_origin = normalize_local_origin(str(raw_origin or ""))
        if normalized_origin is None:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP")
        scheme, host, port = normalized_origin
        origin = f"{scheme}://{host}" + (f":{port}" if port != 80 else "")
        if expected_origin:
            requested = origin_from_url(expected_origin)
            if requested != origin:
                raise ValueError("LOCAL_E2E_ORIGIN_MISMATCH")
        command = value.get("command")
        if (not isinstance(command, list) or not 2 <= len(command) <= 32
                or any(not isinstance(part, str) or not part or len(part) > 300 for part in command)
                or sum(len(part) for part in command) > 4096):
            raise ValueError("LOCAL_E2E_COMMAND_INVALID")
        if any("\x00" in part for part in command):
            raise ValueError("LOCAL_E2E_COMMAND_INVALID")
        # Project-declared commands must affirmatively bind to their exact
        # loopback origin; never broaden local tasks to a wildcard interface.
        host_flag_index = next((i for i, item in enumerate(command[:-1])
                                if item in {"--host", "--hostname", "--bind"}), None)
        port_flag_index = next((i for i, item in enumerate(command[:-1]) if item == "--port"), None)
        if (host_flag_index is None or command[host_flag_index + 1] not in {"127.0.0.1", "localhost"}
                or port_flag_index is None or command[port_flag_index + 1] != str(port)):
            raise ValueError("LOCAL_E2E_COMMAND_MUST_BIND_DECLARED_LOOPBACK_ORIGIN")
        executable = command[0]
        if Path(executable).is_absolute() and not Path(executable).is_file():
            raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND")
        if not Path(executable).is_absolute() and shutil.which(executable) is None:
            raise ValueError("LOCAL_E2E_EXECUTABLE_NOT_FOUND")
        return {"workspace": str(root), "origin": origin, "host": host, "port": port,
                "command": list(command), "manifest": str(manifest), "source": "manifest"}

    discovered = _package_dev_command(root, expected_origin, requested_script)
    if discovered:
        return discovered
    raise ValueError("LOCAL_E2E_MANIFEST_NOT_FOUND; no unambiguous supported package dev script")


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _probe_origin(host: str, port: int, timeout: float = 1.0) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/", headers={"Host": host})
        response = connection.getresponse()
        response.read(256)
        return 100 <= response.status < 500
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class LocalE2EManager:
    """Own process groups launched from a selected project's E2E config or script."""
    def __init__(self, *, popen=subprocess.Popen, startup_timeout: float = 30.0,
                 shutdown_timeout: float = 5.0):
        self._popen = popen
        self._startup_timeout = max(1.0, min(float(startup_timeout), 120.0))
        self._shutdown_timeout = max(0.5, min(float(shutdown_timeout), 20.0))
        self._lock = threading.RLock()
        self._runs: dict[str, dict] = {}

    def attach(self, workspace: str | Path, allowed_roots: tuple[str, ...] | list[str],
               origin: str) -> dict:
        root = _workspace_in_roots(workspace, allowed_roots)
        normalized = normalize_local_origin(origin)
        if normalized is None:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP")
        scheme, host, port = normalized
        canonical_origin = f"{scheme}://{host}" + (f":{port}" if port != 80 else "")
        if not _probe_origin(host, port):
            raise ValueError("LOCAL_E2E_ORIGIN_NOT_READY")
        run_id = str(uuid.uuid4())
        with self._lock:
            self._runs[run_id] = {"id": run_id, "workspace": str(root), "origin": canonical_origin,
                                  "owned": False, "process": None, "status": "READY"}
        return {"id": run_id, "origin": canonical_origin, "status": "READY", "owned_process": False}

    def start(self, workspace: str | Path, allowed_roots: tuple[str, ...] | list[str],
              *, expected_origin: str | None = None,
              requested_script: str | None = None,
              requested_command: str | None = None) -> dict:
        spec = load_project_e2e_spec(workspace, allowed_roots, expected_origin=expected_origin,
                                    requested_script=requested_script,
                                    requested_command=requested_command)
        if _port_is_open(spec["port"]):
            raise ValueError("LOCAL_E2E_PORT_IN_USE; use an explicit attach request to an existing local server")
        try:
            process = self._popen(spec["command"], cwd=spec["workspace"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  close_fds=True, start_new_session=True)
        except OSError as exc:
            raise ValueError("LOCAL_E2E_PROCESS_START_FAILED") from exc
        run_id = str(uuid.uuid4())
        run = {"id": run_id, "workspace": spec["workspace"], "origin": spec["origin"],
               "owned": True, "process": process, "pid": int(process.pid), "status": "STARTING",
               "source": spec["source"]}
        with self._lock:
            self._runs[run_id] = run
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.stop(run_id)
                raise ValueError("LOCAL_E2E_PROCESS_EXITED_BEFORE_READY")
            if _probe_origin(spec["host"], spec["port"]):
                with self._lock:
                    run["status"] = "READY"
                return {"id": run_id, "origin": spec["origin"], "status": "READY",
                        "owned_process": True, "pid": run["pid"]}
            time.sleep(0.1)
        self.stop(run_id)
        raise ValueError("LOCAL_E2E_STARTUP_TIMEOUT")

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(str(run_id))
            if not run:
                return None
            process = run.get("process")
            if process is not None and process.poll() is not None:
                run["status"] = "STOPPED"
            return {key: value for key, value in run.items() if key != "process"}

    def stop(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(str(run_id))
            if not run:
                raise KeyError("LOCAL_E2E_RUN_NOT_FOUND")
            process = run.get("process")
            if not run.get("owned") or process is None:
                return {"id": run_id, "status": run.get("status", "READY"),
                        "owned_process": False, "stopped": False}
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=self._shutdown_timeout)
            run["status"] = "STOPPED"
            run["process"] = None
            return {"id": run_id, "origin": run["origin"], "status": "STOPPED",
                    "owned_process": True, "stopped": True}

    def close_all(self) -> None:
        with self._lock:
            run_ids = [run_id for run_id, run in self._runs.items() if run.get("owned")]
        for run_id in run_ids:
            try:
                self.stop(run_id)
            except (KeyError, OSError, subprocess.TimeoutExpired):
                continue
