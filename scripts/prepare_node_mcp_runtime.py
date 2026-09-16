#!/usr/bin/env python3
"""Assemble the offline Node runtime used only by bundled OLCR MCPs."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "packaging" / "node-mcp" / "artifacts.json"
LOCK = ROOT / "packaging" / "node-mcp" / "package-lock.json"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind = b"L" + path.readlink().as_posix().encode()
        elif path.is_dir():
            kind = b"D"
        else:
            kind = b"F" + digest(path).encode()
        hasher.update(relative + b"\0" + kind + b"\n")
    return hasher.hexdigest()


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents or member.isdev() or member.isfifo():
            raise SystemExit("NODE_RUNTIME_UNSAFE_ARCHIVE")
        if member.issym():
            link = (destination / Path(member.name).parent / member.linkname).resolve()
            if link != root and root not in link.parents:
                raise SystemExit("NODE_RUNTIME_UNSAFE_ARCHIVE")
        if member.islnk():
            link = (destination / member.linkname).resolve()
            if link != root and root not in link.parents:
                raise SystemExit("NODE_RUNTIME_UNSAFE_ARCHIVE")
    archive.extractall(destination)


def verify_lock(lock: Path, expected: dict[str, object]) -> dict[str, object]:
    value = json.loads(lock.read_text(encoding="utf-8"))
    packages = value.get("packages")
    entries = [item for name, item in packages.items() if name.startswith("node_modules/")] if isinstance(packages, dict) else []
    if value.get("lockfileVersion") != expected["lockfile_version"] or len(entries) != expected["package_count"]:
        raise SystemExit("NODE_MCP_LOCK_INVALID")
    if any(not item.get("version") or not item.get("resolved") or not item.get("integrity") for item in entries):
        raise SystemExit("NODE_MCP_LOCK_INTEGRITY_INVALID")
    return value


def write_license_inventory(destination: Path) -> int:
    inventory = [{"component": "Node.js", "license": "MIT", "path": "node/LICENSE"}]
    reference = destination / "servers" / "animejs-reference" / "animejs-v4-reviewed.json"
    if reference.is_file():
        inventory.append({"component": "OLCR Anime.js v4 reviewed reference corpus", "version": "4", "license": "MIT (Anime.js upstream)", "path": str(reference.relative_to(destination)), "source": "https://animejs.com/documentation/"})
    browser_license = next((path for path in (destination / "browser").rglob("LICENSE.headless_shell")), None)
    if browser_license:
        inventory.append({"component": "Chrome Headless Shell", "license": "see bundled notice", "path": str(browser_license.relative_to(destination))})
    for package in sorted((destination / "node_modules").rglob("package.json")):
        if "node_modules" not in package.parts:
            continue
        value = json.loads(package.read_text(encoding="utf-8"))
        inventory.append({"component": value.get("name", str(package.parent)), "version": value.get("version", ""), "license": value.get("license", "see package metadata"), "path": str(package.relative_to(destination))})
    licenses = destination / "licenses"
    licenses.mkdir()
    (licenses / "license-inventory.json").write_text(json.dumps({"runtime": "node-mcp", "components": inventory}, indent=2, sort_keys=True) + "\n")
    return len(inventory)


def assemble(node_archive: Path, package_modules: Path, browser: Path, destination: Path, metadata_path: Path = METADATA, lock: Path = LOCK) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not node_archive.is_file() or digest(node_archive) != metadata["node"]["sha256"]:
        raise SystemExit("NODE_RUNTIME_INPUT_INTEGRITY_FAIL")
    verify_lock(lock, metadata["packages"])
    if not package_modules.is_dir() or not browser.is_dir() or tree_digest(browser) != metadata["playwright_browser"]["tree_sha256"]:
        raise SystemExit("NODE_MCP_INPUT_INVALID")
    if destination.exists():
        raise SystemExit("NODE_MCP_DESTINATION_EXISTS")
    destination.mkdir(parents=True)
    with tarfile.open(node_archive, "r:*") as archive:
        _safe_extract(archive, destination)
    extracted = next((path for path in destination.iterdir() if path.is_dir() and path.name.startswith("node-v")), None)
    if extracted is None:
        raise SystemExit("NODE_RUNTIME_LAYOUT_INVALID")
    node = extracted / "bin" / "node"
    identity = json.loads(subprocess.check_output([str(node), "-e", "console.log(JSON.stringify({version:process.version,platform:process.platform,arch:process.arch}))"], text=True))
    if identity != {"version": "v" + metadata["node"]["version"], "platform": "darwin", "arch": "arm64"}:
        raise SystemExit("NODE_RUNTIME_IDENTITY_INVALID")
    shutil.move(str(extracted), destination / "node")
    shutil.copytree(package_modules, destination / "node_modules", symlinks=True, ignore=shutil.ignore_patterns(".cache", "__pycache__"))
    shutil.copytree(browser, destination / "browser", symlinks=True)
    animejs = ROOT / "packaging" / "node-mcp" / "animejs-reference"
    shutil.copytree(animejs, destination / "servers" / "animejs-reference")
    for relative in (metadata["packages"]["playwright_mcp"]["entrypoint"], metadata["packages"]["shadcn"]["entrypoint"], "servers/animejs-reference/server.js", metadata["playwright_browser"]["path"]):
        if not (destination / relative).is_file():
            raise SystemExit("NODE_MCP_ENTRYPOINT_INVALID")
    shutil.copy2(lock, destination / "package-lock.json")
    count = write_license_inventory(destination)
    entrypoints = {
        "playwright": [metadata["packages"]["playwright_mcp"]["entrypoint"], "--headless", "--isolated", "--executable-path", metadata["playwright_browser"]["path"]],
        "shadcn": [metadata["packages"]["shadcn"]["entrypoint"], "mcp"],
        "animejs": ["servers/animejs-reference/server.js"],
    }
    (destination / "runtime-manifest.json").write_text(json.dumps({"immutable": True, "node": identity, "entrypoints": entrypoints, "package_lock_sha256": digest(lock), "package_count": metadata["packages"]["package_count"], "browser": metadata["playwright_browser"], "license_inventory": "licenses/license-inventory.json", "licensed_component_count": count}, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node_archive", type=Path)
    parser.add_argument("package_modules", type=Path)
    parser.add_argument("browser", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    assemble(arguments.node_archive, arguments.package_modules, arguments.browser, arguments.destination)
    print(arguments.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
