import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.coding_tasks import phase_executor_capability
from olcr_api.package_manager import (
    PackageManagerError,
    PackageManagerExecutor,
    package_manager_from_project,
)


class FakeProcess:
    _next_pid = 4000

    def __init__(self, returncode=0, stdout="installed", stderr=""):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class PackageManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.tmp.name)
        (self.root / "package.json").write_text("{}\n", encoding="utf-8")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def popen(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return FakeProcess()

    def executor(self, **kwargs):
        return PackageManagerExecutor(popen=self.popen, **kwargs)

    def test_authorized_greenfield_install_records_process_and_artifacts(self):
        result = self.executor().install(
            workspace_root=self.root, package_manager="npm", packages=["react", "vite"],
            dependency_kind="dependencies", authorized_packages=["react", "vite"])
        self.assertEqual("PACKAGE_INSTALL", result["operation_type"])
        self.assertEqual("npm", result["package_manager"])
        self.assertEqual(["react", "vite"], result["packages_authorized"])
        self.assertTrue(result["process_started"])
        self.assertEqual(0, result["exit_code"])
        self.assertFalse(result["timeout"])
        self.assertEqual(str(self.root), self.calls[0][1]["cwd"])
        self.assertFalse(self.calls[0][1]["shell"])
        self.assertIn("--ignore-scripts", self.calls[0][0])

    def test_package_not_in_canonical_requirements_is_rejected(self):
        with self.assertRaisesRegex(PackageManagerError, "canonical authorization"):
            self.executor().install(workspace_root=self.root, package_manager="npm",
                                    packages=["left-pad"], authorized_packages=["react"])
        self.assertEqual([], self.calls)

    def test_scoped_package_version_mapping_is_canonicalized(self):
        result = self.executor().install(
            workspace_root=self.root, package_manager="npm", packages=["@shadcn/ui@1"],
            authorized_packages={"@shadcn/ui": "1"})
        self.assertEqual(["@shadcn/ui@1"], result["packages_authorized"])

    def test_global_install_and_raw_shell_syntax_are_rejected(self):
        with self.assertRaises(PackageManagerError):
            self.executor().install(workspace_root=self.root, package_manager="npm",
                                    packages=["-g", "react"], authorized_packages=["react", "-g"])
        with self.assertRaises(PackageManagerError):
            self.executor().install(workspace_root=self.root, package_manager="npm",
                                    packages=["react;touch pwned"], authorized_packages=["react;touch pwned"])
        self.assertEqual([], self.calls)

    def test_workspace_root_must_be_real_selected_directory(self):
        with self.assertRaises(PackageManagerError) as caught:
            self.executor().install(workspace_root=self.root / "missing", package_manager="npm",
                                    packages=["react"], authorized_packages=["react"])
        self.assertEqual("WORKSPACE_INVALID", caught.exception.failure_class)
        with self.assertRaises(PackageManagerError) as caught:
            self.executor().install(workspace_root=self.root / "..", authorized_workspace_root=self.root,
                                    package_manager="npm", packages=["react"], authorized_packages=["react"])
        self.assertEqual("WORKSPACE_OUTSIDE_ROOT", caught.exception.failure_class)

    def test_manager_resolution_uses_metadata_then_lockfile_and_requires_evidence(self):
        (self.root / "package.json").write_text(json.dumps({"packageManager": "pnpm@9.0.0"}), encoding="utf-8")
        self.assertEqual("pnpm", package_manager_from_project(self.root)["package_manager"])
        (self.root / "package.json").write_text("{}\n", encoding="utf-8")
        (self.root / "package-lock.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual("npm", package_manager_from_project(self.root)["package_manager"])
        (self.root / "package-lock.json").unlink()
        with self.assertRaisesRegex(PackageManagerError, "no package-manager evidence"):
            package_manager_from_project(self.root)

    def test_nonzero_exit_is_typed_failure_with_no_false_success(self):
        def failed(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeProcess(returncode=7, stdout="partial", stderr="registry failed")
        executor = PackageManagerExecutor(popen=failed)
        with self.assertRaises(PackageManagerError) as caught:
            executor.install(workspace_root=self.root, package_manager="npm",
                             packages=["react"], authorized_packages=["react"])
        self.assertEqual("PACKAGE_MANAGER_NONZERO_EXIT", caught.exception.failure_class)
        self.assertEqual(7, caught.exception.details["evidence"]["exit_code"])
        self.assertEqual("FAIL", caught.exception.details["evidence"]["status"])
        self.assertEqual("registry failed", caught.exception.details["evidence"]["stderr_summary"])
        self.assertTrue(caught.exception.details["evidence"]["failure_fingerprint"])

    def test_not_found_and_network_failures_are_distinguished_with_sanitized_output(self):
        def missing(argv, **kwargs):
            return FakeProcess(returncode=1, stderr="npm error code E404\nnpm error 404 Not Found - GET token=secret")
        with self.assertRaises(PackageManagerError) as caught:
            PackageManagerExecutor(popen=missing).install(
                workspace_root=self.root, package_manager="npm", packages=["react"], authorized_packages=["react"])
        evidence = caught.exception.details["evidence"]
        self.assertEqual("PACKAGE_NOT_FOUND", caught.exception.failure_class)
        self.assertNotIn("secret", evidence["stderr_summary"])
        self.assertTrue(evidence["resolved_executable"])
        self.assertEqual("ALLOWLIST", evidence["environment_policy"]["mode"])
        def offline(argv, **kwargs):
            return FakeProcess(returncode=1, stderr="npm error code ENOTFOUND registry.npmjs.org")
        with self.assertRaises(PackageManagerError) as caught:
            PackageManagerExecutor(popen=offline).install(
                workspace_root=self.root, package_manager="npm", packages=["react"], authorized_packages=["react"])
        self.assertEqual("DNS_OR_NETWORK_FAILURE", caught.exception.failure_class)

    def test_npm_uses_isolated_cache_and_classifies_cache_permission_failure(self):
        def cache_error(argv, **kwargs):
            self.assertNotEqual(kwargs["env"]["NPM_CONFIG_CACHE"], str(Path.home() / ".npm"))
            return FakeProcess(returncode=1, stderr="npm error code EEXIST\nnpm error EACCES cache")
        with self.assertRaises(PackageManagerError) as caught:
            PackageManagerExecutor(popen=cache_error).install(
                workspace_root=self.root, package_manager="npm", packages=["react"], authorized_packages=["react"])
        self.assertEqual("PACKAGE_MANAGER_CONFIG_FAILURE", caught.exception.failure_class)
        self.assertEqual("ISOLATED_TEMP_CACHE", caught.exception.details["evidence"]["cache_policy"])

    def test_timeout_terminates_child_and_is_typed(self):
        process = FakeProcess()
        def timed(argv, **kwargs):
            self.calls.append((argv, kwargs))
            process.returncode = None
            def communicate(timeout=None):
                if timeout is not None and timeout <= 0.01:
                    raise subprocess.TimeoutExpired(argv, timeout, output="out", stderr="err")
                return "out", "err"
            process.communicate = communicate
            process.poll = lambda: process.returncode
            return process
        with self.assertRaises(PackageManagerError) as caught:
            PackageManagerExecutor(timeout_seconds=0.01, popen=timed).install(
                workspace_root=self.root, package_manager="npm", packages=["react"], authorized_packages=["react"])
        self.assertEqual("PACKAGE_MANAGER_TIMEOUT", caught.exception.failure_class)
        self.assertTrue(process.terminated or process.killed)

    def test_dependency_capability_requires_authorized_executor(self):
        self.assertFalse(phase_executor_capability({"requires_dependency_installation": True})["executable"])
        capability = phase_executor_capability({
            "requires_dependency_installation": True,
            "package_manager_executor": "AUTHORIZED_PACKAGE_MANAGER",
            "authorized_package_requirements": ["react"],
        })
        self.assertTrue(capability["executable"])
        self.assertIn("AUTHORIZED_PACKAGE_MANAGER", capability["available"])


if __name__ == "__main__":
    unittest.main()
