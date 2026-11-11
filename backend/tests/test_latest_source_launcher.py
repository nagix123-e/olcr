"""Exercise the normal-use launcher without building or opening a real app."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("zsh"), "normal-use launcher requires zsh")
class LatestSourceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="olcr launcher test ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events = self.root / "events.jsonl"
        self.state = self.root / "process-state"
        self.state.write_text("absent")
        self.bin = self.root / "desktop/src-tauri/target/debug/bundle/macos/OLCR.app/Contents/MacOS/olcr-desktop"
        self.mock_bin = self.root / "mock-bin"
        self.mock_bin.mkdir()
        self.write("frontend/src/main.tsx", "old ui\n")
        self.write("backend/olcr_api/app.py", "old api\n")
        self.write("desktop/src-tauri/src/main.rs", "old native\n")
        self.write("desktop/src-tauri/Cargo.toml", "old config\n")
        self.write("frontend/package-lock.json", '{"lock":"old"}\n')
        self.write("test-cargo-env", "# The fixture does not change the user's Cargo installation.\n")
        runtime = self.root / ".venv312/bin/python"
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(sys.executable)
        source = (ROOT / "scripts/start-dev-independent.sh").read_text()
        # Replace only OS/build boundaries in the isolated copy. The launcher
        # control flow and its content-fingerprint implementation remain real.
        for command in ("/usr/bin/security", "/usr/bin/codesign", "/usr/libexec/PlistBuddy"):
            source = source.replace(command, '"' + str(self.mock_bin / Path(command).name) + '"')
        source = source.replace('source "$HOME/.cargo/env"', 'source "$ROOT/test-cargo-env"')
        self.launcher = self.write("scripts/start-dev-independent.sh", source)
        fake = "#!/usr/bin/env python3\n" + '''
from pathlib import Path
import json, os, plistlib, sys
root = Path(os.environ["OLCR_TEST_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state = (root / "process-state").read_text()
app_bin = root / "desktop/src-tauri/target/debug/bundle/macos/OLCR.app/Contents/MacOS/olcr-desktop"
with (root / "events.jsonl").open("a") as output:
    output.write(json.dumps([name, args]) + "\\n")
if name == "ps":
    if state == "running": print(app_bin)
elif name == "security":
    print('  1) ABCDEF "OLCR Local Debug Code Signing"')
elif name == "pgrep":
    if state == "running": print("4242")
    else: sys.exit(1)
elif name == "launchctl":
    if args == ["list"]:
        if state != "absent": print("4242 0 local.olcr.dev-independent")
    elif args[0] == "bootout":
        (root / "process-state").write_text("absent")
    elif args[0] == "bootstrap":
        with open(args[2], "rb") as source: value = plistlib.load(source)
        (root / "launched.json").write_text(json.dumps(value))
    else: sys.exit(91)
elif name == "tauri":
    if os.environ.get("OLCR_TEST_BUILD_FAIL"): sys.exit(42)
    app_bin.parent.mkdir(parents=True, exist_ok=True)
    app_bin.write_text("fixture app")
    app_bin.chmod(0o755)
    if os.environ.get("OLCR_TEST_EDIT_DURING_BUILD"):
        (root / "frontend/src/main.tsx").write_text("changed while building\\n")
elif name == "PlistBuddy":
    print("local.olcr.desktop")
elif name != "codesign":
    sys.exit(92)
'''
        for name in ("ps", "security", "pgrep", "launchctl", "codesign", "PlistBuddy"):
            path = self.mock_bin / name
            path.write_text(fake)
            path.chmod(0o755)
        path = self.write("frontend/node_modules/.bin/tauri", fake)
        path.chmod(0o755)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def run_launcher(self, **extra):
        environment = {**os.environ, "PATH": str(self.mock_bin) + os.pathsep + os.environ.get("PATH", ""),
                       "OLCR_TEST_ROOT": str(self.root), **extra}
        environment.pop("OLCR_DEV_LAUNCHD_LABEL", None)
        environment.pop("OLCR_CUA_SIGNING_IDENTITY", None)
        return subprocess.run(["zsh", str(self.launcher)], cwd=self.root, env=environment,
                              text=True, capture_output=True, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def request_launch(self):
        result = self.run_launcher()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def assert_no_launch_or_build(self, calls):
        self.assertFalse(any(name == "tauri" or (name == "launchctl" and args[0] != "list")
                             for name, args in calls), calls)

    def test_bootstrap_reports_a_request_without_claiming_backend_readiness(self):
        result = self.request_launch()
        self.assertIn("OLCR_LAUNCH_REQUESTED=true", result.stdout)
        self.assertNotIn("Started latest OLCR", result.stdout)
        self.assertIn("readiness has not been verified", result.stdout)
        launch = json.loads((self.root / "launched.json").read_text())
        self.assertFalse(launch["KeepAlive"])
        self.assertEqual(str(self.bin.parents[2]), launch["ProgramArguments"][-1])

    def test_unchanged_managed_instance_is_reused_without_build_or_launch(self):
        self.request_launch()
        self.state.write_text("running")
        start = len(self.calls())
        result = self.run_launcher()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OLCR_SOURCE_MATCH=true", result.stdout)
        self.assert_no_launch_or_build(self.calls()[start:])

    def test_content_changes_with_preserved_mtime_refuse_stale_managed_instance(self):
        self.request_launch()
        self.state.write_text("running")
        for relative in ("frontend/src/main.tsx", "backend/olcr_api/app.py",
                         "desktop/src-tauri/src/main.rs", "desktop/src-tauri/Cargo.toml",
                         "frontend/package-lock.json"):
            with self.subTest(source=relative):
                path = self.root / relative
                original, stat = path.read_bytes(), path.stat()
                path.write_bytes(original.replace(b"old", b"new"))
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                start = len(self.calls())
                result = self.run_launcher()
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("OLCR_SOURCE_STALE", result.stderr)
                self.assert_no_launch_or_build(self.calls()[start:])
                path.write_bytes(original)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_new_or_deleted_source_refuses_stale_managed_instance(self):
        self.request_launch()
        self.state.write_text("running")
        path = self.write("backend/olcr_api/new_module.py", "new source\n")
        added = self.run_launcher()
        self.assertNotEqual(0, added.returncode, added.stdout + added.stderr)
        self.assertIn("OLCR_SOURCE_STALE", added.stderr)
        path.unlink()
        (self.root / "frontend/src/main.tsx").unlink()
        deleted = self.run_launcher()
        self.assertNotEqual(0, deleted.returncode, deleted.stdout + deleted.stderr)
        self.assertIn("OLCR_SOURCE_STALE", deleted.stderr)

    def test_existing_instance_without_source_attestation_is_not_claimed_current(self):
        self.state.write_text("running")
        result = self.run_launcher()
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OLCR_SOURCE_STALE", result.stderr)
        self.assertIn("Quit OLCR normally", result.stderr)
        self.assert_no_launch_or_build(self.calls())

    def test_source_change_during_build_prevents_launch(self):
        result = self.run_launcher(OLCR_TEST_EDIT_DURING_BUILD="1")
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OLCR_SOURCE_CHANGED_DURING_BUILD", result.stderr)
        self.assertFalse((self.root / "launched.json").exists())

    def test_failed_build_cannot_attest_an_existing_instance(self):
        result = self.run_launcher(OLCR_TEST_BUILD_FAIL="1")
        self.assertEqual(42, result.returncode, result.stdout + result.stderr)
        self.state.write_text("running")
        result = self.run_launcher()
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OLCR_SOURCE_STALE", result.stderr)

    def test_idle_registration_can_be_replaced_without_terminating_an_app(self):
        self.request_launch()
        self.state.write_text("idle")
        self.write("frontend/src/main.tsx", "new ui\n")
        result = self.request_launch()
        self.assertIn("OLCR_LAUNCH_REQUESTED=true", result.stdout)
        self.assertEqual(2, sum(name == "launchctl" and args[0] == "bootstrap" for name, args in self.calls()))

    def test_python_bytecode_does_not_make_current_sources_stale(self):
        self.request_launch()
        self.state.write_text("running")
        self.write("backend/olcr_api/__pycache__/app.cpython-312.pyc", "runtime bytecode\n")
        result = self.run_launcher()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OLCR_SOURCE_MATCH=true", result.stdout)


if __name__ == "__main__":
    unittest.main()
