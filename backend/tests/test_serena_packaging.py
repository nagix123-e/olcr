import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
assert SPEC and SPEC.loader
BUILD_RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_RELEASE)


class SerenaPackagingTests(unittest.TestCase):
    def test_completed_runtime_is_staged_with_its_license_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "python" / "bin").mkdir(parents=True)
            (source / "python" / "bin" / "python3").touch()
            (source / "licenses").mkdir()
            (source / "wheelhouse").mkdir()
            (source / "wheelhouse" / "not-for-release.whl").touch()
            (source / "runtime-manifest.json").write_text(json.dumps({"serena_version": "1.7.0", "python": {"version": "3.11.16"}}))
            (source / "licenses" / "license-inventory.json").write_text(json.dumps({"components": [{"component": "Serena"}]}))
            stage = root / "stage"
            (stage / "licenses").mkdir(parents=True)
            (stage / "licenses" / "license-manifest.json").write_text(json.dumps({"components": []}))
            (stage / "licenses" / "THIRD_PARTY_NOTICES.md").write_text("# notices\n")

            result = BUILD_RELEASE.stage_serena_runtime(stage, source)

            self.assertEqual("mcp-runtime/serena", result["path"])
            self.assertEqual(1, result["licensed_components"])
            self.assertTrue((stage / "mcp-runtime" / "serena" / "runtime-manifest.json").is_file())
            self.assertFalse((stage / "mcp-runtime" / "serena" / "wheelhouse").exists())
            manifest = json.loads((stage / "licenses" / "license-manifest.json").read_text())
            self.assertEqual("Serena bundled runtime", manifest["components"][-1]["component"])
