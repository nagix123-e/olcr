import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from olcr_api.node_mcp_runtime import launch_command, resolve_mcp_resources, runtime_root


class NodeMcpRuntimeTests(unittest.TestCase):
    def test_desktop_environment_rejects_non_executable_node_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="OLCR Desktop ") as directory:
            root = Path(directory)
            node = root / "mcp-runtime/node/node/bin/node"
            node.parent.mkdir(parents=True)
            node.touch()
            node.parents[2].joinpath("runtime-manifest.json").write_text('{}')
            with patch.dict(os.environ, {"OLCR_NODE_MCP_RUNTIME_ROOT": str(root),
                                         "OLCR_BACKEND_DIR": str(Path(__file__).parents[1]),
                                         "MCP_RESOURCE_MODE": "DEV_SOURCE"}):
                self.assertIsNone(runtime_root())
                self.assertEqual("NODE_RUNTIME_MISSING", resolve_mcp_resources("animejs")["reason"])
                node.chmod(0o755)
                self.assertEqual(node.parents[2], runtime_root())
                command = launch_command("animejs")
                self.assertEqual(str(node), command[0])
                self.assertIn("packaging/node-mcp/animejs-reference/server.js", command[1])

    def test_resolves_installed_current_runtime_without_a_developer_path(self):
        with tempfile.TemporaryDirectory() as directory:
            support = Path(directory)
            runtime = support / "runtime" / "current" / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            entrypoint = runtime / "node_modules" / "shadcn" / "dist" / "index.js"
            entrypoint.parent.mkdir(parents=True); entrypoint.touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"shadcn": ["node_modules/shadcn/dist/index.js", "mcp"]}}))
            previous_support = os.environ.get("OLCR_APP_SUPPORT")
            previous_runtime = os.environ.pop("OLCR_NODE_MCP_RUNTIME_ROOT", None)
            os.environ["OLCR_APP_SUPPORT"] = str(support)
            try:
                self.assertEqual(runtime, runtime_root())
                self.assertEqual([str(runtime / "node" / "bin" / "node"), str(entrypoint), "mcp"], launch_command("shadcn"))
            finally:
                if previous_support is None: os.environ.pop("OLCR_APP_SUPPORT", None)
                else: os.environ["OLCR_APP_SUPPORT"] = previous_support
                if previous_runtime is not None: os.environ["OLCR_NODE_MCP_RUNTIME_ROOT"] = previous_runtime

    def test_resolves_only_a_completed_local_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            entrypoint = runtime / "node_modules" / "shadcn" / "dist" / "index.js"
            entrypoint.parent.mkdir(parents=True); entrypoint.touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"shadcn": ["node_modules/shadcn/dist/index.js", "mcp"]}}))
            previous = os.environ.get("OLCR_NODE_MCP_RUNTIME_ROOT")
            os.environ["OLCR_NODE_MCP_RUNTIME_ROOT"] = str(root)
            try:
                self.assertEqual(runtime, runtime_root())
                self.assertEqual([str(runtime / "node" / "bin" / "node"), str(entrypoint), "mcp"], launch_command("shadcn"))
            finally:
                if previous is None:
                    os.environ.pop("OLCR_NODE_MCP_RUNTIME_ROOT", None)
                else:
                    os.environ["OLCR_NODE_MCP_RUNTIME_ROOT"] = previous

    def test_missing_runtime_never_has_a_node_path_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("OLCR_NODE_MCP_RUNTIME_ROOT")
            os.environ["OLCR_NODE_MCP_RUNTIME_ROOT"] = directory
            try:
                self.assertIsNone(runtime_root())
                self.assertIsNone(launch_command("playwright"))
            finally:
                if previous is None:
                    os.environ.pop("OLCR_NODE_MCP_RUNTIME_ROOT", None)
                else:
                    os.environ["OLCR_NODE_MCP_RUNTIME_ROOT"] = previous

    def test_missing_manifest_entrypoint_is_not_launchable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"shadcn": ["node_modules/shadcn/dist/index.js", "mcp"]}}))
            self.assertIsNone(launch_command("shadcn", root))

    def test_relocated_resource_root_with_spaces_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "OLCR Node MCP Relocated"
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            entrypoint = runtime / "node_modules" / "shadcn" / "dist" / "index.js"
            entrypoint.parent.mkdir(parents=True); entrypoint.touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"shadcn": ["node_modules/shadcn/dist/index.js", "mcp"]}}))
            self.assertEqual([str(runtime / "node" / "bin" / "node"), str(entrypoint), "mcp"], launch_command("shadcn", root))

    def test_olcr_owned_server_entrypoint_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True); (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            entrypoint = runtime / "servers" / "animejs-reference" / "server.js"
            entrypoint.parent.mkdir(parents=True); entrypoint.touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"animejs": ["servers/animejs-reference/server.js"]}}))
            self.assertEqual([str(runtime / "node" / "bin" / "node"), str(entrypoint)], launch_command("animejs", root))

    def test_latest_source_uses_bundled_node_with_checked_in_anime_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime-root"
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            node = runtime / "node" / "bin" / "node"
            node.touch(); node.chmod(0o755)
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {}}))
            source = Path(directory) / "checkout"
            source_server = source / "packaging" / "node-mcp" / "animejs-reference" / "server.js"
            source_server.parent.mkdir(parents=True)
            source_server.touch()
            corpus = source_server.with_name("animejs-v4-reviewed.json")
            corpus.touch()
            resolution = resolve_mcp_resources("animejs", root, source)
            self.assertEqual("DEV_SOURCE", resolution["resource_mode"])
            self.assertTrue(resolution["node_runtime_available"])
            self.assertTrue(resolution["server_resource_available"])
            self.assertTrue(resolution["corpus_resource_available"])
            self.assertEqual([str(node), str(source_server)], resolution["command"])

    def test_release_mode_never_falls_back_to_source_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime-root"
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {}}))
            source = Path(directory) / "checkout"
            source_server = source / "packaging" / "node-mcp" / "animejs-reference" / "server.js"
            source_server.parent.mkdir(parents=True); source_server.touch()
            source_server.with_name("animejs-v4-reviewed.json").touch()
            previous = os.environ.get("MCP_RESOURCE_MODE")
            os.environ["MCP_RESOURCE_MODE"] = "BUNDLED_RELEASE"
            try:
                resolution = resolve_mcp_resources("animejs", root, source)
            finally:
                if previous is None: os.environ.pop("MCP_RESOURCE_MODE", None)
                else: os.environ["MCP_RESOURCE_MODE"] = previous
            self.assertEqual("SERVER_RESOURCE_MISSING", resolution["reason"])
            self.assertIsNone(resolution["command"])

    def test_bundled_anime_server_without_corpus_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "mcp-runtime" / "node"
            (runtime / "node" / "bin").mkdir(parents=True)
            (runtime / "node" / "bin" / "node").touch(); (runtime / "node" / "bin" / "node").chmod(0o755)
            server = runtime / "servers" / "animejs-reference" / "server.js"
            server.parent.mkdir(parents=True); server.touch()
            (runtime / "runtime-manifest.json").write_text(json.dumps({"entrypoints": {"animejs": ["servers/animejs-reference/server.js"]}}))
            resolution = resolve_mcp_resources("animejs", root)
            self.assertFalse(resolution["corpus_resource_available"])
            self.assertEqual("CORPUS_RESOURCE_MISSING", resolution["reason"])
            self.assertIsNone(resolution["command"])
