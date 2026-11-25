"""Immutable, versioned per-user environments for managed local models."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from typing import Any, Iterator

from .model_runtime_profiles import (
    RuntimeProfile,
    asset_identity_for_entry,
    runtime_profile_for_asset,
    runtime_profiles,
    validate_runtime_lock,
)


SETTINGS_KEY = "model_runtime_bindings"
RUNTIME_METADATA = ".olcr-runtime.json"


@dataclass(frozen=True)
class RuntimeBinding:
    asset_id: str
    profile_id: str
    package_lock_id: str
    runtime_id: str
    python_path: str | None
    runtime_dir: str | None
    managed: bool
    ready: bool
    legacy: bool = False
    error: str | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimePrepareResult:
    status: str
    profile_id: str
    asset_id: str
    binding: RuntimeBinding | None = None
    created: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RuntimeRemovalResult:
    removed: bool
    runtime_id: str
    reason: str | None = None


class RuntimeManager:
    """Stages new environments and never modifies a live or legacy one."""

    def __init__(self, db, support_root: str | Path | None = None,
                 app_root: str | Path | None = None):
        self.db = db
        if support_root is None:
            support_root = os.environ.get("OLCR_APP_SUPPORT") or Path(db.path).expanduser().parent
        self.support_root = Path(support_root).expanduser().resolve()
        self.root = self.support_root / "model-runtimes"
        self.app_root = Path(app_root or Path(__file__).resolve().parents[2]).expanduser().resolve()
        self.legacy_candidates = (
            self.app_root / ".venv-mlx" / "bin" / "python",
            self.support_root / ".venv-mlx" / "bin" / "python",
        )
        self._lock = threading.RLock()
        self._active: Counter[str] = Counter()
        self._verification_cache: dict[tuple[str, str, str], bool] = {}
        self._downloads = None

    def attach_downloads(self, downloads) -> None:
        self._downloads = downloads

    @staticmethod
    def _override_for(profile: RuntimeProfile) -> Path | None:
        names = ("OLCR_AUDIO_MLX_PYTHON", "OLCR_MLX_PYTHON") if profile.loader_id in {
            "mlx-audio", "mlx-whisper"} else ("OLCR_MLX_PYTHON",)
        for name in names:
            value = os.environ.get(name)
            if value:
                return Path(value).expanduser()
        return None

    @staticmethod
    def _external_binding(asset_id: str, profile: RuntimeProfile, python: Path,
                          ready: bool) -> RuntimeBinding:
        return RuntimeBinding(
            asset_id=asset_id,
            profile_id=profile.id,
            package_lock_id=profile.package_lock_id,
            runtime_id="external:" + str(python),
            python_path=str(python),
            runtime_dir=None,
            managed=False,
            ready=ready,
            error=None if ready else "EXTERNAL_RUNTIME_INCOMPATIBLE",
        )

    def _state(self) -> dict[str, Any]:
        try:
            value = self.db.load_settings().get(SETTINGS_KEY, {})
        except (AttributeError, OSError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _asset_bindings(self) -> dict[str, dict[str, Any]]:
        value = self._state().get("assets", {})
        return value if isinstance(value, dict) else {}

    def _save_binding(self, binding: RuntimeBinding) -> None:
        if not binding.ready or not binding.managed:
            raise ValueError("MODEL_RUNTIME_BINDING_NOT_COMMITTABLE")

        def update(current):
            state = current if isinstance(current, dict) else {}
            assets = state.get("assets") if isinstance(state.get("assets"), dict) else {}
            assets = dict(assets)
            record = binding.to_record()
            record["updated_at"] = time.time()
            assets[binding.asset_id] = record
            return {"schema_version": 1, "assets": assets}

        if hasattr(self.db, "update_setting"):
            self.db.update_setting(SETTINGS_KEY, update, time.time())
        else:
            with self._lock:
                state = self._state()
                self.db.save_setting(SETTINGS_KEY, update(state), time.time())

    def commit_binding(self, asset_id: str, binding: RuntimeBinding) -> RuntimeBinding:
        expected = runtime_profile_for_asset(asset_id, None)
        if (binding.asset_id != asset_id or not binding.ready or not binding.managed
                or binding.profile_id != expected.id
                or binding.package_lock_id != expected.package_lock_id):
            raise ValueError("MODEL_RUNTIME_BINDING_MISMATCH")
        if not binding.legacy:
            if not binding.runtime_dir:
                raise ValueError("MODEL_RUNTIME_NOT_VERIFIED")
            self._ensure_runtime_root()
            with self._filesystem_lock():
                runtime_dir = self.root / binding.runtime_id
                expected_python = runtime_dir / "bin" / "python"
                if (Path(binding.runtime_dir).resolve() != runtime_dir.resolve()
                        or Path(binding.python_path or "").resolve() != expected_python.resolve()
                        or runtime_dir.is_symlink()):
                    raise ValueError("MODEL_RUNTIME_NOT_VERIFIED")
                manifest = self._runtime_manifest(runtime_dir)
                if not self._manifest_matches(manifest, binding.profile_id, binding.package_lock_id,
                                              binding.runtime_id):
                    raise ValueError("MODEL_RUNTIME_NOT_VERIFIED")
                if not self._verify_profile(expected_python, expected):
                    raise ValueError("MODEL_RUNTIME_NOT_VERIFIED")
                self._save_binding(binding)
        else:
            allowed = {candidate.resolve() for candidate in self.legacy_candidates}
            try:
                if Path(binding.python_path or "").resolve() not in allowed:
                    raise ValueError("MODEL_RUNTIME_BINDING_MISMATCH")
            except OSError as exc:
                raise ValueError("MODEL_RUNTIME_BINDING_MISMATCH") from exc
            if not self._verify_profile(Path(binding.python_path or ""), expected):
                raise ValueError("MODEL_RUNTIME_NOT_VERIFIED")
            self._save_binding(binding)
        return binding

    def unbind(self, asset_id: str) -> None:
        def update(current):
            state = current if isinstance(current, dict) else {}
            assets = state.get("assets") if isinstance(state.get("assets"), dict) else {}
            assets = dict(assets)
            assets.pop(asset_id, None)
            return {"schema_version": 1, "assets": assets}

        if hasattr(self.db, "update_setting"):
            self.db.update_setting(SETTINGS_KEY, update, time.time())
        else:
            with self._lock:
                self.db.save_setting(SETTINGS_KEY, update(self._state()), time.time())

    def resolve(self, asset_id: str) -> RuntimeBinding:
        profile = runtime_profile_for_asset(asset_id, None)
        if not profile.managed:
            return RuntimeBinding(asset_id, profile.id, profile.package_lock_id,
                                  "external:" + profile.id, None, None, False, True)

        override = self._override_for(profile)
        if override is not None:
            return self._external_binding(asset_id, profile, override,
                                          self._verify_profile(override, profile))

        state = self._asset_bindings().get(asset_id)
        if isinstance(state, dict):
            # Keep an older verified binding usable during a package/profile
            # migration. The artifact remains the same; only an explicitly
            # prepared and validated replacement may supersede this record.
            from .model_runtime_profiles import runtime_profile_by_id
            recorded_profile = runtime_profile_by_id(str(state.get("profile_id") or ""))
            if recorded_profile and state.get("package_lock_id") == recorded_profile.package_lock_id:
                binding = self._binding_from_record(asset_id, recorded_profile, state)
                if binding and binding.ready:
                    return binding

        for python in self.legacy_candidates:
            if python.is_file() and self._verify_profile(python, profile):
                # The legacy runtime is deliberately not persisted as the new
                # model binding until the selected artifact passes its load check.
                return RuntimeBinding(
                    asset_id, profile.id, profile.package_lock_id, "legacy-mlx",
                    str(python), str(python.parent.parent), True, True, legacy=True)

        return RuntimeBinding(asset_id, profile.id, profile.package_lock_id, "",
                              None, None, True, False, error="MODEL_RUNTIME_NOT_READY")

    def prepare(self, profile_id: str, asset_id: str) -> RuntimePrepareResult:
        profile = runtime_profile_for_asset(asset_id, None)
        if profile.id != profile_id:
            return RuntimePrepareResult("failed", profile_id, asset_id,
                                        error="MODEL_RUNTIME_PROFILE_MISMATCH")
        if not profile.managed:
            return RuntimePrepareResult("ready", profile.id, asset_id,
                                        binding=self.resolve(asset_id), created=False)
        if self._override_for(profile) is not None:
            return RuntimePrepareResult("failed", profile.id, asset_id,
                                        error="EXTERNAL_RUNTIME_CANNOT_BE_MODIFIED")
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            return RuntimePrepareResult("failed", profile.id, asset_id,
                                        error="MODEL_RUNTIME_PLATFORM_UNSUPPORTED")
        try:
            validate_runtime_lock()
        except RuntimeError as exc:
            return RuntimePrepareResult("failed", profile.id, asset_id, error=str(exc))

        current = self.resolve(asset_id)
        if current.ready and current.managed and not current.legacy:
            return RuntimePrepareResult("ready", profile.id, asset_id,
                                        binding=current, created=False)

        try:
            self._ensure_runtime_root()
            with self._filesystem_lock():
                abi = self._python_abi()
                existing = self._find_ready_profile(profile, abi, asset_id)
                if existing is not None:
                    return RuntimePrepareResult("ready", profile.id, asset_id,
                                                binding=existing, created=False)
                return self._build_profile(profile, asset_id, abi)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
            return RuntimePrepareResult("failed", profile.id, asset_id,
                                        error="MODEL_RUNTIME_PREPARATION_FAILED")

    def _build_profile(self, profile: RuntimeProfile, asset_id: str,
                       python_abi: str) -> RuntimePrepareResult:
        base_python = Path(sys.executable).resolve()
        if not base_python.is_file():
            raise RuntimeError("MODEL_RUNTIME_BASE_PYTHON_UNAVAILABLE")
        stage = self.root / (".staging-" + re.sub(r"[^A-Za-z0-9._-]", "-", profile.id)
                             + "-" + uuid.uuid4().hex)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONHOME", "PYTHONPATH"}}
        environment["PIP_NO_INPUT"] = "1"
        try:
            created = subprocess.run(
                [str(base_python), "-m", "venv", "--copies", str(stage)],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=180, check=False)
            if created.returncode:
                raise RuntimeError("MODEL_RUNTIME_VENV_FAILED")
            python = stage / "bin" / "python"
            installed = subprocess.run(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                 "--prefer-binary", *profile.packages],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=1800, check=False)
            if installed.returncode or not self._verify_profile(python, profile, force=True):
                raise RuntimeError("MODEL_RUNTIME_INSTALL_FAILED")
            frozen = subprocess.run(
                [str(python), "-m", "pip", "freeze", "--all"],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", timeout=120, check=False)
            if frozen.returncode:
                raise RuntimeError("MODEL_RUNTIME_LOCK_CAPTURE_FAILED")
            package_lines = sorted(line.strip() for line in frozen.stdout.splitlines() if line.strip())
            if not package_lines:
                raise RuntimeError("MODEL_RUNTIME_LOCK_CAPTURE_FAILED")
            resolved_material = "\n".join((profile.package_lock_id, python_abi,
                                             platform.platform(), *package_lines)) + "\n"
            resolved_lock_id = "sha256:" + hashlib.sha256(resolved_material.encode("utf-8")).hexdigest()
            runtime_id = f"{profile.id}-{python_abi}-{resolved_lock_id.removeprefix('sha256:')[:20]}"
            manifest = {
                "schema_version": 1,
                "runtime_id": runtime_id,
                "profile_id": profile.id,
                "package_lock_id": profile.package_lock_id,
                "resolved_lock_id": resolved_lock_id,
                "python_abi": python_abi,
                "platform": platform.platform(),
                "base_python_version": platform.python_version(),
                "packages": package_lines,
                "created_at": time.time(),
                "managed": True,
            }
            self._write_manifest(stage, manifest)
            target = self.root / runtime_id
            if target.exists():
                existing_manifest = self._runtime_manifest(target)
                if not self._manifest_matches(existing_manifest, profile.id,
                                              profile.package_lock_id, runtime_id):
                    raise RuntimeError("MODEL_RUNTIME_TARGET_COLLISION")
                shutil.rmtree(stage)
            else:
                os.rename(stage, target)
            binding = RuntimeBinding(
                asset_id=asset_id,
                profile_id=profile.id,
                package_lock_id=profile.package_lock_id,
                runtime_id=runtime_id,
                python_path=str(target / "bin" / "python"),
                runtime_dir=str(target),
                managed=True,
                ready=True,
            )
            return RuntimePrepareResult("ready", profile.id, asset_id,
                                        binding=binding, created=True)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def _find_ready_profile(self, profile: RuntimeProfile, python_abi: str,
                            asset_id: str) -> RuntimeBinding | None:
        try:
            candidates = tuple(self.root.iterdir())
        except OSError:
            return None
        for candidate in candidates:
            if not candidate.is_dir() or candidate.is_symlink() or candidate.name.startswith("."):
                continue
            manifest = self._runtime_manifest(candidate)
            runtime_id = str(manifest.get("runtime_id") or "") if manifest else ""
            if (not self._manifest_matches(manifest, profile.id, profile.package_lock_id, runtime_id)
                    or manifest.get("python_abi") != python_abi):
                continue
            python = candidate / "bin" / "python"
            if python.is_file() and self._verify_profile(python, profile):
                return RuntimeBinding(asset_id, profile.id, profile.package_lock_id, runtime_id,
                                      str(python), str(candidate), True, True)
        return None

    def _binding_from_record(self, asset_id: str, profile: RuntimeProfile,
                             record: dict[str, Any]) -> RuntimeBinding | None:
        if record.get("legacy"):
            python = Path(str(record.get("python_path") or ""))
            allowed = {candidate.resolve() for candidate in self.legacy_candidates}
            try:
                if python.resolve() not in allowed:
                    return None
            except OSError:
                return None
            if python.is_file() and self._verify_profile(python, profile):
                return RuntimeBinding(asset_id, profile.id, profile.package_lock_id,
                                      "legacy-mlx", str(python), record.get("runtime_dir"),
                                      True, True, legacy=True)
            return None
        runtime_id = str(record.get("runtime_id") or "")
        if (Path(runtime_id).name != runtime_id
                or not re.fullmatch(r"[A-Za-z0-9._-]+", runtime_id)):
            return None
        runtime_dir = self.root / runtime_id
        try:
            runtime_dir.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError):
            return None
        if runtime_dir.is_symlink() or not runtime_dir.is_dir():
            return None
        manifest = self._runtime_manifest(runtime_dir)
        if not self._manifest_matches(manifest, profile.id, profile.package_lock_id, runtime_id):
            return None
        python = runtime_dir / "bin" / "python"
        if not python.is_file() or not self._verify_profile(python, profile):
            return None
        return RuntimeBinding(asset_id, profile.id, profile.package_lock_id,
                              runtime_id, str(python), str(runtime_dir), True, True)

    @staticmethod
    def _runtime_manifest(runtime_dir: Path) -> dict[str, Any]:
        try:
            value = json.loads((runtime_dir / RUNTIME_METADATA).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _manifest_matches(value: dict[str, Any], profile_id: str,
                          package_lock_id: str, runtime_id: str) -> bool:
        if (not value or value.get("schema_version") != 1
                or value.get("managed") is not True
                or value.get("runtime_id") != runtime_id
                or value.get("profile_id") != profile_id
                or value.get("package_lock_id") != package_lock_id):
            return False
        python_abi = value.get("python_abi")
        platform_tag = value.get("platform")
        packages = value.get("packages")
        if (not isinstance(python_abi, str) or not python_abi
                or not isinstance(platform_tag, str) or not platform_tag
                or not isinstance(packages, list)
                or any(not isinstance(package, str) or not package for package in packages)):
            return False
        material = "\n".join((package_lock_id, python_abi, platform_tag,
                                 *sorted(packages))) + "\n"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return (value.get("resolved_lock_id") == "sha256:" + digest
                and runtime_id == f"{profile_id}-{python_abi}-{digest[:20]}")

    @staticmethod
    def _python_abi() -> str:
        material = "-".join(filter(None, (
            getattr(sys.implementation, "cache_tag", "python"),
            str(sysconfig.get_config_var("SOABI") or ""),
        )))
        return re.sub(r"[^A-Za-z0-9._-]", "-", material)[:80] or "python-unknown"

    def _ensure_runtime_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.resolve().relative_to(self.support_root.resolve())
        except (OSError, ValueError) as exc:
            raise ValueError("MODEL_RUNTIME_ROOT_INVALID") from exc

    @staticmethod
    def _write_manifest(runtime_dir: Path, value: dict[str, Any]) -> None:
        temporary_name = None
        target = runtime_dir / RUNTIME_METADATA
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=runtime_dir,
                                             prefix=RUNTIME_METADATA + ".", suffix=".tmp",
                                             delete=False) as handle:
                temporary_name = handle.name
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _verify_profile(self, python: Path, profile: RuntimeProfile,
                        force: bool = False) -> bool:
        python = Path(python).expanduser()
        if not python.is_file():
            return False
        try:
            cache_key = (str(python.resolve()), profile.id, profile.package_lock_id)
            stamp = str(python.stat().st_mtime_ns)
        except OSError:
            return False
        if not force and cache_key + (stamp,) in self._verification_cache:
            return self._verification_cache[cache_key + (stamp,)]
        expected: dict[str, str] = {}
        for spec in profile.packages:
            if "==" not in spec:
                return False
            name, version = spec.rsplit("==", 1)
            expected[name] = version
        check = (
            "import importlib, importlib.metadata, json, pathlib, sys\n"
            "expected=json.loads(sys.argv[1]); imports=json.loads(sys.argv[2])\n"
            "assert all(importlib.metadata.version(name)==version for name,version in expected.items())\n"
            "[importlib.import_module(name) for name in imports]\n"
        )
        try:
            result = subprocess.run(
                [str(python), "-c", check, json.dumps(expected), json.dumps(profile.verify_imports)],
                env={key: value for key, value in os.environ.items()
                     if key not in {"PYTHONHOME", "PYTHONPATH"}},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=40, check=False)
            valid = result.returncode == 0
            if valid and profile.loader_id == "plamo-translate-cli":
                valid = (Path(python).parent / "plamo-translate").is_file()
        except (OSError, subprocess.SubprocessError):
            valid = False
        self._verification_cache[cache_key + (stamp,)] = valid
        return valid

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        lock_path = self.root / ".runtime-manager.lock"
        handle = lock_path.open("a+b")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            handle.close()

    def lease_worker(self, binding: RuntimeBinding, worker_pid: int | None = None) -> str | None:
        """Keep a runtime alive while a child worker still uses its interpreter."""
        if not binding.managed or not binding.runtime_dir:
            return None
        pid = int(worker_pid or os.getpid())
        self._ensure_runtime_root()
        with self._filesystem_lock():
            if binding.legacy:
                allowed = {candidate.resolve() for candidate in self.legacy_candidates}
                try:
                    if Path(binding.python_path or "").resolve() not in allowed:
                        return None
                except OSError:
                    return None
                lease_dir = self.root / ".legacy-mlx-leases"
            else:
                manifest = self._runtime_manifest(Path(binding.runtime_dir))
                if not self._manifest_matches(manifest, binding.profile_id,
                                              binding.package_lock_id, binding.runtime_id):
                    return None
                lease_dir = Path(binding.runtime_dir) / ".leases"
            if lease_dir.is_symlink():
                return None
            lease_dir.mkdir(mode=0o700, exist_ok=True)
            token = uuid.uuid4().hex
            lease_path = lease_dir / f"{pid}-{token}.json"
            lease_path.write_text(json.dumps({"pid": pid, "runtime_id": binding.runtime_id,
                                              "created_at": time.time()}), encoding="utf-8")
            with self._lock:
                self._active[binding.runtime_id] += 1
        return str(lease_path)

    def release_worker(self, lease_token: str | None) -> None:
        if not lease_token:
            return
        path = Path(lease_token)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            runtime_id = str(record.get("runtime_id") or "")
        except (OSError, ValueError, AttributeError):
            runtime_id = ""
        try:
            self._ensure_runtime_root()
            with self._filesystem_lock():
                path.resolve().relative_to(self.root.resolve())
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            return
        with self._lock:
            if runtime_id:
                self._active[runtime_id] = max(0, self._active[runtime_id] - 1)
        # A prior legacy worker may have been the last reason to retain the
        # shared environment. Retry cleanup as soon as that worker has exited.
        if runtime_id == "legacy-mlx" and self._downloads is not None:
            self.cleanup_legacy_if_unused(self._downloads)

    @contextmanager
    def worker_lease(self, binding: RuntimeBinding,
                     worker_pid: int | None = None) -> Iterator[None]:
        token = self.lease_worker(binding, worker_pid)
        try:
            yield
        finally:
            self.release_worker(token)

    def _active_worker_count(self, runtime_id: str, runtime_dir: Path) -> int:
        with self._lock:
            count = self._active.get(runtime_id, 0)
        lease_dir = runtime_dir / ".leases"
        try:
            leases = tuple(lease_dir.glob("*.json"))
        except OSError:
            return count
        for lease in leases:
            try:
                record = json.loads(lease.read_text(encoding="utf-8"))
                pid = int(record.get("pid") or 0)
                os.kill(pid, 0)
                count += 1
            except ProcessLookupError:
                lease.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, AttributeError):
                # Invalid leases fail safe and keep the environment until
                # settings cleanup or a later explicit repair can inspect it.
                count += 1
        # Leases are owned by the backend process. If it exited unexpectedly,
        # an orphaned model worker may outlive its stale lease; check the
        # executable path as a second guard before allowing runtime deletion.
        try:
            process_list = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                          text=True, timeout=8, check=False)
            if process_list.returncode != 0:
                return count + 1
            python = runtime_dir / "bin" / "python"
            markers = {str(python), str(python.resolve()), str(runtime_dir.resolve())}
            if any(marker in process_list.stdout for marker in markers):
                count += 1
        except (OSError, subprocess.SubprocessError):
            return count + 1
        return count

    def list_profiles(self) -> list[dict[str, Any]]:
        assets = self._asset_bindings()
        references: dict[str, list[str]] = {}
        for asset_id, row in assets.items():
            if isinstance(row, dict) and row.get("runtime_id"):
                references.setdefault(str(row["runtime_id"]), []).append(str(asset_id))
        try:
            candidates = tuple(self.root.iterdir())
        except OSError:
            return []
        result = []
        for directory in candidates:
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            manifest = self._runtime_manifest(directory)
            runtime_id = str(manifest.get("runtime_id") or "")
            if not runtime_id:
                continue
            result.append({
                "runtime_id": runtime_id,
                "profile_id": manifest.get("profile_id"),
                "package_lock_id": manifest.get("package_lock_id"),
                "resolved_lock_id": manifest.get("resolved_lock_id"),
                "python_abi": manifest.get("python_abi"),
                "created_at": manifest.get("created_at"),
                "managed": manifest.get("managed") is True,
                "referenced_assets": sorted(references.get(runtime_id, [])),
                "active_workers": self._active_worker_count(runtime_id, directory),
                "removable": not references.get(runtime_id)
                and self._active_worker_count(runtime_id, directory) == 0,
            })
        return result

    def remove(self, profile_id: str) -> RuntimeRemovalResult:
        runtime_id = str(profile_id or "")
        if (not runtime_id or Path(runtime_id).name != runtime_id
                or runtime_id.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._-]+", runtime_id)):
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_NOT_FOUND")
        try:
            self._ensure_runtime_root()
        except (OSError, ValueError):
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_ROOT_INVALID")
        with self._filesystem_lock():
            return self._remove_locked(runtime_id)

    def _remove_locked(self, runtime_id: str) -> RuntimeRemovalResult:
        directory = self.root / runtime_id
        if directory.is_symlink():
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_NOT_MANAGED")
        manifest = self._runtime_manifest(directory)
        if not self._manifest_matches(manifest, str(manifest.get("profile_id") or ""),
                                      str(manifest.get("package_lock_id") or ""), runtime_id):
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_NOT_MANAGED")
        references = [asset_id for asset_id, row in self._asset_bindings().items()
                      if isinstance(row, dict) and row.get("runtime_id") == runtime_id]
        if references:
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_REFERENCED")
        if self._active_worker_count(runtime_id, directory):
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_WORKER_ACTIVE")
        try:
            directory.resolve().relative_to(self.root.resolve())
            shutil.rmtree(directory)
        except (OSError, ValueError):
            return RuntimeRemovalResult(False, runtime_id, "RUNTIME_REMOVE_FAILED")
        return RuntimeRemovalResult(True, runtime_id)

    def cleanup_unreferenced(self) -> list[str]:
        removed = []
        for row in self.list_profiles():
            if row.get("removable"):
                result = self.remove(str(row["runtime_id"]))
                if result.removed:
                    removed.append(result.runtime_id)
        return removed

    def cleanup_legacy_if_unused(self, downloads) -> list[str]:
        """Remove the old shared MLX environment only after all local assets migrate."""
        if not any(candidate.is_file() for candidate in self.legacy_candidates):
            return []
        overrides = set()
        for profile in runtime_profiles():
            override = self._override_for(profile)
            if override is not None:
                try:
                    overrides.add(override.resolve())
                except OSError:
                    pass
        if any(candidate.resolve() in overrides for candidate in self.legacy_candidates if candidate.exists()):
            return []
        if any(isinstance(row, dict) and row.get('legacy')
               for row in self._asset_bindings().values()):
            return []
        for entry in downloads.presets().values():
            try:
                identity = asset_identity_for_entry(entry)
                profile = runtime_profile_for_asset(identity.asset_id, identity.revision)
                if (identity.asset_id.startswith('transcription_')
                        and os.environ.get('OLCR_AUDIO_ENGINE', 'auto').lower() == 'torch'):
                    continue
                if not profile.managed or not downloads.downloaded(entry):
                    continue
                binding = self.resolve(identity.asset_id)
                if not binding.ready or binding.legacy:
                    return []
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                return []
        # Hold the same lock used by lease acquisition through deletion. A
        # worker must not acquire a legacy lease after the idle check but
        # before the environment is removed.
        try:
            self._ensure_runtime_root()
            with self._filesystem_lock():
                lease_dir = self.root / ".legacy-mlx-leases"
                for lease in lease_dir.glob("*.json"):
                    try:
                        record = json.loads(lease.read_text(encoding="utf-8"))
                        pid = int(record.get("pid") or 0)
                        os.kill(pid, 0)
                        return []
                    except ProcessLookupError:
                        lease.unlink(missing_ok=True)
                    except (OSError, ValueError, TypeError, AttributeError):
                        return []
                # Also wait for processes started outside this manager. On
                # macOS their command line normally includes the interpreter.
                process_list = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                              text=True, timeout=8, check=False)
                if process_list.returncode != 0:
                    return []
                commands = process_list.stdout
                if any(candidate.is_file() and any(
                        marker in commands for marker in {
                            str(candidate), str(candidate.resolve()),
                            str(candidate.parent.parent.resolve()),
                        }) for candidate in self.legacy_candidates):
                    return []

                removed = []
                for candidate in self.legacy_candidates:
                    environment = candidate.parent.parent
                    if not candidate.is_file() or environment.is_symlink():
                        continue
                    try:
                        resolved = environment.resolve()
                        allowed_root = (self.app_root if self.app_root == resolved
                                        or self.app_root in resolved.parents else self.support_root)
                        resolved.relative_to(allowed_root)
                        shutil.rmtree(environment)
                        removed.append(str(environment))
                    except (OSError, ValueError):
                        continue
                return removed
        except (OSError, subprocess.SubprocessError, ValueError):
            return []


_runtime_manager: RuntimeManager | None = None


def set_runtime_manager(manager: RuntimeManager | None) -> None:
    global _runtime_manager
    _runtime_manager = manager


def get_runtime_manager() -> RuntimeManager | None:
    return _runtime_manager
