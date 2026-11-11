import unittest
import json
from pathlib import Path
from unittest.mock import patch

from olcr_api.api_acceptance import (ACCEPTANCE_CASES, MANIFEST_SOURCE_COUNT,
                                     PRODUCTION_ACCEPTANCE_CASES, PRODUCTION_EXCLUDED_API_IDS,
                                     acceptance_tool_ids, manifest_registry_diff,
                                     production_tool_ids, validate_acceptance_fixture)
from olcr_api import external_tools


class ApiAcceptanceManifestTests(unittest.TestCase):
    def test_fixture_has_exactly_one_case_per_manifest_source(self):
        diagnostics = validate_acceptance_fixture()
        self.assertTrue(diagnostics["valid"])
        self.assertEqual(MANIFEST_SOURCE_COUNT, diagnostics["source_count"])
        self.assertEqual(MANIFEST_SOURCE_COUNT, diagnostics["case_count"])
        self.assertEqual(MANIFEST_SOURCE_COUNT, diagnostics["unique_tool_ids"])

    def test_registry_diff_is_fail_closed(self):
        diagnostics = manifest_registry_diff(["calendar.public_holidays"])
        self.assertFalse(diagnostics["exact_match"])
        self.assertIn("space.launches", diagnostics["missing"])

    def test_fixture_policy_never_promotes_skips(self):
        diagnostics = validate_acceptance_fixture()
        self.assertFalse(diagnostics["skip_is_pass"])
        self.assertFalse(diagnostics["auth_blocked_is_pass"])
        self.assertEqual(len(acceptance_tool_ids()), len(set(acceptance_tool_ids())))

    def test_checked_in_json_fixture_matches_python_fixture(self):
        path = Path(__file__).parents[2] / "benchmark_results" / "api_e2e" / "acceptance_cases.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(ACCEPTANCE_CASES), payload["cases"])

    def test_failed_or_user_blocked_providers_are_not_in_production_surface(self):
        production_ids = set(production_tool_ids())
        self.assertEqual(len(PRODUCTION_ACCEPTANCE_CASES), len(production_ids))
        self.assertFalse(PRODUCTION_EXCLUDED_API_IDS & production_ids)
        self.assertEqual(89, len(production_ids))
        self.assertEqual(89, manifest_registry_diff(set(external_tools.RUNTIME_REGISTRY))["expected_count"])
        self.assertTrue(manifest_registry_diff(set(external_tools.RUNTIME_REGISTRY))["exact_match"])
        self.assertTrue(PRODUCTION_EXCLUDED_API_IDS.issubset(set(acceptance_tool_ids())))

    def test_ucdp_is_back_in_production_route_and_credential_onboarding(self):
        tool_id = "conflict.ucdp"
        prompt = "UCDPでウクライナの最近の紛争イベントを5件見せて"
        self.assertNotIn(tool_id, PRODUCTION_EXCLUDED_API_IDS)
        self.assertIn(tool_id, external_tools.RUNTIME_REGISTRY)
        self.assertEqual(tool_id, external_tools.route(prompt)[0])
        with patch.dict("os.environ", {"OLCR_UCDP_API_TOKEN": ""}), \
             patch.object(external_tools, "_RUNTIME_CREDENTIALS", {}):
            setup = external_tools.credential_setup(tool_id)
        self.assertEqual(tool_id, setup["tool_id"])
        self.assertEqual("https://ucdp.uu.se/apidocs/", setup["setup_url"])

    def test_excluded_provider_cannot_route_execute_or_prompt_for_credentials(self):
        historical_cases = {case["tool_id"]: case for case in ACCEPTANCE_CASES}
        for tool_id in PRODUCTION_EXCLUDED_API_IDS:
            with self.subTest(tool_id=tool_id):
                routed = external_tools.route(historical_cases[tool_id]["user_prompt"])
                self.assertNotEqual(tool_id, routed[0] if routed else None)
                self.assertIsNone(external_tools.credential_setup(tool_id))
                with self.assertRaisesRegex(external_tools.ExternalToolError, "PROVIDER_NOT_IN_PRODUCTION"):
                    external_tools.configure_credentials(tool_id, {})
                with self.assertRaisesRegex(external_tools.ExternalToolError, "PROVIDER_NOT_IN_PRODUCTION"):
                    external_tools.execute(tool_id, {})
                self.assertNotIn(tool_id, {row["tool_id"] for row in external_tools.status(True)})


if __name__ == "__main__":
    unittest.main()
