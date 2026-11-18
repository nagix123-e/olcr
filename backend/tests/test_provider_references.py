"""Official reference selection is local, typed, and restricted to reviewed hosts."""
import copy
import json
from urllib.parse import urlsplit

import pytest


EXPECTED = {
    "github": (
        ("docs.github.com", "github.com"),
        {
            ("https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository", "publishing"),
            ("https://docs.github.com/en/rest/releases", "api"),
            ("https://docs.github.com/en/site-policy/github-terms/github-terms-of-service", "terms"),
        },
    ),
    "apple_macos": (
        ("developer.apple.com",),
        {
            ("https://developer.apple.com/macos/distribution", "publishing"),
            ("https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/", "signing"),
            ("https://developer.apple.com/app-store/review/guidelines/", "review"),
            ("https://developer.apple.com/support/terms", "terms"),
            ("https://developer.apple.com/help/app-store-connect/manage-agreements/sign-and-update-agreements", "terms"),
        },
    ),
    "app_store_connect": (
        ("developer.apple.com",),
        {
            ("https://developer.apple.com/app-store/review/guidelines/", "review"),
            ("https://developer.apple.com/support/terms", "terms"),
            ("https://developer.apple.com/help/app-store-connect/manage-agreements/sign-and-update-agreements", "terms"),
        },
    ),
    "google_play": (
        ("support.google.com", "play.google", "googleplay.com"),
        {
            ("https://support.google.com/googleplay/android-developer/answer/9859348?hl=en", "publishing"),
            ("https://play.google/developer-content-policy/", "review"),
            ("https://support.google.com/googleplay/android-developer/answer/6112435?hl=en", "publishing"),
            ("https://play.google/developer-distribution-agreement.html", "terms"),
        },
    ),
    "npm": (
        ("docs.npmjs.com",),
        {
            ("https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages/", "publishing"),
            ("https://docs.npmjs.com/package-scope-access-level-and-visibility/", "publishing"),
            ("https://docs.npmjs.com/policies/terms/", "terms"),
        },
    ),
    "pypi": (
        ("docs.pypi.org", "packaging.python.org", "policies.python.org"),
        {
            ("https://docs.pypi.org/publishing/publishing-to-pypi/", "publishing"),
            ("https://docs.pypi.org/trusted-publishers/", "api"),
            ("https://packaging.python.org/en/latest/tutorials/packaging-projects/", "technical_spec"),
            ("https://policies.python.org/pypi.org/Terms-of-Service/", "terms"),
        },
    ),
    "vercel": (
        ("vercel.com",),
        {
            ("https://vercel.com/docs/deployments", "publishing"),
            ("https://vercel.com/docs/projects/deploy-from-cli", "publishing"),
            ("https://vercel.com/legal/terms", "terms"),
            ("https://vercel.com/legal", "terms"),
        },
    ),
    "cloudflare_pages": (
        ("developers.cloudflare.com", "cloudflare.com"),
        {
            ("https://developers.cloudflare.com/pages/get-started/direct-upload/", "publishing"),
            ("https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/", "publishing"),
            ("https://www.cloudflare.com/policies/terms/", "terms"),
        },
    ),
}


def load_catalog(tmp_path, catalog):
    from olcr_api.provider_references import ProviderReferenceRegistry

    path = tmp_path / "references.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return ProviderReferenceRegistry.load(path)


@pytest.fixture
def small_catalog():
    return {"registered_on": "2026-11-18", "providers": [{
        "provider_id": "github", "display_name": "GitHub Releases",
        "official_hosts": ["github.com", "docs.github.com"],
        "documents": [{"url": "https://docs.github.com/en/rest/releases",
                       "category": "api", "work_kinds": ["release"],
                       "platforms": ["any"]}],
    }]}


def test_catalog_has_all_initial_providers_and_official_documents():
    from olcr_api.provider_references import ProviderReferenceRegistry

    registry = ProviderReferenceRegistry.load()
    providers = {provider.provider_id: provider for provider in registry.providers}
    assert set(providers) == set(EXPECTED)
    for provider_id, (hosts, documents) in EXPECTED.items():
        provider = providers[provider_id]
        assert provider.display_name
        assert provider.official_hosts == hosts
        assert {(doc.url, doc.category) for doc in provider.documents} == documents
        for document in provider.documents:
            assert urlsplit(document.url).scheme == "https"
            assert document.work_kinds
            assert document.platforms


@pytest.mark.parametrize("changes", [
    {"category": "advertisement"},
    {"url": "http://docs.github.com/en/rest/releases"},
    {"url": "file:///tmp/secret"},
    {"url": "https://notgithub.com/releases"},
    {"url": "https://github.com.evil.example/releases"},
    {"url": "https://github.com@evil.example/releases"},
    {"url": "https://preview.docs.github.com/releases"},
    {"url": "https://user:secret@github.com/releases"},
    {"url": "https://github.com:9443/releases"},
    {"url": "https://github.com/\nreleases"},
])
def test_catalog_rejects_unknown_categories_and_non_https_urls(tmp_path, small_catalog, changes):
    small_catalog["providers"][0]["documents"][0].update(changes)
    # The docs subdomain must not expand just because its parent is listed in
    # a different provider: only an explicitly listed registrable root expands.
    if "preview.docs.github.com" in changes.get("url", ""):
        small_catalog["providers"][0]["official_hosts"] = ["docs.github.com"]
    with pytest.raises(ValueError):
        load_catalog(tmp_path, small_catalog)


def test_catalog_rejects_duplicate_provider_ids(tmp_path, small_catalog):
    small_catalog["providers"].append(copy.deepcopy(small_catalog["providers"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(tmp_path, small_catalog)


@pytest.mark.parametrize("url", [
    "https://github.com/releases", "https://api.github.com/releases",
    "https://API.GITHUB.COM/releases", "https://docs.github.com/en/rest/releases",
])
def test_catalog_accepts_reviewed_hosts_and_registrable_subdomains(tmp_path, small_catalog, url):
    small_catalog["providers"][0]["documents"][0]["url"] = url
    registry = load_catalog(tmp_path, small_catalog)
    assert registry.providers[0].documents[0].url == url


def test_privacy_is_valid_without_inventing_initial_privacy_urls(tmp_path, small_catalog):
    small_catalog["providers"][0]["documents"][0]["category"] = "privacy"
    registry = load_catalog(tmp_path, small_catalog)
    assert registry.providers[0].documents[0].category == "privacy"


@pytest.mark.parametrize("bad", [
    [], {"providers": "github"}, {"providers": [None]},
    {"providers": [{"provider_id": "github"}]},
])
def test_catalog_rejects_invalid_structure(tmp_path, bad):
    with pytest.raises(ValueError):
        load_catalog(tmp_path, bad)


@pytest.mark.parametrize("field,value", [
    ("official_hosts", ["https://github.com"]), ("official_hosts", ["com"]),
    ("official_hosts", []), ("display_name", ""), ("documents", []),
])
def test_catalog_rejects_invalid_provider_metadata(tmp_path, small_catalog, field, value):
    small_catalog["providers"][0][field] = value
    with pytest.raises(ValueError):
        load_catalog(tmp_path, small_catalog)


@pytest.mark.parametrize("field,value", [
    ("work_kinds", "release"), ("work_kinds", []),
    ("platforms", [1]), ("platforms", []),
])
def test_catalog_rejects_invalid_document_metadata(tmp_path, small_catalog, field, value):
    small_catalog["providers"][0]["documents"][0][field] = value
    with pytest.raises(ValueError):
        load_catalog(tmp_path, small_catalog)


@pytest.mark.parametrize("goal,facts,expected", [
    ("Prepare GitHub Releases", {}, ["github"]),
    ("macOS アプリを署名する", {}, ["apple_macos"]),
    ("Upload to TestFlight", {}, ["app_store_connect"]),
    ("Google Play に公開", {}, ["google_play"]),
    ("Publish an npm package", {}, ["npm"]),
    ("Publish on PyPI", {}, ["pypi"]),
    ("Deploy to Vercel", {}, ["vercel"]),
    ("Deploy to Cloudflare Pages", {}, ["cloudflare_pages"]),
    ("Prepare publication", {"publishing_targets": ["app_store_connect", "github"]},
     ["github", "app_store_connect"]),
    ("Update dependencies", {"manifest_names": ["package.json", "pyproject.toml"]}, ["npm", "pypi"]),
    ("Update dependencies", {"manifest_names": ["setup.py", "tauri.conf.json"]}, ["apple_macos", "pypi"]),
    ("Implement feature", {"technologies": ["SwiftUI", "Android"]}, ["apple_macos", "google_play"]),
    ("Update file", {"manifest_names": ["Cargo.toml"]}, []),
    ("Fix typo", {}, []),
    ("Use mynpmclient and notgithub.com", {}, []),
])
def test_for_task_selects_known_providers_from_public_facts(goal, facts, expected):
    from olcr_api.provider_references import ProviderReferenceRegistry

    registry = ProviderReferenceRegistry.load()
    assert [ref.provider_id for ref in registry.for_task(goal, facts)] == expected


def test_for_task_ignores_paths_contents_and_unknown_metadata(tmp_path):
    from olcr_api.provider_references import ProviderReferenceRegistry

    # A real private file exists; neither its path nor its contents are signals.
    private = tmp_path / "package.json"
    private.write_text('"Deploy to Vercel"', encoding="utf-8")
    facts = {
        "workspace_path": str(private.parent), "file_contents": "Publish on GitHub",
        "project_name": "Cloudflare Pages", "private_package": "npm",
        "manifest_names": [str(private), "private-package.json"],
        "technologies": ["private-python-project"],
        "publishing_targets": ["https://vercel.com/private-project"],
    }
    assert ProviderReferenceRegistry.load().for_task("Fix typo", facts) == []


def test_for_task_deduplicates_and_returns_catalog_metadata_only():
    from dataclasses import asdict
    from olcr_api.provider_references import ProviderReferenceRegistry

    registry = ProviderReferenceRegistry.load()
    selected = registry.for_task("npm", {"manifest_names": ["package.json"],
                                       "publishing_targets": ["npm", "npm"],
                                       "workspace_path": "/private/secret"})
    assert [provider.provider_id for provider in selected] == ["npm"]
    assert set(asdict(selected[0])) == {"provider_id", "display_name", "official_hosts", "documents"}
    assert "/private/secret" not in json.dumps(asdict(selected[0]))


@pytest.mark.parametrize("host,hosts,expected", [
    ("api.github.com", ("github.com",), True),
    ("API.GITHUB.COM", ("github.com",), True),
    ("notgithub.com", ("github.com",), False),
    ("github.com.evil.example", ("github.com",), False),
    ("preview.docs.github.com", ("docs.github.com",), False),
    ("developer.apple.com", ("developer.apple.com",), True),
    ("preview.developer.apple.com", ("developer.apple.com",), False),
    ("www.cloudflare.com", ("cloudflare.com",), True),
    ("www.vercel.com", ("vercel.com",), True),
    ("console.play.google", ("play.google",), True),
    ("console.googleplay.com", ("googleplay.com",), True),
    ("", ("github.com",), False),
])
def test_official_host_matching_respects_dns_boundaries(host, hosts, expected):
    from olcr_api.provider_references import official_host_matches

    assert official_host_matches(host, hosts) is expected
