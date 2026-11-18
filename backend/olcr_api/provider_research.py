"""Task-time official reference discovery, independent of chat web routing.

Only canonical public deployment labels leave the process. Page bodies, titles,
search snippets, and exception messages never enter the evidence record. A
VERIFIED reference establishes source provenance and live retrieval only; it
is not a legal conclusion, publication permission, or acceptance of terms.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from . import web
from .provider_references import ProviderReference, ProviderReferenceRegistry, official_host_matches


@dataclass(frozen=True)
class ResearchPolicy:
    external_access_enabled: bool
    web_mode: Literal["auto", "manual", "off"]
    web_provider: str


@dataclass(frozen=True)
class ProviderEvidence:
    task_id: str
    provider_id: str
    category: str
    title: str
    url: str
    host: str
    officiality: Literal["VERIFIED", "CANDIDATE"]
    accessed_at: float
    applicability: str
    summary: str
    phase_id: str | None = None
    plan_revision: int | None = None


@dataclass(frozen=True)
class ResearchResult:
    status: Literal["COMPLETE", "PARTIAL", "DISABLED", "UNAVAILABLE"]
    providers: list[str]
    references: list[ProviderEvidence]
    blockers: list[str]


Search = Callable[[str, int], list[dict]]
Fetch = Callable[[str], dict]
_CURRENT_WORK = re.compile(
    r"\b(releas\w*|publish\w*|deploy\w*|signing|notari\w*|submit\w*|"
    r"latest|current|requirements?|terms|privacy|api|integrat\w*)\b|"
    r"公開|配布|リリース|申請|署名|公証|最新|現行|利用規約|プライバシー", re.I,
)
_CATEGORY_WORDS = {"technical_spec": "technical specification", "api": "API documentation",
                   "terms": "terms of service", "privacy": "privacy policy", "review": "review guidelines",
                   "publishing": "publishing requirements", "signing": "signing notarization requirements"}
_CATEGORY_HINTS = {"terms": ("terms", "agreement", "legal"), "privacy": ("privacy",),
                   "review": ("review", "guidelines", "policy"),
                   "publishing": ("publish", "release", "deploy", "distribution", "upload"),
                   "signing": ("sign", "notari", "certificate"),
                   "api": ("api", "rest", "reference"),
                   "technical_spec": ("docs", "documentation", "spec", "developer")}


def _public_label(value: object) -> str | None:
    # publishing_targets is a public-provider field, never a project/package
    # field. Reject path, URL, org/package, credential, and free-text shapes.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+){0,3}", value):
        return None
    return value if len(value) <= 50 else None


def _source_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            return None
        # Tracking or credentials in snippets/URL queries are not evidence.
        return urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))
    except ValueError:
        return None


class ProviderResearchService:
    def __init__(self, *, registry: ProviderReferenceRegistry | None = None,
                 search: Search | None = None, provider_searches: Mapping[str, Search] | None = None,
                 fetch: Fetch | None = None, clock: Callable[[], float] = time.time):
        self.registry = registry if registry is not None else ProviderReferenceRegistry.load()
        self.search = search if search is not None else web.search
        self.provider_searches = dict(provider_searches) if provider_searches is not None else {
            "brave": web.brave_search, "tavily": web.tavily_search,
        }
        self.fetch = fetch
        self.clock = clock

    def research(self, task_id: str, goal: str, project_facts: dict[str, object], policy: ResearchPolicy,
                 *, explicit_current_lookup: bool = False, phase_id: str | None = None,
                 plan_revision: int | None = None) -> ResearchResult:
        if (not policy.external_access_enabled or policy.web_mode not in {"auto", "manual"}
                or (policy.web_mode == "manual" and not explicit_current_lookup)
                or (not explicit_current_lookup and not _CURRENT_WORK.search(goal))):
            return ResearchResult("DISABLED", [], [], [])

        providers = self.registry.for_task(goal, project_facts)
        unknown = self._unknown_targets(project_facts)
        ids = [provider.provider_id for provider in providers] + list(unknown)
        if not ids:
            return ResearchResult("UNAVAILABLE", [], [], ["No public provider could be identified for current research."])
        # Never silently switch away from the user's configured search provider.
        search = self.search if policy.web_provider in {"duckduckgo", "ddg", "html"} else self.provider_searches.get(policy.web_provider)
        evidence: list[ProviderEvidence] = []
        blockers: list[str] = []

        def record(provider_id: str, category: str, url: str, official: bool) -> None:
            host = urlsplit(url).hostname or ""
            kind = "VERIFIED" if official else "CANDIDATE"
            summary = (f"Live official {_CATEGORY_WORDS[category]} reference retrieved. "
                       "Source provenance only; requirements and terms need review, with no publication authorization."
                       if official else f"Unverified {_CATEGORY_WORDS[category]} discovery candidate; official provenance remains unconfirmed.")
            evidence.append(ProviderEvidence(
                task_id, provider_id, category, f"{provider_id}: {_CATEGORY_WORDS[category]}", url, host, kind,
                self.clock(), f"Current task {_CATEGORY_WORDS[category]} reference", summary, phase_id, plan_revision,
            ))

        def discover(query: str) -> list[dict]:
            if search is None:
                return []
            try:
                rows = search(query, 5)
                return [row for row in rows[:5] if isinstance(row, dict)] if isinstance(rows, list) else []
            except Exception:
                # External exception strings can contain credentials or query data.
                return []

        for provider in providers:
            required = set(document.category for document in provider.documents)
            required.add("privacy")
            if re.search(r"\b(api|integrat\w*|specification|version)\b|仕様|バージョン|連携", goal, re.I):
                required.update(("api", "technical_spec"))
            succeeded: set[str] = set()
            seen: set[str] = set()
            for document in provider.documents:
                if self._live_official(document.url, provider):
                    record(provider.provider_id, document.category, document.url, True)
                    succeeded.add(document.category)
                seen.add(document.url)
            for category in sorted(required - succeeded):
                scope = " OR ".join(f"site:{host}" for host in provider.official_hosts)
                query = f"{provider.display_name} {scope} official {_CATEGORY_WORDS[category]}"
                for row in discover(query):
                    url = _source_url(row.get("url"))
                    if (not url or url in seen
                            or not official_host_matches(urlsplit(url).hostname or "", provider.official_hosts)
                            or not any(hint in urlsplit(url).path.casefold() for hint in _CATEGORY_HINTS[category])):
                        continue
                    seen.add(url)
                    if self._live_official(url, provider):
                        record(provider.provider_id, category, url, True)
                        succeeded.add(category)
                        break
            blockers.extend(f"{provider.provider_id}: current official {category} source unavailable."
                            for category in sorted(required - succeeded))

        for provider_id, label in unknown.items():
            for category in ("technical_spec", "terms"):
                seen = set()
                for row in discover(f"{label} official developer {_CATEGORY_WORDS[category]}"):
                    url = _source_url(row.get("url"))
                    if url and url not in seen:
                        record(provider_id, category, url, False)
                        seen.add(url)
            blockers.append(f"{provider_id}: official provenance and current release requirements remain unverified.")

        verified = any(ref.officiality == "VERIFIED" for ref in evidence)
        status = "COMPLETE" if not blockers else "PARTIAL" if verified else "UNAVAILABLE"
        return ResearchResult(status, ids, evidence, blockers)

    def _unknown_targets(self, project_facts: dict[str, object]) -> dict[str, str]:
        values = project_facts.get("publishing_targets")
        unknown = {}
        if isinstance(values, (list, tuple)):
            for value in values[:10]:
                label = _public_label(value)
                if label and not self.registry.for_task("", {"publishing_targets": [label]}):
                    provider_id = re.sub(r"[ -]+", "_", label.casefold())
                    unknown[provider_id] = label
        return unknown

    def _live_official(self, url: str, provider: ProviderReference) -> bool:
        if not _source_url(url) or not official_host_matches(urlsplit(url).hostname or "", provider.official_hosts):
            return False
        try:
            page = (self.fetch(url) if self.fetch is not None
                    else web.fetch(url, allowed_hosts=provider.official_hosts))
            if not isinstance(page, dict):
                return False
            final = _source_url(page.get("final_url", url))
            status = page.get("http_status", 200)
            body = page.get("clean_markdown") or page.get("text")
            return bool(final and official_host_matches(urlsplit(final).hostname or "", provider.official_hosts)
                        and isinstance(status, int) and 200 <= status < 300
                        and isinstance(body, str) and body.strip())
        except Exception:
            return False
