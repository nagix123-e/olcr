import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepare_serena_runtime", ROOT / "scripts" / "prepare_serena_runtime.py")
assert SPEC and SPEC.loader
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class PrepareSerenaRuntimeTests(unittest.TestCase):
    def test_exact_artifact_metadata_is_pinned(self):
        metadata = json.loads((ROOT / "packaging" / "serena-runtime" / "artifacts.json").read_text())
        python = metadata["python"]
        self.assertEqual("20260901", python["release"])
        self.assertEqual("3.11.16", python["version"])
        self.assertIn("aarch64-apple-darwin-install_only_stripped", python["artifact"])
        self.assertEqual(64, len(python["sha256"]))
        self.assertEqual("1.7.0", metadata["serena"]["version"])

    def test_input_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.tar.gz"
            artifact.write_bytes(b"not the reviewed artifact")
            with self.assertRaises(SystemExit) as error:
                PREPARE.verify(artifact, "0" * 64)
            self.assertEqual("SERENA_RUNTIME_INPUT_INTEGRITY_FAIL", str(error.exception))

    def test_wheelhouse_rejects_extra_or_tampered_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "example-1.0-py3-none-any.whl"
            wheel.write_bytes(b"verified wheel")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"artifacts": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}))
            PREPARE.verify_wheelhouse(wheelhouse, manifest)

            (wheelhouse / "unreviewed-1.0-py3-none-any.whl").write_bytes(b"unexpected")
            with self.assertRaises(SystemExit) as error:
                PREPARE.verify_wheelhouse(wheelhouse, manifest)
            self.assertEqual("SERENA_WHEELHOUSE_CONTENTS_INVALID", str(error.exception))


if __name__ == "__main__":
    unittest.main()
