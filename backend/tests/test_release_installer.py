"""Release-installer tests using a fake xattr executable; no macOS settings are changed."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "packaging" / "install.sh"


class ReleaseInstallerTests(unittest.TestCase):
    def make_release(self, temporary: Path) -> Path:
        release = temporary / "olcr-v0.1.2-macos-arm64"
        release.mkdir()
        shutil.copy2(INSTALLER, release / "install.sh")
        for name in ("app", "frontend", "models", "runtime", "manifest", "licenses"):
            (release / name).mkdir()
        (release / "README.txt").write_text("test release\n")
        return release

    def fake_xattr(self, temporary: Path) -> Path:
        fake_bin = temporary / "bin"
        fake_bin.mkdir(exist_ok=True)
        script = fake_bin / "xattr"
        script.write_text(
            "#!/bin/sh\n"
            "echo \"$@\" >> \"$XATTR_LOG\"\n"
            "case \"$1\" in\n"
            "  -r) [ -f \"$XATTR_STATE\" ] && echo com.apple.quarantine; exit 0;;\n"
            "  -p) exit 1;;\n"
            "  -dr) [ \"${XATTR_FAIL:-0}\" = 1 ] && exit 1; rm -f \"$XATTR_STATE\"; exit 0;;\n"
            "  -d) exit 0;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        return fake_bin

    def run_installer(self, release: Path, temporary: Path, *, fail: bool = False):
        state = temporary / "quarantine-present"
        state.touch()
        log = temporary / "xattr.log"
        app_home = temporary / "app-home"
        workspace = app_home / "workspaces"
        workspace.mkdir(parents=True)
        preserved = workspace / "existing.txt"
        preserved.write_text("preserve me")
        env = os.environ | {
            "PATH": f"{self.fake_xattr(temporary)}:{os.environ['PATH']}",
            "OLCR_APP_SUPPORT": str(app_home),
            "OLCR_BIN_DIR": str(temporary / "olcr-bin"),
            "XATTR_STATE": str(state),
            "XATTR_LOG": str(log),
            "XATTR_FAIL": "1" if fail else "0",
        }
        result = subprocess.run(["sh", "install.sh"], cwd=release, env=env, text=True, capture_output=True)
        return result, log, app_home, preserved

    def test_removes_quarantine_only_from_owned_runtime_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            release = self.make_release(temporary)
            result, log, app_home, preserved = self.run_installer(release, temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unsigned, not-notarized", result.stderr)
            runtime = app_home / "runtime" / "0.1.2" / "runtime"
            self.assertIn(f"-dr com.apple.quarantine {runtime}", log.read_text())
            self.assertNotIn(str(Path.home()), log.read_text())
            self.assertEqual(preserved.read_text(), "preserve me")
            second = subprocess.run(["sh", "install.sh"], cwd=release, env=os.environ | {"PATH": f"{self.fake_xattr(temporary)}:{os.environ['PATH']}", "OLCR_APP_SUPPORT": str(app_home), "OLCR_BIN_DIR": str(temporary / "olcr-bin"), "XATTR_STATE": str(temporary / "quarantine-present"), "XATTR_LOG": str(log)}, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)

    def test_reports_failure_when_runtime_quarantine_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result, _log, _app_home, _preserved = self.run_installer(self.make_release(temporary), temporary, fail=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Failed to remove macOS quarantine from OLCR runtime", result.stderr)
