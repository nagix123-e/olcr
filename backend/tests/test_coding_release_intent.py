from __future__ import annotations

import unittest

from olcr_api.coding_tasks import (
    ReleaseIntent,
    build_release_preflight,
    canonical_coding_requirements,
    detect_release_intent,
    normalize_coding_requirements,
)


def intent_with_target() -> ReleaseIntent:
    return detect_release_intent(
        "Publish the package on npm.",
        {
            "publishing_targets": ["npm"],
            "app_or_package": "@acme/widget",
            "version": "1.2.3",
            "channel": "beta",
        },
    )


def references(*categories: str, officiality: str = "VERIFIED", accessed_at: float = 1_900_000_000.0):
    return [
        {
            "provider_id": "npm",
            "category": category,
            "officiality": officiality,
            "accessed_at": accessed_at,
        }
        for category in categories
    ]


class ReleaseIntentTests(unittest.TestCase):
    def test_build_only_does_not_authorize_publication(self):
        intent = detect_release_intent(
            "Prepare a release build for npm.",
            {
                "publishing_targets": ["npm"],
                "app_or_package": "@acme/widget",
                "version": "1.2.3",
                "channel": "stable",
            },
        )
        self.assertFalse(intent.requested)
        self.assertIsNone(intent.provider_id)
        self.assertEqual([], intent.blockers)
        self.assertFalse(detect_release_intent("How do I publish to npm?").requested)
        self.assertFalse(detect_release_intent("npmに公開する手順を教えてください").requested)
        self.assertFalse(detect_release_intent("Add a publish button to the admin UI.").requested)
        self.assertFalse(detect_release_intent("公開する機能を作成してください").requested)
        self.assertFalse(detect_release_intent(
            "Release build for npm",
            {"publishing_targets": ["npm"], "app_or_package": "@acme/widget",
             "version": "1.2.3", "channel": "stable"},
        ).requested)
        build_then_publish = detect_release_intent(
            "リリースビルドを作成してからnpmに公開する",
            {"publishing_targets": ["npm"], "app_or_package": "@acme/widget",
             "version": "1.2.3", "channel": "stable"},
        )
        self.assertTrue(build_then_publish.requested)
        self.assertEqual("npm", build_then_publish.provider_id)
        self.assertEqual([], build_then_publish.blockers)
        self.assertTrue(detect_release_intent("Can you publish this package?").requested)
        self.assertTrue(detect_release_intent("npmに公開してください").requested)
        self.assertTrue(detect_release_intent("Release it to npm.").requested)

    def test_explicit_release_request_authorizes_only_resolved_target(self):
        intent = intent_with_target()
        self.assertTrue(intent.requested)
        self.assertEqual("npm", intent.provider_id)
        self.assertEqual("@acme/widget", intent.app_or_package)
        self.assertEqual("1.2.3", intent.version)
        self.assertEqual("beta", intent.channel)
        self.assertEqual([], intent.blockers)

    def test_unknown_or_conflicting_target_returns_blocker(self):
        unknown = detect_release_intent(
            "Publish it now.",
            {"publishing_targets": ["unknown_registry"], "app_or_package": "demo", "version": "1.0", "channel": "stable"},
        )
        self.assertTrue(unknown.requested)
        self.assertIsNone(unknown.provider_id)
        self.assertTrue(any("provider" in blocker.lower() for blocker in unknown.blockers))

        conflicting = detect_release_intent(
            "Release the app to GitHub and npm.",
            {"app_or_package": "demo", "version": "1.0", "channel": "stable"},
        )
        self.assertTrue(conflicting.requested)
        self.assertIsNone(conflicting.provider_id)
        self.assertTrue(any("conflict" in blocker.lower() or "ambiguous" in blocker.lower()
                            for blocker in conflicting.blockers))

        explicit_unknown = detect_release_intent(
            "Publish it to FooStore.",
            {"publishing_targets": ["npm"], "app_or_package": "demo", "version": "1.0", "channel": "stable"},
        )
        self.assertIsNone(explicit_unknown.provider_id)
        self.assertTrue(any("foostore" in blocker.lower() for blocker in explicit_unknown.blockers))

    def test_current_user_target_values_override_stale_project_candidates(self):
        intent = detect_release_intent(
            "Publish package name @new/widget version 2.0.0 to npm under beta.",
            {
                "publishing_targets": ["npm"],
                "app_or_package": "@old/widget",
                "version": "1.0.0",
                "channel": "stable",
            },
        )
        self.assertEqual("@new/widget", intent.app_or_package)
        self.assertEqual("2.0.0", intent.version)
        self.assertEqual("beta", intent.channel)
        self.assertEqual([], intent.blockers)

    def test_current_release_intent_is_saved_in_canonical_and_normalized_requirements(self):
        facts = {
            "publishing_targets": ["npm"],
            "app_or_package": "@acme/widget",
            "version": "1.2.3",
            "channel": "beta",
        }
        canonical = canonical_coding_requirements("Publish the package on npm.", project_facts=facts)
        self.assertTrue(canonical["release_intent"]["requested"])
        self.assertEqual("npm", canonical["release_intent"]["provider_id"])

        normalized = normalize_coding_requirements(
            {
                "current_user_text": "Publish the package on npm.",
                "project_metadata": facts,
                "model_context": {"release_intent": {"requested": True, "provider_id": "pypi"}},
            }
        )
        self.assertEqual(canonical["release_intent"], normalized["release_intent"])


class ReleasePreflightTests(unittest.TestCase):
    def test_missing_current_official_requirements_blocks_release(self):
        result = build_release_preflight(
            intent_with_target(),
            current_references=references("terms"),
            external_access_enabled=True,
            agreement_accepted_by_user=True,
            credentials_configured=True,
            local_build_validated=True,
            now=1_900_000_100.0,
        )
        self.assertEqual("BLOCKED", result.status)
        self.assertTrue(any("publishing" in blocker.lower() for blocker in result.blockers))

    def test_external_access_off_or_missing_agreement_blocks_dependent_publish(self):
        result = build_release_preflight(
            intent_with_target(),
            current_references=references("publishing", "terms"),
            external_access_enabled=False,
            agreement_accepted_by_user=False,
            credentials_configured=True,
            local_build_validated=True,
            now=1_900_000_100.0,
        )
        self.assertEqual("BLOCKED", result.status)
        self.assertTrue(any("external access" in blocker.lower() for blocker in result.blockers))
        self.assertTrue(any("agreement" in blocker.lower() or "terms" in blocker.lower()
                            for blocker in result.blockers))

    def test_price_terms_and_attestations_require_user_action(self):
        result = build_release_preflight(
            intent_with_target(),
            current_references=references("publishing", "terms"),
            external_access_enabled=True,
            agreement_accepted_by_user=True,
            credentials_configured=True,
            local_build_validated=True,
            paid_plan_choice_required=True,
            required_attestations=("ownership", "privacy disclosures"),
            completed_attestations=("ownership",),
            now=1_900_000_100.0,
        )
        self.assertEqual("BLOCKED", result.status)
        self.assertTrue(any("price" in blocker.lower() or "plan" in blocker.lower()
                            for blocker in result.blockers))
        self.assertTrue(any("privacy disclosures" in blocker for blocker in result.blockers))

    def test_candidate_or_stale_references_do_not_satisfy_current_requirements(self):
        candidate = build_release_preflight(
            intent_with_target(),
            current_references=references("publishing", "terms", officiality="CANDIDATE"),
            external_access_enabled=True,
            agreement_accepted_by_user=True,
            credentials_configured=True,
            local_build_validated=True,
            now=1_900_000_100.0,
        )
        stale = build_release_preflight(
            intent_with_target(),
            current_references=references("publishing", "terms", accessed_at=1_800_000_000.0),
            external_access_enabled=True,
            agreement_accepted_by_user=True,
            credentials_configured=True,
            local_build_validated=True,
            now=1_900_000_100.0,
        )
        self.assertEqual("BLOCKED", candidate.status)
        self.assertEqual("BLOCKED", stale.status)

    def test_ready_requires_every_explicit_release_gate(self):
        result = build_release_preflight(
            intent_with_target(),
            current_references=references("publishing", "terms"),
            external_access_enabled=True,
            agreement_accepted_by_user=True,
            credentials_configured=True,
            local_build_validated=True,
            now=1_900_000_100.0,
        )
        self.assertEqual("READY", result.status)
        self.assertEqual([], result.blockers)


if __name__ == "__main__":
    unittest.main()
