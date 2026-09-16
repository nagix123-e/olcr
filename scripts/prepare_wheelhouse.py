#!/usr/bin/env python3
"""Prepare and validate the offline wheelhouse used by build_release.py.

The command intentionally downloads wheels only during dependency preparation;
OLCR startup never invokes it.  The default target is the bundled runtime's
CPython 3.10 on macOS Apple Silicon.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
# Current SciPy CPython 3.10 arm64 wheels start at macOS 12.  A 12.0 target
# remains compatible with the older 11.0 wheels used by the other dependencies.
DEFAULT_PLATFORM = "macosx_12_0_arm64"
DEFAULT_PYTHON = "3.10"
DEFAULT_ABI = "cp310"


def _requirements(path: Path = REQUIREMENTS) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            names.add(match.group(1).replace("-", "_").lower())
    return names


def _wheel_name(path: Path) -> str:
    return path.name.lower()


def scipy_wheel_compatible(path: Path, python: str = DEFAULT_PYTHON, platform: str = DEFAULT_PLATFORM, abi: str = DEFAULT_ABI) -> bool:
    """Accept only CPython wheels for the requested macOS arm64 target."""
    name = _wheel_name(path)
    if not name.startswith("scipy-") or not name.endswith(".whl"):
        return False
    py_tag = "cp" + python.replace(".", "")
    if f"-{py_tag}-" not in name:
        return False
    if f"-{abi}-" not in name and f"-abi3-" not in name:
        return False
    target = platform.lower()
    # A universal2 wheel is valid on arm64; x86_64-only and Linux wheels are not.
    return f"-{target}.whl" in name or re.search(r"-macosx_[0-9_]+_(?:arm64|universal2)\.whl$", name) is not None


def _compatible_numpy(path: Path, python: str = DEFAULT_PYTHON, platform: str = DEFAULT_PLATFORM, abi: str = DEFAULT_ABI) -> bool:
    name = _wheel_name(path)
    if not name.startswith("numpy-") or not name.endswith(".whl"):
        return False
    py_tag = "cp" + python.replace(".", "")
    return f"-{py_tag}-" in name and (f"-{abi}-" in name or "-abi3-" in name) and (
        f"-{platform}.whl" in name or re.search(r"-macosx_[0-9_]+_(?:arm64|universal2)\.whl$", name) is not None
    )


def wheelhouse_preflight(wheelhouse: Path, requirements: Path = REQUIREMENTS, python: str = DEFAULT_PYTHON, platform: str = DEFAULT_PLATFORM, abi: str = DEFAULT_ABI) -> dict[str, object]:
    wheels = sorted(wheelhouse.glob("*.whl")) if wheelhouse.is_dir() else []
    scipy = next((wheel for wheel in wheels if scipy_wheel_compatible(wheel, python, platform, abi)), None)
    scipy_any = any(_wheel_name(w).startswith("scipy-") for w in wheels)
    numpy = any(_compatible_numpy(w, python, platform, abi) for w in wheels)
    required = _requirements(requirements)
    return {
        "SCIPY_REQUIREMENT_PRESENT": "PASS" if "scipy" in required else "FAIL",
        "SCIPY_COMPATIBLE_WHEEL_PRESENT": "PASS" if scipy else "FAIL",
        "SCIPY_WHEEL_FILENAME": scipy.name if scipy else "",
        "SCIPY_PLATFORM_COMPATIBLE": "PASS" if scipy else "FAIL",
        "SCIPY_TRANSITIVE_WHEELS_PRESENT": "PASS" if scipy and numpy else "FAIL",
        "SCIPY_WHEELHOUSE_PREFLIGHT": "PASS" if scipy and numpy and "scipy" in required else "FAIL",
        "SCIPY_WHEELHOUSE_REASON": "" if scipy else ("SCIPY_WHEEL_MISSING" if not scipy_any else "SCIPY_WHEEL_INCOMPATIBLE"),
    }


def runtime_import_available(module: str = "scipy", python_executable: str = sys.executable, extra_path: Path | None = None) -> bool:
    """Probe imports in a constructed runtime without changing that runtime."""
    env = os.environ.copy()
    if extra_path:
        env["PYTHONPATH"] = str(extra_path) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    probe = subprocess.run([python_executable, "-c", f"import {module}"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


def prepare(wheelhouse: Path, python_executable: str = sys.executable) -> dict[str, object]:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    command = [python_executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheelhouse), "--platform", DEFAULT_PLATFORM, "--python-version", DEFAULT_PYTHON, "--implementation", "cp", "--abi", DEFAULT_ABI, "-r", str(REQUIREMENTS)]
    subprocess.run(command, check=True)
    result = wheelhouse_preflight(wheelhouse, REQUIREMENTS)
    if result["SCIPY_WHEELHOUSE_PREFLIGHT"] != "PASS":
        raise SystemExit(f"WHEELHOUSE_PREFLIGHT=FAIL reason={result['SCIPY_WHEELHOUSE_REASON']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse", type=Path, help="destination directory for target wheels")
    parser.add_argument("--check-only", action="store_true", help="validate an existing wheelhouse without downloading")
    args = parser.parse_args()
    result = wheelhouse_preflight(args.wheelhouse) if args.check_only else prepare(args.wheelhouse)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0 if result["SCIPY_WHEELHOUSE_PREFLIGHT"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
