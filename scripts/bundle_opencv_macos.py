#!/usr/bin/env python3
"""Embed the Homebrew dylib closure of an opt-in OpenCV OLCR app bundle.

This intentionally operates only on an explicit .app path. It resolves the
Mach-O dependency closure, copies non-system Homebrew libraries into the app,
rewrites load commands to @rpath, and signs the result with the configured
local identity. It never launches the app or changes an installed OLCR bundle.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys


SYSTEM_PREFIXES = ("/System/", "/usr/lib/")
DEFAULT_SIGNING_IDENTITY = "OLCR Local Debug Code Signing"


class BundleError(RuntimeError):
    pass


def run(*args: str, capture: bool = True) -> str:
    try:
        result = subprocess.run(args, check=True, text=True, capture_output=capture)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise BundleError(f"command failed: {args[0]} ({detail.strip()})") from exc
    return result.stdout if capture else ""


@lru_cache(maxsize=None)
def linked_libraries(path: pathlib.Path) -> list[str]:
    output = run("otool", "-L", str(path))
    dependencies = []
    for line in output.splitlines()[1:]:
        if not line[:1].isspace() or line.strip().endswith(":"):
            continue
        value = line.strip().split(" (compatibility", 1)[0]
        if value:
            dependencies.append(value)
    return dependencies


@lru_cache(maxsize=None)
def rpaths(path: pathlib.Path) -> list[str]:
    lines = run("otool", "-l", str(path)).splitlines()
    values: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for item in lines[index + 1 : index + 5]:
            match = re.match(r"\s*path (.+?) \(offset \d+\)", item)
            if match:
                values.append(match.group(1))
                break
    return values


@lru_cache(maxsize=None)
def weak_libraries(path: pathlib.Path) -> set[str]:
    lines = run("otool", "-l", str(path)).splitlines()
    values: set[str] = set()
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_LOAD_WEAK_DYLIB":
            continue
        for item in lines[index + 1 : index + 5]:
            match = re.match(r"\s*name (.+?) \(offset \d+\)", item)
            if match:
                values.add(match.group(1))
                break
    return values


def expand_path(value: str, loader: pathlib.Path, executable: pathlib.Path) -> pathlib.Path:
    value = value.replace("@loader_path", str(loader.parent))
    value = value.replace("@executable_path", str(executable.parent))
    return pathlib.Path(value)


@lru_cache(maxsize=1)
def homebrew_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    for prefix in ("/opt/homebrew", "/usr/local"):
        path = pathlib.Path(prefix)
        if path.exists():
            roots.append(path)
    return roots


def resolve_dependency(
    dependency: str,
    loader: pathlib.Path,
    executable: pathlib.Path,
    brew_roots: list[pathlib.Path],
) -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    if dependency.startswith(SYSTEM_PREFIXES):
        # macOS Swift runtime libraries can live only in the dyld shared cache,
        # so filesystem existence is not a reliable test for /usr/lib paths.
        return None
    elif dependency.startswith("@loader_path/"):
        candidates.append(expand_path(dependency, loader, executable))
    elif dependency.startswith("@executable_path/"):
        candidates.append(expand_path(dependency, loader, executable))
    elif dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        if pathlib.Path(suffix).name.startswith("libswift"):
            return None
        for entry in [*rpaths(loader), *rpaths(executable)]:
            candidates.append(expand_path(entry, loader, executable) / suffix)
        candidates.extend(root / "opt/opencv/lib" / suffix for root in brew_roots)
        candidates.extend(root / "lib" / suffix for root in brew_roots)
    elif dependency.startswith("/"):
        candidates.append(pathlib.Path(dependency))
    else:
        raise BundleError(f"unsupported Mach-O dependency path: {dependency}")

    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            if not any(str(resolved).startswith(str(root) + "/") for root in brew_roots):
                raise BundleError(f"refusing to bundle a non-system dependency outside Homebrew: {resolved}")
            return resolved
    raise BundleError(f"cannot resolve dependency {dependency!r} used by {loader}")


def dependency_closure(executable: pathlib.Path) -> tuple[dict[pathlib.Path, list[str]], set[pathlib.Path]]:
    brew_roots = homebrew_roots()
    graph: dict[pathlib.Path, list[str]] = {}
    queued = [executable]
    bundled: set[pathlib.Path] = set()
    while queued:
        owner = queued.pop(0)
        if owner in graph:
            continue
        dependencies = linked_libraries(owner)
        graph[owner] = dependencies
        for dependency in dependencies:
            try:
                resolved = resolve_dependency(dependency, owner, executable, brew_roots)
            except BundleError:
                if dependency in weak_libraries(owner):
                    continue
                raise
            if resolved is None:
                continue
            if resolved != executable:
                bundled.add(resolved)
            if resolved not in graph:
                queued.append(resolved)
    return graph, bundled


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def architectures(path: pathlib.Path) -> set[str]:
    output = run("lipo", "-archs", str(path)).strip()
    return set(output.split())


def ensure_identity(identity: str) -> None:
    output = run("security", "find-identity", "-v", "-p", "codesigning")
    if not any(re.search(rf'"{re.escape(identity)}"$', line) for line in output.splitlines()):
        raise BundleError(f"approved signing identity is not available: {identity}")


def package(app: pathlib.Path, identity: str) -> tuple[int, int]:
    if sys.platform != "darwin":
        raise BundleError("OpenCV app bundling is supported only on macOS")
    app = app.expanduser().resolve()
    if app.suffix != ".app" or not app.is_dir():
        raise BundleError(f"expected an existing .app bundle: {app}")
    contents = app / "Contents"
    info_path = contents / "Info.plist"
    if not info_path.is_file():
        raise BundleError(f"missing app Info.plist: {info_path}")
    info = plistlib.loads(info_path.read_bytes())
    if info.get("CFBundleIdentifier") != "local.olcr.desktop":
        raise BundleError("refusing to modify a bundle other than local.olcr.desktop")
    executable = contents / "MacOS" / str(info.get("CFBundleExecutable", ""))
    if not executable.is_file():
        raise BundleError(f"missing app executable: {executable}")

    graph, bundled = dependency_closure(executable)
    if not any("opencv" in str(path).casefold() for path in bundled):
        raise BundleError("the app executable has no Homebrew OpenCV dependency; build with opencv-perception")

    framework_dir = contents / "Frameworks"
    framework_dir.mkdir(parents=True, exist_ok=True)
    resources_dir = contents / "Resources"
    resources_dir.mkdir(parents=True, exist_ok=True)
    targets: dict[pathlib.Path, pathlib.Path] = {}
    for source in sorted(bundled):
        target = framework_dir / source.name
        if target.exists() and file_hash(target) != file_hash(source):
            raise BundleError(f"bundle contains a conflicting dependency: {target}")
        if not target.exists():
            shutil.copy2(source, target)
        if not architectures(target).issuperset(architectures(executable)):
            raise BundleError(f"dependency architecture does not cover the app executable: {target}")
        targets[source] = target

    for owner, dependencies in graph.items():
        packaged_owner = executable if owner == executable else targets[owner]
        for dependency in dependencies:
            try:
                resolved = resolve_dependency(dependency, owner, executable, homebrew_roots())
            except BundleError:
                if dependency in weak_libraries(owner):
                    continue
                raise
            if resolved is None:
                continue
            target = targets.get(resolved)
            if target is None:
                continue
            replacement = f"@rpath/{target.name}"
            if dependency != replacement:
                run("install_name_tool", "-change", dependency, replacement, str(packaged_owner))
        if owner != executable:
            run("install_name_tool", "-id", f"@rpath/{packaged_owner.name}", str(packaged_owner))

    packaged_objects = [executable, *sorted(framework_dir.glob("*.dylib"))]
    for obj in packaged_objects:
        for entry in rpaths(obj):
            if any(entry.startswith(str(root) + "/") for root in homebrew_roots()):
                run("install_name_tool", "-delete_rpath", entry, str(obj))

    executable_rpath = "@executable_path/../Frameworks"
    for required_rpath in (executable_rpath, "/usr/lib/swift"):
        if required_rpath not in rpaths(executable):
            run("install_name_tool", "-add_rpath", required_rpath, str(executable))

    # install_name_tool changes the cached otool metadata on disk.
    linked_libraries.cache_clear()
    weak_libraries.cache_clear()
    rpaths.cache_clear()

    manifest = resources_dir / "opencv-native-dependencies.txt"
    manifest.write_text(
        "Bundled Mach-O dependencies for OLCR's optional opencv-perception feature.\n"
        "Apple Swift runtime resolution: macOS /usr/lib/swift dyld shared cache.\n"
        f"Homebrew dylibs: {len(bundled)}\n"
        + "\n".join(f"{path.name} sha256={file_hash(path)}" for path in sorted(bundled))
        + "\n",
        encoding="utf-8",
    )

    # Sign nested code first and the outer application last; never use ad-hoc signing.
    ensure_identity(identity)
    for library in sorted(framework_dir.glob("*.dylib")):
        run("codesign", "--force", "--sign", identity, "--timestamp=none", str(library))
    entitlements=pathlib.Path(__file__).resolve().parents[1] / "desktop/src-tauri/Entitlements.plist"
    run("codesign", "--force", "--sign", identity, "--timestamp=none", "--entitlements", str(entitlements), str(executable))
    run("codesign", "--force", "--sign", identity, "--timestamp=none", "--entitlements", str(entitlements), str(app))

    # Verify the dependency closure no longer points at a machine-local Homebrew path.
    packaged_objects = [executable, *sorted(framework_dir.glob("*.dylib"))]
    for obj in packaged_objects:
        weak = weak_libraries(obj)
        if any(any(entry.startswith(str(root) + "/") for root in homebrew_roots()) for entry in rpaths(obj)):
            raise BundleError(f"machine-local Homebrew runpath remains after bundling: {obj}")
        for dependency in linked_libraries(obj):
            if dependency.startswith(("/opt/homebrew/", "/usr/local/")):
                raise BundleError(f"machine-local dependency remains after bundling: {obj} -> {dependency}")
            if dependency.startswith("@rpath/"):
                if not (framework_dir / pathlib.Path(dependency).name).is_file():
                    if pathlib.Path(dependency).name.startswith("libswift"):
                        continue
                    if dependency in weak:
                        continue
                    raise BundleError(f"bundled @rpath dependency is missing: {obj} -> {dependency}")
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
    return len(bundled), sum(path.stat().st_size for path in bundled)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=pathlib.Path, help="explicit OLCR .app bundle to modify")
    parser.add_argument(
        "--signing-identity",
        default=os.environ.get("OLCR_CUA_SIGNING_IDENTITY", DEFAULT_SIGNING_IDENTITY),
    )
    args = parser.parse_args()
    try:
        count, size = package(args.app, args.signing_identity)
    except BundleError as exc:
        print(f"OpenCV bundle failed: {exc}", file=sys.stderr)
        return 1
    print(f"OpenCV bundle ready: {args.app.resolve()}")
    print(f"Bundled {count} Homebrew dylibs ({size / (1024 * 1024):.1f} MiB); signed with {args.signing_identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
