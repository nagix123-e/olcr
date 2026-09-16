#!/usr/bin/env python3
"""Build the separate, offline Serena runtime used by an OLCR release.

This utility is intentionally for release preparation only. OLCR never calls
it at startup and it never selects a host or application Python interpreter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "packaging" / "serena-runtime" / "artifacts.json"
LOCK = ROOT / "packaging" / "serena-runtime" / "requirements-lock.txt"
WHEEL_MANIFEST = ROOT / "packaging" / "serena-runtime" / "wheelhouse-manifest.json"
SERENA_VERSION = "1.7.0"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify(path: Path, expected: str) -> None:
    if not path.is_file() or digest(path) != expected:
        raise SystemExit("SERENA_RUNTIME_INPUT_INTEGRITY_FAIL")


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit("SERENA_STANDALONE_PYTHON_UNSAFE_ARCHIVE")
        if member.isdev() or member.isfifo():
            raise SystemExit("SERENA_STANDALONE_PYTHON_UNSAFE_ARCHIVE")
        if member.issym():
            link_target = (destination / Path(member.name).parent / member.linkname).resolve()
            if link_target != root and root not in link_target.parents:
                raise SystemExit("SERENA_STANDALONE_PYTHON_UNSAFE_ARCHIVE")
        if member.islnk():
            link_target = (destination / member.linkname).resolve()
            if link_target != root and root not in link_target.parents:
                raise SystemExit("SERENA_STANDALONE_PYTHON_UNSAFE_ARCHIVE")
    # Python 3.10 is a supported release-preparation host and predates the
    # tarfile filter argument, so every member is checked above before extract.
    archive.extractall(destination)


def _runtime_python(destination: Path) -> Path:
    candidate = destination / "python" / "bin" / "python3"
    if not candidate.is_file():
        raise SystemExit("SERENA_STANDALONE_PYTHON_LAYOUT_INVALID")
    return candidate


def _python_identity(python: Path) -> dict[str, str]:
    result = subprocess.check_output(
        [str(python), "-c", "import json, platform; print(json.dumps({'version': platform.python_version(), 'system': platform.system(), 'machine': platform.machine()}))"],
        text=True,
        env=_runtime_environment(),
    )
    return json.loads(result)


def _runtime_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Release validation can direct bytecode outside the repository, but that
    # location must never become an installed-distribution record.
    environment.pop("PYTHONPYCACHEPREFIX", None)
    return environment


def _remove_nonportable_console_scripts(python: Path) -> None:
    """Keep only the interpreter launchers used by the bundled runtime."""
    for candidate in python.parent.iterdir():
        if candidate.name not in {"python", "python3", "python3.11"} and (candidate.is_file() or candidate.is_symlink()):
            candidate.unlink()


def _remove_build_bytecode(destination: Path) -> None:
    """Do not ship installer-created caches that add no runtime dependency."""
    for cache in destination.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)


def _metadata_value(metadata: Path, field: str, default: str = "") -> str:
    prefix = f"{field}: "
    return next((line[len(prefix) :] for line in metadata.read_text(errors="replace").splitlines() if line.startswith(prefix)), default)


def write_license_inventory(destination: Path) -> dict[str, object]:
    """Record the license evidence shipped with the separate Serena runtime."""
    site = destination / "python" / "lib" / "python3.11" / "site-packages"
    if not site.is_dir():
        raise SystemExit("SERENA_RUNTIME_SITE_PACKAGES_MISSING")
    license_root = destination / "licenses"
    license_root.mkdir()
    python_license = destination / "python" / "lib" / "python3.11" / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit("SERENA_RUNTIME_PYTHON_LICENSE_MISSING")
    shutil.copy2(python_license, license_root / "CPYTHON_LICENSE.txt")
    records = [
        {
            "component": "CPython (Astral python-build-standalone)",
            "version": "3.11.16",
            "license": "PSF-2.0 and bundled notices",
            "evidence": "runtime LICENSE.txt",
            "path": "licenses/CPYTHON_LICENSE.txt",
        }
    ]
    for distribution in sorted(site.glob("*.dist-info")):
        metadata = distribution / "METADATA"
        if not metadata.is_file():
            continue
        evidence = sorted(
            path
            for path in distribution.rglob("*")
            if path.is_file() and path.name.lower().startswith(("license", "copying", "notice"))
        )
        records.append(
            {
                "component": _metadata_value(metadata, "Name", distribution.name),
                "version": _metadata_value(metadata, "Version"),
                "license": _metadata_value(metadata, "License-Expression", _metadata_value(metadata, "License", "see installed metadata")),
                "evidence": "installed license file" if evidence else "installed distribution metadata",
                "path": str((evidence[0] if evidence else metadata).relative_to(destination)),
            }
        )
    inventory = {"runtime": "serena", "serena_version": SERENA_VERSION, "components": records}
    (license_root / "license-inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return inventory


def verify_wheelhouse(wheelhouse: Path, manifest: Path) -> dict[str, object]:
    """Require an exact, hashed wheelhouse with no unreviewed artifacts."""
    if not manifest.is_file():
        raise SystemExit("SERENA_WHEELHOUSE_MANIFEST_MISSING")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SystemExit("SERENA_WHEELHOUSE_MANIFEST_INVALID")
    expected = {item.get("filename"): item.get("sha256") for item in artifacts}
    actual = {path.name for path in wheelhouse.glob("*.whl")}
    if set(expected) != actual or None in expected:
        raise SystemExit("SERENA_WHEELHOUSE_CONTENTS_INVALID")
    for filename, expected_hash in expected.items():
        if not isinstance(expected_hash, str) or digest(wheelhouse / filename) != expected_hash:
            raise SystemExit("SERENA_WHEELHOUSE_INTEGRITY_FAIL")
    return metadata


def assemble(
    python_archive: Path,
    python_sha256: str,
    wheelhouse: Path,
    lock: Path,
    destination: Path,
    entrypoint: list[str],
    wheel_manifest: Path = WHEEL_MANIFEST,
) -> None:
    """Assemble a new runtime from verified, local-only inputs."""
    verify(python_archive, python_sha256)
    if not wheelhouse.is_dir() or not lock.is_file():
        raise SystemExit("SERENA_WHEELHOUSE_OR_LOCK_MISSING")
    wheelhouse_metadata = verify_wheelhouse(wheelhouse, wheel_manifest)
    if destination.exists():
        raise SystemExit("SERENA_RUNTIME_DESTINATION_EXISTS")

    destination.mkdir(parents=True)
    with tarfile.open(python_archive, "r:*") as archive:
        _safe_extract(archive, destination)
    python = _runtime_python(destination)
    identity = _python_identity(python)
    if not identity["version"].startswith("3.11.") or identity["system"] != "Darwin" or identity["machine"] not in {"arm64", "aarch64"}:
        raise SystemExit("SERENA_STANDALONE_PYTHON_IDENTITY_INVALID")

    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "-r",
            str(lock),
        ],
        check=True,
        env=_runtime_environment(),
    )
    installed = subprocess.check_output(
        [str(python), "-c", "from importlib.metadata import version; print(version('serena-agent'))"], text=True, env=_runtime_environment()
    ).strip()
    if installed != SERENA_VERSION:
        raise SystemExit("SERENA_RUNTIME_VERSION_INVALID")
    _remove_nonportable_console_scripts(python)
    _remove_build_bytecode(destination)
    licenses = write_license_inventory(destination)
    (destination / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "serena_version": SERENA_VERSION,
                "entrypoint": entrypoint,
                "immutable": True,
                "python": identity,
                "lock_sha256": digest(lock),
                "wheelhouse_mode": "offline-required-hashes",
                "wheelhouse_manifest_sha256": digest(wheel_manifest),
                "wheelhouse_artifact_count": len(wheelhouse_metadata["artifacts"]),
                "nonportable_console_scripts_removed": True,
                "license_inventory": "licenses/license-inventory.json",
                "licensed_component_count": len(licenses["components"]),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python_archive", type=Path)
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--metadata", type=Path, default=METADATA)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--wheel-manifest", type=Path, default=WHEEL_MANIFEST)
    arguments = parser.parse_args()
    metadata = json.loads(arguments.metadata.read_text(encoding="utf-8"))
    python = metadata["python"]
    assemble(
        arguments.python_archive,
        python["sha256"],
        arguments.wheelhouse,
        arguments.lock,
        arguments.destination,
        metadata["serena"]["entrypoint"],
        arguments.wheel_manifest,
    )
    print(arguments.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
