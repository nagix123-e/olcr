"""Reviewed official links, independent of the offline coding-knowledge index.

Registration dates inventory links only. They do not establish that terms or
publishing requirements are current; those sources need a live task-time check.
Loading and candidate selection perform no network or workspace inspection.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Literal, cast
from urllib.parse import urlsplit


DocumentCategory = Literal[
    "technical_spec", "api", "terms", "privacy", "review", "publishing", "signing"
]
_CATEGORIES = {"technical_spec", "api", "terms", "privacy", "review", "publishing", "signing"}
# Only reviewed registrable roots expand to subdomains. Do not guess public
# suffixes from label counts: a host such as developer.apple.com is exact-only.
_REGISTRABLE_ROOTS = {"github.com", "play.google", "googleplay.com", "vercel.com", "cloudflare.com"}
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\Z")


@dataclass(frozen=True)
class ProviderDocument:
    url: str
    category: DocumentCategory
    work_kinds: tuple[str, ...]
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class ProviderReference:
    provider_id: str
    display_name: str
    official_hosts: tuple[str, ...]
    documents: tuple[ProviderDocument, ...]


def official_host_matches(host: str, official_hosts: Sequence[str]) -> bool:
    """Match a parsed DNS hostname against exact hosts and reviewed root domains."""
    normalized = host.lower()
    return bool(_HOST.fullmatch(normalized)) and any(
        normalized == allowed or (
            allowed in _REGISTRABLE_ROOTS and normalized.endswith("." + allowed)
        )
        for allowed in official_hosts
    )


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"invalid provider reference {field}")
    return tuple(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid provider reference {field}")
    return value


def _document(value: object, hosts: tuple[str, ...]) -> ProviderDocument:
    if not isinstance(value, dict):
        raise ValueError("invalid provider reference document")
    category = _text(value.get("category"), "category")
    if category not in _CATEGORIES:
        raise ValueError("unknown provider reference category")
    url = _text(value.get("url"), "url")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("invalid provider reference URL")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https" and parsed.hostname is not None
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443)
            and official_host_matches(parsed.hostname, hosts)
        )
    except ValueError as exc:
        raise ValueError("invalid provider reference URL") from exc
    if not valid:
        raise ValueError("provider reference URL must use HTTPS and an official host")
    return ProviderDocument(url, cast(DocumentCategory, category),
                            _strings(value.get("work_kinds"), "work_kinds"),
                            _strings(value.get("platforms"), "platforms"))


# Only exact, public identifiers are read from facts. Never stringify arbitrary
# metadata or take basenames of private paths to infer a publishing destination.
_ALIASES = {
    "github": ("github", "github releases"),
    "apple_macos": ("apple_macos", "macos", "mac os", "swift", "swiftui", "tauri"),
    "app_store_connect": ("app_store_connect", "app store connect", "app store", "testflight", "ios"),
    "google_play": ("google_play", "google play", "play console", "android"),
    "npm": ("npm", "node", "nodejs", "node.js", "javascript", "typescript"),
    "pypi": ("pypi", "python"),
    "vercel": ("vercel",),
    "cloudflare_pages": ("cloudflare_pages", "cloudflare pages"),
}
_MANIFESTS = {
    "package.json": "npm", "pyproject.toml": "pypi", "setup.py": "pypi",
    "setup.cfg": "pypi", "tauri.conf.json": "apple_macos",
    "tauri.conf.json5": "apple_macos", "androidmanifest.xml": "google_play",
    "vercel.json": "vercel", "wrangler.toml": "cloudflare_pages",
    "wrangler.json": "cloudflare_pages", "wrangler.jsonc": "cloudflare_pages",
}


@dataclass(frozen=True)
class ProviderReferenceRegistry:
    providers: tuple[ProviderReference, ...]
    registered_on: str

    @classmethod
    def load(cls, path: Path | None = None) -> ProviderReferenceRegistry:
        """Load packaged links or an explicit catalog, rejecting malformed entries."""
        source = path if path is not None else Path(__file__).with_name("provider_references.json")
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("providers"), list):
            raise ValueError("invalid provider reference catalog")
        registered_on = _text(value.get("registered_on"), "registered_on")
        date.fromisoformat(registered_on)
        providers = []
        seen = set()
        for row in value["providers"]:
            if not isinstance(row, dict):
                raise ValueError("invalid provider reference provider")
            provider_id = _text(row.get("provider_id"), "provider_id")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", provider_id):
                raise ValueError("invalid provider reference provider_id")
            if provider_id in seen:
                raise ValueError("duplicate provider reference provider_id")
            seen.add(provider_id)
            hosts = _strings(row.get("official_hosts"), "official_hosts")
            if any(not _HOST.fullmatch(host) for host in hosts):
                raise ValueError("invalid provider reference official_hosts")
            documents = row.get("documents")
            if not isinstance(documents, list) or not documents:
                raise ValueError("invalid provider reference documents")
            providers.append(ProviderReference(
                provider_id, _text(row.get("display_name"), "display_name"), hosts,
                tuple(_document(document, hosts) for document in documents),
            ))
        return cls(tuple(providers), registered_on)

    def for_task(self, goal: str, project_facts: dict[str, object]) -> list[ProviderReference]:
        """Select deterministic candidates from a goal and public identifier lists.

        Recognized facts: publishing_targets, manifest_names, technologies. Each
        is a sequence of public identifiers; all other metadata is ignored.
        Candidate selection does not authorize publication or verify currency.
        """
        selected = set()
        identifiers = set()
        for key in ("publishing_targets", "technologies"):
            values = project_facts.get(key)
            if isinstance(values, (list, tuple)):
                identifiers.update(item.casefold() for item in values if isinstance(item, str))
        manifests = project_facts.get("manifest_names")
        if isinstance(manifests, (list, tuple)):
            selected.update(_MANIFESTS[name.casefold()] for name in manifests
                            if isinstance(name, str) and name.casefold() in _MANIFESTS)
        for provider_id, aliases in _ALIASES.items():
            if any(alias in identifiers or re.search(
                r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", goal, re.IGNORECASE
            ) for alias in aliases):
                selected.add(provider_id)
        return [provider for provider in self.providers if provider.provider_id in selected]
