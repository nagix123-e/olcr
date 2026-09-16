import sys
import unittest
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from olcr_api.coding_tasks import canonical_coding_requirements, animejs_project_version, animejs_version_compatibility
from olcr_api.mcp_manifest import server_definition


class AnimeJsCapabilityTests(unittest.TestCase):
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
            self.assertEqual("NO_DEPENDENCY", animejs_version_compatibility(animejs_project_version(directory)))
            Path(directory, "package.json").write_text(json.dumps({"dependencies": {"animejs": "workspace:*"}}))
            self.assertEqual("UNKNOWN", animejs_version_compatibility(animejs_project_version(directory)))

    def test_lockfile_is_used_when_manifest_has_no_direct_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text("{}")
            Path(directory, "package-lock.json").write_text(json.dumps({"packages": {"node_modules/animejs": {"version": "4.0.0"}}}))
            evidence = animejs_project_version(directory)
            self.assertEqual("4", evidence["ANIMEJS_PROJECT_MAJOR_VERSION"])
            self.assertEqual("PASS", animejs_version_compatibility(evidence))
