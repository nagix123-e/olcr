import json
import os
import tempfile
import unittest
from pathlib import Path

from olcr_api.mcp_manifest import server_definition
from olcr_api.serena_runtime import launch_command, runtime_root


class SerenaRuntimeTests(unittest.TestCase):
    def test_only_completed_bundled_runtime_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            resource_root = Path(directory)
            runtime = resource_root / "mcp-runtime" / "serena"
            (runtime / "python" / "bin").mkdir(parents=True)
            (runtime / "python" / "bin" / "python3").touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoint": ["-c", "pass"]}))
            old = os.environ.get("OLCR_MCP_RUNTIME_ROOT")
            os.environ["OLCR_MCP_RUNTIME_ROOT"] = str(resource_root)
            try:
                self.assertEqual(runtime, runtime_root())
                self.assertEqual([str(runtime / "python" / "bin" / "python3"), "-c", "pass"], launch_command())
            finally:
                if old is None:
                    os.environ.pop("OLCR_MCP_RUNTIME_ROOT", None)
                else:
                    os.environ["OLCR_MCP_RUNTIME_ROOT"] = old

    def test_missing_marker_has_no_host_python_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.environ.get("OLCR_MCP_RUNTIME_ROOT")
            os.environ["OLCR_MCP_RUNTIME_ROOT"] = directory
            try:
                self.assertIsNone(runtime_root())
                self.assertIsNone(launch_command())
            finally:
                if old is None:
                    os.environ.pop("OLCR_MCP_RUNTIME_ROOT", None)
                else:
                    os.environ["OLCR_MCP_RUNTIME_ROOT"] = old

    def test_missing_runtime_keeps_the_declared_repo_tools_fallback(self):
        """An unavailable bundle cannot cause a host-Python substitution."""
        with tempfile.TemporaryDirectory() as directory:
            old = os.environ.get("OLCR_MCP_RUNTIME_ROOT")
            os.environ["OLCR_MCP_RUNTIME_ROOT"] = directory
            try:
                self.assertIsNone(launch_command())
                self.assertEqual("repo_tools", server_definition("serena")["fallback"])
            finally:
                if old is None:
                    os.environ.pop("OLCR_MCP_RUNTIME_ROOT", None)
                else:
                    os.environ["OLCR_MCP_RUNTIME_ROOT"] = old
