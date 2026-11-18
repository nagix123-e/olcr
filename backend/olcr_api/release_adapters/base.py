"""Narrow, fail-closed interfaces shared by release provider adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request

from ..coding_tasks import ReleasePreflight
from ..release_workflow import ReleaseRequest, ReleaseResult


_COMMAND_ALLOWLIST = frozenset({"git", "gh", "xcrun", "codesign", "ditto"})
_COMMAND_TIMEOUT_MAX = 300
_OUTPUT_MAX_BYTES = 1024 * 1024
_COMMAND_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "APPDATA",
})
_PROFILE_CREDENTIAL_ENV = {
    "github": frozenset({"GH_TOKEN", "GITHUB_TOKEN"}),
    "apple_notary": frozenset(),
}
_API_HOST_PROFILES = {
    "github_api": "api.github.com",
    "app_store_connect_api": "api.appstoreconnect.apple.com",
    "google_play_api": "androidpublisher.googleapis.com",
}
_API_AUTH_SCHEME = {
    "github_api": "Bearer",
    "app_store_connect_api": "Bearer",
    "google_play_api": "Bearer",
}
_API_HEADERS_PROFILE = {
    "github_api": {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    "app_store_connect_api": {
        "Accept": "application/json",
    },
    "google_play_api": {
        "Accept": "application/json",
    },
}
_API_TIMEOUT_MAX = 30
_API_BODY_MAX_BYTES = 256 * 1024
_API_RESPONSE_MAX_BYTES = 1024 * 1024
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Za-z0-9_-]*(?:token|secret|password|credential|api[_ -]?key|authorization)"
    r"[A-Za-z0-9_-]*\b\s*[:=]\s*)[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b")
_CREDENTIAL_URL = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@runtime_checkable
class ReleaseAdapter(Protocol):
    """The provider operations required by the common release coordinator."""

    provider_id: str

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight: ...

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult: ...

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult: ...


@dataclass(frozen=True)
class CommandSpec:
    """A fixed executable and argv to run within a resolved workspace."""

    executable: str
    argv: tuple[str, ...]
    cwd: Path
    environment_profile: str = "github"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class CommandResult:
    """Bounded, redacted subprocess outcome; errors contain safe codes only."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    error_code: str | None


def _redact(value: str, secrets: tuple[str, ...] = ()) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", result)
    result = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", result)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _GITHUB_TOKEN.sub("[REDACTED]", result)
    return _CONTROL.sub(" ", result)


class ReleaseCommandRunner:
    """Run only allowlisted tools with an explicit environment and no shell."""

    def __init__(
        self,
        *,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        executable_resolver: Callable[[str], str | None] = shutil.which,
        process_runner: Callable = subprocess.Popen,
        max_output_bytes: int = _OUTPUT_MAX_BYTES,
    ) -> None:
        if not 1 <= max_output_bytes <= _OUTPUT_MAX_BYTES:
            raise ValueError("OUTPUT_LIMIT_INVALID")
        self.credential_provider = credential_provider
        self.executable_resolver = executable_resolver
        self.process_runner = process_runner
        self.max_output_bytes = max_output_bytes

    def _environment(self, profile: str) -> tuple[dict[str, str], tuple[str, ...]]:
        if profile not in _PROFILE_CREDENTIAL_ENV:
            raise ValueError("ENVIRONMENT_PROFILE_NOT_ALLOWED")
        env = {key: os.environ[key] for key in _COMMAND_ENV_ALLOWLIST if key in os.environ}
        env.update({"GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"})
        secrets: list[str] = []
        if self.credential_provider is not None:
            supplied = self.credential_provider(profile)
            if supplied is not None:
                if not isinstance(supplied, Mapping):
                    raise ValueError("CREDENTIAL_PROVIDER_INVALID")
                for key, value in supplied.items():
                    if key in _PROFILE_CREDENTIAL_ENV[profile] and isinstance(value, str) and value:
                        env[key] = value
                        secrets.append(value)
        return env, tuple(secrets)

    def run(self, spec: CommandSpec) -> CommandResult:
        if not isinstance(spec, CommandSpec):
            raise ValueError("COMMAND_SPEC_REQUIRED")
        if spec.executable not in _COMMAND_ALLOWLIST:
            raise ValueError("EXECUTABLE_NOT_ALLOWED")
        if (not isinstance(spec.argv, tuple) or len(spec.argv) > 256
                or any(not isinstance(arg, str) or "\x00" in arg for arg in spec.argv)):
            raise ValueError("ARGUMENT_VECTOR_INVALID")
        if (not isinstance(spec.timeout_seconds, int) or isinstance(spec.timeout_seconds, bool)
                or not 1 <= spec.timeout_seconds <= _COMMAND_TIMEOUT_MAX):
            raise ValueError("TIMEOUT_INVALID")
        executable = self.executable_resolver(spec.executable)
        if not executable:
            return CommandResult(None, "", "", False, "EXECUTABLE_UNAVAILABLE")
        try:
            cwd = Path(spec.cwd).expanduser().resolve(strict=True)
            if not cwd.is_dir():
                raise ValueError("WORKSPACE_CWD_INVALID")
        except (OSError, RuntimeError) as exc:
            raise ValueError("WORKSPACE_CWD_INVALID") from exc
        env, secrets = self._environment(spec.environment_profile)
        try:
            process = self.process_runner(
                (executable, *spec.argv),
                cwd=cwd,
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            return CommandResult(None, "", "", False, "COMMAND_EXECUTION_FAILED")

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}

        def drain(name: str, stream) -> None:
            total = 0
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    remaining = self.max_output_bytes + 1 - len(buffers[name])
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                    if total > self.max_output_bytes:
                        truncated[name] = True
            except (OSError, ValueError):
                truncated[name] = True
                return

        readers = [
            threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        error_code = None
        try:
            returncode = process.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            error_code = "COMMAND_TIMEOUT"
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait()
            except OSError:
                pass
            returncode = None
        except OSError:
            returncode = None
            error_code = "COMMAND_EXECUTION_FAILED"
        for name, reader in zip(("stdout", "stderr"), readers):
            reader.join(timeout=1.0)
            if reader.is_alive():
                truncated[name] = True
                stream = getattr(process, name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        for name in ("stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        stdout = ("[OUTPUT TRUNCATED]" if truncated["stdout"] else
                  _redact(bytes(buffers["stdout"]).decode("utf-8", "replace"), secrets))
        stderr = ("[OUTPUT TRUNCATED]" if truncated["stderr"] else
                  _redact(bytes(buffers["stderr"]).decode("utf-8", "replace"), secrets))
        return CommandResult(returncode, stdout, stderr, timed_out, error_code)


@dataclass(frozen=True)
class ProviderRequest:
    """Provider API operation with no caller-controlled URL or HTTP headers."""

    host_profile: str
    method: str
    path: str
    body: bytes | str | Mapping[str, object] | None = None
    timeout_seconds: int = 20


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    body: str
    content_type: str | None


class ProviderApiTransport:
    """HTTPS client limited to named provider hosts and typed request fields."""

    def __init__(
        self,
        *,
        credential_provider: Callable[[str], str | None] | None = None,
        opener_factory: Callable | None = None,
        max_request_bytes: int = _API_BODY_MAX_BYTES,
        max_response_bytes: int = _API_RESPONSE_MAX_BYTES,
    ) -> None:
        if not 1 <= max_request_bytes <= _API_BODY_MAX_BYTES:
            raise ValueError("REQUEST_LIMIT_INVALID")
        if not 1 <= max_response_bytes <= _API_RESPONSE_MAX_BYTES:
            raise ValueError("RESPONSE_LIMIT_INVALID")
        self.credential_provider = credential_provider
        self.opener_factory = opener_factory
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes

    @staticmethod
    def _body_bytes(body: bytes | str | Mapping[str, object] | None, limit: int) -> bytes | None:
        if body is None:
            return None
        if isinstance(body, Mapping):
            try:
                value = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("REQUEST_BODY_INVALID") from exc
        elif isinstance(body, str):
            value = body.encode("utf-8")
        elif isinstance(body, bytes):
            value = body
        else:
            raise ValueError("REQUEST_BODY_INVALID")
        if len(value) > limit:
            raise ValueError("REQUEST_TOO_LARGE")
        return value

    @staticmethod
    def _validated_path(path: str) -> str:
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or len(path) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or "\\" in path):
            raise ValueError("API_PATH_INVALID")
        parsed = urlsplit(path)
        decoded_segments = unquote(parsed.path).split("/")
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or any(segment in {".", ".."} for segment in decoded_segments)):
            raise ValueError("API_PATH_INVALID")
        return path

    def request(self, spec: ProviderRequest) -> ProviderResponse:
        if not isinstance(spec, ProviderRequest):
            raise ValueError("PROVIDER_REQUEST_REQUIRED")
        host = _API_HOST_PROFILES.get(spec.host_profile)
        if host is None:
            raise ValueError("HOST_PROFILE_NOT_ALLOWED")
        method = spec.method.upper() if isinstance(spec.method, str) else ""
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("HTTP_METHOD_NOT_ALLOWED")
        path = self._validated_path(spec.path)
        if (not isinstance(spec.timeout_seconds, int) or isinstance(spec.timeout_seconds, bool)
                or not 1 <= spec.timeout_seconds <= _API_TIMEOUT_MAX):
            raise ValueError("TIMEOUT_INVALID")
        body = self._body_bytes(spec.body, self.max_request_bytes)
        headers = dict(_API_HEADERS_PROFILE[spec.host_profile])
        token = self.credential_provider(spec.host_profile) if self.credential_provider else None
        secrets: tuple[str, ...] = ()
        if token is not None:
            if not isinstance(token, str) or not token.strip() or any(ord(ch) < 32 for ch in token):
                raise ValueError("CREDENTIAL_PROVIDER_INVALID")
            token = token.strip()
            headers["Authorization"] = f"{_API_AUTH_SCHEME[spec.host_profile]} {token}"
            secrets = (token,)
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"https://{host}{path}", data=body, headers=headers, method=method)
        if self.opener_factory is None:
            from ..web import _safe_opener

            opener = _safe_opener(allowed_hosts=(host,))
        else:
            opener = self.opener_factory(host, allowed_hosts=(host,))
        try:
            response = opener.open(request, timeout=spec.timeout_seconds)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ValueError("RESPONSE_TOO_LARGE")
            content_type = response.headers.get("Content-Type") if response.headers else None
            text = _redact(raw.decode("utf-8", "replace"), secrets)
            status = int(response.status)
        return ProviderResponse(status, text, content_type)


__all__ = [
    "CommandResult", "CommandSpec", "ProviderApiTransport", "ProviderRequest",
    "ProviderResponse", "ReleaseAdapter", "ReleaseCommandRunner", "ReleaseResult",
]
