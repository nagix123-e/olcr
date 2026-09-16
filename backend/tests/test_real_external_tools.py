"""Opt-in smoke checks for the live provider endpoints.

These tests are skipped during the normal offline suite.  Run with
``OLCR_REAL_PROVIDER_SMOKE=1`` when a network-connected runtime is available.
Provider outages are reported as provider outcomes rather than test failures;
adapter/provenance regressions still fail the test.
"""
import os
import unittest

from olcr_api import external_tools as tools


def _classify(exc: Exception) -> str:
    code = str(exc)
    if code.startswith("PROVIDER_RATE_LIMITED"):
        return "RATE_LIMITED"
    if "AUTH" in code or "CREDENTIAL" in code:
        return "AUTH_REQUIRED"
    if code.startswith(("PROVIDER_", "LOCAL_TOOL_UNAVAILABLE")):
        return "NETWORK_UNAVAILABLE" if code in {"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT"} else code
    return "IMPLEMENTATION_FAILURE"


@unittest.skipUnless(os.environ.get("OLCR_REAL_PROVIDER_SMOKE") == "1", "opt-in real provider smoke")
class RealExternalProviderSmokeTests(unittest.TestCase):
    def _smoke(self, label, tool_id, arguments, expect_items=True):
        try:
            result = tools.execute(tool_id, arguments)
        except tools.ExternalToolError as exc:
            outcome = _classify(exc)
            print(f"REAL_{label}={outcome}")
            self.assertNotEqual("IMPLEMENTATION_FAILURE", outcome, str(exc))
            return None
        self.assertEqual(tool_id, result.get("tool_id"))
        self.assertEqual(tools.REGISTRY[tool_id].provider, result.get("provider"))
        self.assertTrue(result.get("sources"), "provider provenance missing")
        self.assertEqual(tools.REGISTRY[tool_id].provider, result["sources"][0].get("provider"))
        self.assertTrue(result["sources"][0].get("canonical_url"))
        self.assertIn("data", result)
        if expect_items:
            self.assertTrue(result["data"].get("items") or result["data"].get("raw_metadata"), "normalized data empty")
        print(f"REAL_{label}=PASS")
        return result

    def test_representative_real_http_providers(self):
        self._smoke("WIKIDATA_SMOKE", "knowledge.wikidata", {"query": "SELECT ?item WHERE { ?item wdt:P31 wd:Q5 } LIMIT 1"})
        self._smoke("WORLD_BANK_SMOKE", "statistics.world_bank", {"country": "JP", "indicator": "NY.GDP.MKTP.CD", "limit": 1})
        self._smoke("USGS_SMOKE", "earth.earthquake", {"query": "Japan", "minmagnitude": 8, "limit": 1})
        self._smoke("OPEN_LIBRARY_SMOKE", "books.search", {"query": "Python", "limit": 1})
        self._smoke("GITHUB_SMOKE", "software.github", {"repo": "octocat/Hello-World", "endpoint": "repo"})

    def test_real_router_and_local_tools(self):
        for text, expected in (("日本のGDP", "statistics.world_bank"), ("最近の大きな地震", "earth.earthquake"), ("x^2 - 1を因数分解", "math.symbolic"), ("数値積分", "math.numeric")):
            selected = tools.route(text)
            self.assertIsNotNone(selected)
            self.assertEqual(expected, selected[0])
            if expected == "math.symbolic":
                result = tools.execute(*selected)
                self.assertEqual(expected, result["tool_id"])
                self.assertTrue(result["sources"])
            elif expected == "math.numeric":
                try:
                    result = tools.execute(*selected)
                except tools.ExternalToolError as exc:
                    self.assertEqual("LOCAL_TOOL_UNAVAILABLE", str(exc))
                    print("SCIPY_RUNTIME_SMOKE=LOCAL_TOOL_UNAVAILABLE")
                else:
                    self.assertEqual(expected, result["tool_id"])
                    print("SCIPY_RUNTIME_SMOKE=PASS")
            else:
                self._smoke("ROUTER_" + expected.upper().replace(".", "_"), *selected)


if __name__ == "__main__":
    unittest.main()
