from pathlib import Path
import importlib.util
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ReleaseBuildTests(unittest.TestCase):
    def test_release_identity_is_current(self):
        self.assertEqual("0.4.8", BUILD.VERSION)
        source = (ROOT / "scripts" / "build_release.py").read_text()
        self.assertIn("OLCR v0.4.8", source)
        self.assertNotIn("OLCR v0.1.3", source)

    def test_runtime_cache_is_removed_from_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            cache = stage / "runtime" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "module.cpython-310.pyc").write_bytes(b"cache")
            (stage / "runtime" / "module.pyc").write_bytes(b"cache")
            (stage / "runtime" / "module.pyo").write_bytes(b"cache")
            (stage / "runtime" / "module.py").write_text("pass")
            BUILD.clean_runtime_cache(stage)
            self.assertEqual([], list(stage.rglob("__pycache__")))
            self.assertEqual([], list(stage.rglob("*.pyc")))
            self.assertEqual([], list(stage.rglob("*.pyo")))
            self.assertTrue((stage / "runtime" / "module.py").exists())


if __name__ == "__main__":
    unittest.main()
