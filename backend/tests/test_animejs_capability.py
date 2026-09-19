import sys
import unittest
import json
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from olcr_api.coding_tasks import canonical_coding_requirements, animejs_project_version, animejs_version_compatibility
from olcr_api.mcp_manifest import server_definition


class AnimeJsCapabilityTests(unittest.TestCase):
    def test_reference_precedes_version_gate_and_never_mutates_project(self):
        import olcr_api.app as api
        for version, action, expected_error in ((None, "ADD_V4", None), ("3.2.2", "BLOCK", "VERSION_CONFLICT")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory, "package.json")
                if version:
                    manifest.write_text(json.dumps({"dependencies": {"animejs": version}}))
                before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
                database = MagicMock()
                database.coding_task.return_value = {"original_goal": "Use Anime.js v4", "requirements": {}}
                runtime = MagicMock()
                runtime.initialize.return_value = {"status": "AVAILABLE", "response": {"result": {}}}
                runtime.tools_list.return_value = {"status": "AVAILABLE", "response": {"result": {"tools": [{"name": "search_animejs_docs"}]}}}
                runtime.call.return_value = {"status": "AVAILABLE", "response": {"result": {"content": [{"text": "v4 reference"}]}}}
                def inspect(root):
                    runtime.call.assert_called_once()
                    self.assertEqual(3, evidence.call_count)
                    return animejs_project_version(root)
                with patch.object(api, "db", database), patch.object(api, "MCPRuntime", return_value=runtime), \
                     patch.object(api, "node_mcp_launch_command", return_value=["fake"]), \
                     patch.object(api, "node_mcp_resource_status", return_value={}), \
                     patch.object(api, "animejs_project_version", side_effect=inspect), \
                     patch.object(api, "_mcp_evidence", return_value={"status": "PASS"}) as evidence:
                    _, error = api._run_required_mcp("test", "animejs", directory, "reference")
                self.assertEqual(expected_error, error)
                saved = database.update_coding_task.call_args.kwargs["requirements"]["animejs_project"]
                self.assertEqual(action, saved["ANIMEJS_PROJECT_ACTION"])
                self.assertEqual(before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})
                runtime.close.assert_called_once()

    def test_missing_manifest_is_greenfield_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            before = list(Path(directory).iterdir())
            value = animejs_project_version(directory)
            self.assertEqual("NOT_DECLARED", value["ANIMEJS_PROJECT_VERSION_STATE"])
            self.assertEqual("PASS_FOR_INSTALL", animejs_version_compatibility(value))
            self.assertEqual(before, list(Path(directory).iterdir()))

    def test_malformed_manifest_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text('{')
            self.assertEqual("UNREADABLE", animejs_project_version(directory)["ANIMEJS_PROJECT_VERSION_STATE"])

    def test_conflicting_lock_and_manifest_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text(json.dumps({"dependencies": {"animejs": "^4.0.0"}}))
            Path(directory, "package-lock.json").write_text(json.dumps({"packages": {"node_modules/animejs": {"version": "3.2.2"}}}))
            value = animejs_project_version(directory)
            self.assertEqual("AMBIGUOUS", value["ANIMEJS_PROJECT_VERSION_STATE"])
            self.assertEqual("UNKNOWN", animejs_version_compatibility(value))

    def test_animation_frontend_auto_selects_offline_reference(self):
        value = canonical_coding_requirements("Build a React animated hero with staggered text motion")
        self.assertEqual(["animejs"], value["selected_mcps"])
        self.assertEqual([], value["required_mcps"])

    def test_ordinary_frontend_and_explicit_opt_out_do_not_select(self):
        self.assertEqual([], canonical_coding_requirements("Create a React settings form")["selected_mcps"])
        self.assertEqual([], canonical_coding_requirements("Create a React animation, but アニメーション不要")["selected_mcps"])

    def test_explicit_animejs_is_required(self):
        self.assertIn("animejs", canonical_coding_requirements("Anime.jsを使って React page")["required_mcps"])

    def test_policy_is_read_only_and_v4(self):
        definition = server_definition("animejs")
        self.assertTrue(definition["version"].startswith("4."))
        self.assertEqual([], definition["allowed_network_hosts"])
        self.assertEqual({"search_animejs_docs", "get_animejs_api", "get_animejs_example", "get_animejs_pattern"}, set(definition["allowed_tools"]))

    def test_bundled_corpus_has_no_v3_metadata_or_legacy_global_example(self):
        corpus = (Path(__file__).parents[2] / "packaging" / "node-mcp" / "animejs-reference" / "animejs-v4-reviewed.json").read_text()
        self.assertNotIn('"animejs_version":"3.', corpus)
        self.assertNotIn("import anime from", corpus)

    def test_project_v4_is_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text(json.dumps({"dependencies": {"animejs": "^4.0.0"}}))
            evidence = animejs_project_version(directory)
            self.assertEqual("4", evidence["ANIMEJS_PROJECT_MAJOR_VERSION"])
            self.assertEqual("PASS", animejs_version_compatibility(evidence))

    def test_v3_conflict_and_explicit_migration_are_not_implicit_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text(json.dumps({"dependencies": {"animejs": "3.2.2"}}))
            evidence = animejs_project_version(directory)
            self.assertEqual("CONFLICT_V3_V4", animejs_version_compatibility(evidence, "add animation"))
            self.assertEqual("MIGRATION_REQUESTED", animejs_version_compatibility(evidence, "Anime.js v4へ移行して"))
            self.assertEqual("3.2.2", evidence["ANIMEJS_PROJECT_VERSION"])

    def test_no_dependency_and_unknown_are_not_guessed_as_v4(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text("{}")
            self.assertEqual("PASS_FOR_INSTALL", animejs_version_compatibility(animejs_project_version(directory)))
            Path(directory, "package.json").write_text(json.dumps({"dependencies": {"animejs": "workspace:*"}}))
            self.assertEqual("UNKNOWN", animejs_version_compatibility(animejs_project_version(directory)))

    def test_lockfile_is_used_when_manifest_has_no_direct_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text("{}")
            Path(directory, "package-lock.json").write_text(json.dumps({"packages": {"node_modules/animejs": {"version": "4.0.0"}}}))
            evidence = animejs_project_version(directory)
            self.assertEqual("4", evidence["ANIMEJS_PROJECT_MAJOR_VERSION"])
            self.assertEqual("PASS", animejs_version_compatibility(evidence))
