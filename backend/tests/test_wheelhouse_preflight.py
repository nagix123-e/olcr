import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepare_wheelhouse", ROOT / "scripts" / "prepare_wheelhouse.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class WheelhousePreflightTests(unittest.TestCase):
    def _requirements(self, directory: Path) -> Path:
        requirements = directory / "requirements.txt"
        requirements.write_text("scipy>=1.11,<2.0\nnumpy>=2.0\n")
        return requirements

    def test_missing_scipy_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = PREPARE.wheelhouse_preflight(root, self._requirements(root))
            self.assertEqual("PASS", result["SCIPY_REQUIREMENT_PRESENT"])
            self.assertEqual("FAIL", result["SCIPY_WHEELHOUSE_PREFLIGHT"])
            self.assertEqual("SCIPY_WHEEL_MISSING", result["SCIPY_WHEELHOUSE_REASON"])

    def test_compatible_arm64_wheel_and_transitive_numpy_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self._requirements(root)
            (root / "scipy-1.14.1-cp310-cp310-macosx_12_0_arm64.whl").touch()
            (root / "numpy-2.2.6-cp310-cp310-macosx_12_0_arm64.whl").touch()
            result = PREPARE.wheelhouse_preflight(root, requirements)
            self.assertEqual("PASS", result["SCIPY_WHEELHOUSE_PREFLIGHT"])
            self.assertEqual("PASS", result["SCIPY_TRANSITIVE_WHEELS_PRESENT"])

    def test_wrong_platform_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self._requirements(root)
            (root / "scipy-1.14.1-cp310-cp310-manylinux_2_17_x86_64.whl").touch()
            (root / "numpy-2.2.6-cp310-cp310-macosx_11_0_arm64.whl").touch()
            result = PREPARE.wheelhouse_preflight(root, requirements)
            self.assertEqual("FAIL", result["SCIPY_WHEELHOUSE_PREFLIGHT"])
            self.assertEqual("SCIPY_WHEEL_INCOMPATIBLE", result["SCIPY_WHEELHOUSE_REASON"])

    def test_runtime_import_probe_detects_constructed_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scipy").mkdir()
            (root / "scipy" / "__init__.py").write_text("__version__ = 'test'\n")
            self.assertTrue(PREPARE.runtime_import_available(extra_path=root))

    def test_prepare_uses_binary_targeted_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self._requirements(root)

            def download(command, check):
                self.assertTrue(check)
                (root / "scipy-1.14.1-cp310-cp310-macosx_12_0_arm64.whl").touch()
                (root / "numpy-2.2.6-cp310-cp310-macosx_12_0_arm64.whl").touch()

            with patch.object(PREPARE, "REQUIREMENTS", requirements), patch.object(PREPARE.subprocess, "run", side_effect=download) as run:
                result = PREPARE.prepare(root, python_executable="python")
            command = run.call_args.args[0]
            self.assertIn("--only-binary=:all:", command)
            self.assertIn("--platform", command)
            self.assertIn("macosx_12_0_arm64", command)
            self.assertEqual("PASS", result["SCIPY_WHEELHOUSE_PREFLIGHT"])


if __name__ == "__main__":
    unittest.main()
