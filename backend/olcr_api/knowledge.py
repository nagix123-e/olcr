"""Immutable, curated Coding Knowledge Index.

The Coding Knowledge Index is release content.  It never receives workspace
contents, conversation history, or completed Coding Task output.  The only
runtime write-free operation is embedding a current subtask query and comparing
it to vectors embedded during the release build.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Protocol

INDEX_VERSION = "olcr-coding-knowledge-v1"
EXPECTED_EMBEDDING_MODEL = "embeddinggemma:latest"
MAX_DETAILED_RESULTS = 5
DOMAINS = frozenset({"architecture", "security", "testing", "uiux"})
STACKS = frozenset({
    "general", "html_css", "javascript", "typescript", "react", "nodejs",
    "python", "fastapi", "sql", "sqlite", "postgresql", "rust", "tauri",
})
SOURCE_REVIEW_STATES = frozenset({"APPROVED", "REJECTED", "NEEDS_REVIEW", "DEPRECATED"})
RECORD_FIELDS = frozenset({
    "knowledge_id", "content", "domain", "stack", "topic", "source_type",
    "source", "source_version", "applicable_versions", "reviewed_at",
    "confidence", "deprecated", "embedding", "content_hash",
})
SEED_FIELDS = RECORD_FIELDS - {"knowledge_id", "embedding", "content_hash"}


class KnowledgeBuildError(ValueError):
    """The release artifact cannot be safely created."""


class KnowledgeUnavailable(RuntimeError):
    """A read-only index cannot safely answer this retrieval."""


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str], model: str) -> list[list[float]]: ...


def normalized_content(value: str) -> str:
    return " ".join(value.strip().split())


def content_hash(value: str) -> str:
    return hashlib.sha256(normalized_content(value).encode("utf-8")).hexdigest()


def stable_knowledge_id(record: dict[str, Any]) -> str:
    """Stable across builds while the semantic record remains the same."""
    canonical = {
        "content": normalized_content(str(record.get("content", ""))),
        "domain": record.get("domain"),
        "stack": record.get("stack"),
        "topic": record.get("topic"),
        "source": record.get("source"),
        "source_version": record.get("source_version"),
        "applicable_versions": record.get("applicable_versions"),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cki-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_content_fingerprint(source: dict[str, Any]) -> str:
    """Fingerprint the reviewed source descriptor used by this release input."""
    material = "|".join(str(source[key]) for key in ("source_id", "publisher", "title", "canonical_source", "version", "retrieved_at"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _valid_iso_date(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?", value))


def _token_set(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{3,}", value.lower()))


def near_duplicate_pairs(records: Iterable[dict[str, Any]], threshold: float = 0.82) -> list[tuple[str, str]]:
    """Deterministic, explainable pre-embedding duplicate gate; no model call."""
    rows = list(records)
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(rows):
        left_words = _token_set(str(left.get("content", "")))
        for right in rows[index + 1:]:
            right_words = _token_set(str(right.get("content", "")))
            if not left_words or not right_words:
                continue
            similarity = len(left_words & right_words) / len(left_words | right_words)
            if similarity >= threshold:
                pairs.append((str(left.get("knowledge_id", "")), str(right.get("knowledge_id", ""))))
    return pairs


def _conflicts(records: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """Catch explicitly marked contradictory statements; semantic adjudication is forbidden."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (str(record.get("domain")), str(record.get("stack")), json.dumps(record.get("applicable_versions"), sort_keys=True))
        groups.setdefault(key, []).append(record)
    conflicts: list[tuple[str, str]] = []
    for group in groups.values():
        for index, left in enumerate(group):
            opposite = str(left.get("conflicts_with", ""))
            for right in group[index + 1:]:
                if opposite and opposite == right.get("topic"):
                    conflicts.append((str(left.get("knowledge_id")), str(right.get("knowledge_id"))))
    return conflicts


def validate_source_manifest(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise KnowledgeBuildError("source manifest must contain sources")
    ids: set[str] = set()
    approved: list[dict[str, Any]] = []
    for source in value["sources"]:
        required = {"source_id", "domain", "publisher", "title", "canonical_source", "source_type", "version", "retrieved_at", "license_or_usage_note", "content_hash", "review_status"}
        if not isinstance(source, dict) or not required <= source.keys():
            raise KnowledgeBuildError("source manifest record is incomplete")
        source_id = source["source_id"]
        if not isinstance(source_id, str) or source_id in ids:
            raise KnowledgeBuildError("source ids must be unique")
        ids.add(source_id)
        if source["domain"] not in DOMAINS or source["review_status"] not in SOURCE_REVIEW_STATES:
            raise KnowledgeBuildError("source has unknown domain or review state")
        if not str(source["canonical_source"]).startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", str(source["content_hash"])) or source["content_hash"] != source_content_fingerprint(source):
            raise KnowledgeBuildError("source provenance is invalid")
        if source["review_status"] == "APPROVED":
            approved.append(source)
    return approved


def _validate_seed(record: dict[str, Any], approved_ids: set[str]) -> None:
    if not isinstance(record, dict) or not SEED_FIELDS <= record.keys():
        raise KnowledgeBuildError("knowledge record is incomplete")
    if record["domain"] not in DOMAINS or record["stack"] not in STACKS:
        raise KnowledgeBuildError("knowledge record has unknown domain or stack")
    if record["source"] not in approved_ids:
        raise KnowledgeBuildError("knowledge record refers to an unapproved source")
    if not normalized_content(str(record["content"])) or len(normalized_content(str(record["content"]))) > 1400:
        raise KnowledgeBuildError("knowledge content is empty or excessive")
    if re.search(r"\b(?:todo|placeholder|fixture)\b", str(record["content"]), re.I):
        raise KnowledgeBuildError("production knowledge must not contain placeholders")
    if not _valid_iso_date(record["reviewed_at"]) or not isinstance(record["applicable_versions"], dict):
        raise KnowledgeBuildError("knowledge version metadata is invalid")
    if record["deprecated"] and not record["applicable_versions"]:
        raise KnowledgeBuildError("deprecated record needs applicable version metadata")
    if not isinstance(record["confidence"], (int, float)) or not 0 < float(record["confidence"]) <= 1:
        raise KnowledgeBuildError("knowledge confidence is invalid")


def validate_production_index(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise KnowledgeBuildError("index must be an object")
    metadata, core_rules, records = value.get("metadata"), value.get("core_rules"), value.get("records")
    required_metadata = {"knowledge_index_version", "created_at", "embedding_model", "embedding_model_version_or_identity", "embedding_dimension", "record_count", "core_rule_count", "content_manifest_hash", "source_manifest_hash", "target_olcr_version"}
    if not isinstance(metadata, dict) or not required_metadata <= metadata.keys() or metadata.get("knowledge_index_version") != INDEX_VERSION:
        raise KnowledgeBuildError("index metadata is invalid")
    if not isinstance(core_rules, list) or not isinstance(records, list) or metadata["record_count"] != len(records) or metadata["core_rule_count"] != len(core_rules):
        raise KnowledgeBuildError("index count metadata is invalid")
    dimension = metadata.get("embedding_dimension")
    if not isinstance(dimension, int) or dimension <= 0 or metadata.get("embedding_model") != EXPECTED_EMBEDDING_MODEL:
        raise KnowledgeBuildError("index embedding metadata is invalid")
    seen_ids: set[str] = set(); seen_hashes: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not RECORD_FIELDS <= record.keys():
            raise KnowledgeBuildError("index record is incomplete")
        _validate_seed(record, {str(record["source"])})  # schema-only; approval was enforced at build time
        if record["knowledge_id"] != stable_knowledge_id(record) or record["content_hash"] != content_hash(record["content"]):
            raise KnowledgeBuildError("record identity or content integrity failed")
        if record["knowledge_id"] in seen_ids or record["content_hash"] in seen_hashes:
            raise KnowledgeBuildError("duplicate record")
        if not isinstance(record["embedding"], list) or len(record["embedding"]) != dimension or any(not isinstance(x, (int, float)) for x in record["embedding"]):
            raise KnowledgeBuildError("record embedding is invalid")
        seen_ids.add(record["knowledge_id"]); seen_hashes.add(record["content_hash"])
    for rule in core_rules:
        if not isinstance(rule, dict) or not {"knowledge_id", "content", "domain", "stack", "mandatory", "source", "reviewed_at", "content_hash"} <= rule.keys():
            raise KnowledgeBuildError("core rule is incomplete")
        if not rule["mandatory"] or rule["domain"] not in DOMAINS | {"general"} or rule["stack"] not in STACKS:
            raise KnowledgeBuildError("core rule metadata is invalid")
        if rule["content_hash"] != content_hash(rule["content"]):
            raise KnowledgeBuildError("core rule integrity failed")
    return metadata, core_rules, records


def build_production_index(*, source_manifest: dict[str, Any], seeds: dict[str, Any], provider: EmbeddingProvider, output: Path, model_identity: str, created_at: str, target_olcr_version: str) -> dict[str, Any]:
    """Generate the one immutable release artifact.  It is intentionally offline after this call."""
    approved = validate_source_manifest(source_manifest)
    approved_ids = {source["source_id"] for source in approved}
    raw_records = seeds.get("records") if isinstance(seeds, dict) else None
    raw_rules = seeds.get("core_rules") if isinstance(seeds, dict) else None
    if not isinstance(raw_records, list) or not isinstance(raw_rules, list):
        raise KnowledgeBuildError("knowledge seeds must contain records and core_rules")
    records: list[dict[str, Any]] = []
    content_hashes: set[str] = set()
    for raw in raw_records:
        _validate_seed(raw, approved_ids)
        record = dict(raw)
        record["content"] = normalized_content(str(record["content"]))
        record["knowledge_id"] = stable_knowledge_id(record)
        record["content_hash"] = content_hash(record["content"])
        if record["content_hash"] in content_hashes:
            raise KnowledgeBuildError("exact duplicate knowledge content")
        content_hashes.add(record["content_hash"])
        records.append(record)
    if duplicate_pairs := near_duplicate_pairs(records):
        raise KnowledgeBuildError("near duplicate knowledge: " + repr(duplicate_pairs))
    if conflict_pairs := _conflicts(records):
        raise KnowledgeBuildError("unresolved knowledge conflict: " + repr(conflict_pairs))
    records.sort(key=lambda row: row["knowledge_id"])
    vectors = provider.embed([row["content"] for row in records], EXPECTED_EMBEDDING_MODEL)
    if len(vectors) != len(records) or not vectors:
        raise KnowledgeBuildError("embedding provider did not return every vector")
    dimension = len(vectors[0])
    if dimension <= 0 or any(len(vector) != dimension for vector in vectors):
        raise KnowledgeBuildError("embedding dimension mismatch")
    for record, vector in zip(records, vectors):
        record["embedding"] = [float(value) for value in vector]
    core_rules: list[dict[str, Any]] = []
    for raw in raw_rules:
        if not isinstance(raw, dict) or not {"content", "domain", "stack", "source", "reviewed_at"} <= raw.keys() or raw["source"] not in approved_ids:
            raise KnowledgeBuildError("core rule has missing or unapproved provenance")
        if raw["domain"] not in DOMAINS | {"general"} or raw["stack"] not in STACKS:
            raise KnowledgeBuildError("core rule metadata is invalid")
        rule = dict(raw, mandatory=True)
        rule["content"] = normalized_content(str(rule["content"]))
        rule["content_hash"] = content_hash(rule["content"])
        rule["knowledge_id"] = "ckr-" + hashlib.sha256((rule["domain"] + "|" + rule["stack"] + "|" + rule["content"]).encode()).hexdigest()[:20]
        core_rules.append(rule)
    core_rules.sort(key=lambda row: row["knowledge_id"])
    metadata = {
        "knowledge_index_version": INDEX_VERSION,
        "created_at": created_at,
        "embedding_model": EXPECTED_EMBEDDING_MODEL,
        "embedding_model_version_or_identity": model_identity,
        "embedding_dimension": dimension,
        "record_count": len(records),
        "core_rule_count": len(core_rules),
        "content_manifest_hash": canonical_hash({"records": [{key: value for key, value in row.items() if key != "embedding"} for row in records], "core_rules": core_rules}),
        "source_manifest_hash": canonical_hash(source_manifest),
        "target_olcr_version": target_olcr_version,
    }
    index = {"metadata": metadata, "core_rules": core_rules, "records": records}
    validate_production_index(index)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return metadata


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+(?:\.\d+){0,2})", value)
    return tuple(int(item) for item in match.group(1).split(".")) if match else None


def _matches_constraint(version: str | None, constraint: str) -> bool:
    if not version or not constraint:
        return not constraint
    actual = _version_tuple(version)
    if actual is None:
        return False
    for part in constraint.split(","):
        part = part.strip(); match = re.fullmatch(r"(>=|>|<=|<|==)?\s*(\d+(?:\.\d+){0,2})", part)
        if not match:
            return False
        target = _version_tuple(match.group(2)) or ()
        width = max(len(actual), len(target)); left = actual + (0,) * (width - len(actual)); right = target + (0,) * (width - len(target))
        op = match.group(1) or "=="
        if not {">=": left >= right, ">": left > right, "<=": left <= right, "<": left < right, "==": left == right}[op]:
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(sum(item * item for item in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else -1.0


@dataclass(frozen=True)
class KnowledgeResult:
    status: str
    core_rules: tuple[dict[str, Any], ...] = ()
    records: tuple[dict[str, Any], ...] = ()
    reason: str = ""


class KnowledgeIndex:
    """A read-only artifact loader.  There are deliberately no mutation methods."""
    def __init__(self, metadata: dict[str, Any] | list[dict[str, Any]] | None = None, core_rules: list[dict[str, Any]] | bool | None = None, records: list[dict[str, Any]] | None = None, available: bool = False, reason: str = "MISSING_INDEX"):
        # Keep the tiny constructor shape used by the earlier read-only
        # retrieval fixture (`KnowledgeIndex(entries, available)`) while all
        # production artifacts use the versioned metadata shape.
        if isinstance(metadata, list):
            self.metadata = {}; self.core_rules = []; self.entries = metadata
            self.available = bool(core_rules) if isinstance(core_rules, bool) else available
            self.reason = "AVAILABLE" if self.available else reason
        else:
            self.metadata = metadata or {}; self.core_rules = core_rules if isinstance(core_rules, list) else []; self.entries = records or []; self.available = available; self.reason = reason

    @classmethod
    def load(cls, path: str | Path) -> "KnowledgeIndex":
        candidate = Path(path)
        if not candidate.is_file():
            return cls(reason="MISSING_INDEX")
        try:
            metadata, core_rules, records = validate_production_index(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, KnowledgeBuildError) as exc:
            return cls(reason="CORRUPT_INDEX:" + type(exc).__name__)
        return cls(metadata, core_rules, records, True, "AVAILABLE")

    @property
    def state(self) -> str:
        return "AVAILABLE" if self.available else "NOT_AVAILABLE"

    def compatible(self, model: str, model_identity: str | None) -> bool:
        if not self.available or self.metadata.get("embedding_model") != model:
            return False
        expected = str(self.metadata.get("embedding_model_version_or_identity", ""))
        return bool(expected and model_identity and expected == model_identity)

    def select_core_rules(self, domains: Iterable[str], stacks: Iterable[str]) -> list[dict[str, Any]]:
        wanted_domains = set(domains) | {"general"}; wanted_stacks = set(stacks) | {"general"}
        return [dict(rule) for rule in self.core_rules if rule["domain"] in wanted_domains and rule["stack"] in wanted_stacks]

    def search(self, query_embedding: list[float], *, domains: Iterable[str] | None = None, stacks: Iterable[str] | None = None, versions: dict[str, str] | None = None, domain: str | None = None, stack: str | None = None, limit: int = MAX_DETAILED_RESULTS) -> list[dict[str, Any]]:
        if domain is not None: domains = {domain}
        if stack is not None: stacks = {stack}
        wanted_domains = set(domains or DOMAINS); wanted_stacks = set(stacks or STACKS); versions = versions or {}
        rows: list[dict[str, Any]] = []
        for record in self.entries:
            if record.get("deprecated", False) or record.get("domain") not in wanted_domains or record.get("stack") not in wanted_stacks:
                continue
            constraints = record.get("applicable_versions", {})
            if any(stack in versions and not _matches_constraint(versions[stack], requirement) for stack, requirement in constraints.items()):
                continue
            score = _cosine(query_embedding, record.get("embedding", []))
            if score >= 0:
                rows.append(dict(record, similarity=score))
        rows.sort(key=lambda item: (-item["similarity"], item.get("knowledge_id", item.get("id", ""))))
        return rows[:max(0, min(int(limit), MAX_DETAILED_RESULTS))]


def discover_repository_stacks(workspace_root: str | Path | None, request: str) -> tuple[set[str], dict[str, str]]:
    """Use only small manifest filenames/metadata for this subtask; retain nothing."""
    text = request.lower(); stacks: set[str] = set(); versions: dict[str, str] = {}
    patterns = {
        "react": r"\breact\b|\.jsx\b|\.tsx\b", "typescript": r"\btypescript\b|\.tsx?\b", "javascript": r"\bjavascript\b|\.m?js\b",
        "html_css": r"\bhtml\b|\bcss\b", "nodejs": r"\bnode(?:\.js)?\b|package\.json", "python": r"\bpython\b|\.py\b",
        "fastapi": r"\bfastapi\b", "sql": r"\bsql\b", "sqlite": r"\bsqlite\b", "postgresql": r"\bpostgres(?:ql)?\b",
        "rust": r"\brust\b|cargo\.toml", "tauri": r"\btauri\b|src-tauri",
    }
    for stack, pattern in patterns.items():
        if re.search(pattern, text): stacks.add(stack)
    # A filename mentioned as context is not an instruction to apply a
    # frontend framework pack to an explicitly backend-only phase.
    if re.search(r"\bbackend[ -]?only\b|バックエンド(?:のみ|だけ)", text) and not re.search(r"\breact\b", text):
        stacks.discard("react")
    root = Path(workspace_root).expanduser() if workspace_root else None
    if root and root.is_dir():
        package = root / "package.json"; cargo = root / "Cargo.toml"; requirements = root / "requirements.txt"; pyproject = root / "pyproject.toml"
        try:
            if package.is_file():
                data = json.loads(package.read_text(encoding="utf-8")); deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                stacks.update({"nodejs", "javascript"})
                if "typescript" in deps: stacks.add("typescript"); versions["typescript"] = str(deps["typescript"])
                if "react" in deps: stacks.add("react"); versions["react"] = str(deps["react"])
            if cargo.is_file():
                data = cargo.read_text(encoding="utf-8"); stacks.add("rust")
                if re.search(r"\btauri\s*=", data): stacks.add("tauri")
            if requirements.is_file() or pyproject.is_file():
                stacks.add("python")
                all_python_metadata = "\n".join(path.read_text(encoding="utf-8", errors="ignore")[:100_000] for path in (requirements, pyproject) if path.is_file())
                if re.search(r"\bfastapi\b", all_python_metadata, re.I): stacks.add("fastapi")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    return stacks, versions


def determine_domains(request: str) -> set[str]:
    value = request.lower(); result: set[str] = set()
    if re.search(r"auth|authoriz|secret|security|csrf|xss|injection|path traversal|permission|validat(?:e|ion)|認証|認可|秘密|脆弱|検証", value): result.add("security")
    if re.search(r"test|fixture|regression|verify|coverage|検証|テスト", value): result.add("testing")
    if re.search(r"ui|ux|accessib|keyboard|focus|responsive|form|画面|表示|アクセシ", value): result.add("uiux")
    if re.search(r"architect|module|contract|api|migration|transaction|設計|構成|境界", value): result.add("architecture")
    return result or {"architecture"}


class CodingKnowledge:
    def __init__(self, index_path: str | Path | None = None, model: str = EXPECTED_EMBEDDING_MODEL):
        module = Path(__file__).resolve()
        candidates = [
            Path(os.environ["OLCR_CODING_KNOWLEDGE_INDEX"]) if os.environ.get("OLCR_CODING_KNOWLEDGE_INDEX") else None,
            module.parents[3] / "knowledge" / "coding-knowledge-index.json",
            module.parents[2] / "packaging" / "coding-knowledge" / "coding-knowledge-index.json",
        ]
        default = next((candidate for candidate in candidates if candidate and candidate.is_file()), candidates[-1])
        self.path = Path(index_path) if index_path else default
        self.model = model
        self.index = KnowledgeIndex.load(self.path)

    def context_for(self, request: str, workspace_root: str | Path | None, provider: EmbeddingProvider, model_identity: str | None) -> KnowledgeResult:
        if not self.index.available:
            return KnowledgeResult("NOT_AVAILABLE", reason=self.index.reason)
        if not self.index.compatible(self.model, model_identity):
            return KnowledgeResult("NOT_AVAILABLE", reason="INCOMPATIBLE_EMBEDDING")
        stacks, versions = discover_repository_stacks(workspace_root, request)
        domains = determine_domains(request)
        core = self.index.select_core_rules(domains, stacks)
        try:
            query = provider.embed([request], self.model)[0]
        except Exception as exc:
            # Core rules remain immutable and useful, but dynamic detail must not be guessed.
            return KnowledgeResult("CORE_ONLY", tuple(core), (), "QUERY_EMBEDDING_UNAVAILABLE:" + type(exc).__name__)
        if len(query) != self.index.metadata["embedding_dimension"]:
            return KnowledgeResult("NOT_AVAILABLE", reason="QUERY_EMBEDDING_DIMENSION_MISMATCH")
        return KnowledgeResult("AVAILABLE", tuple(core), tuple(self.index.search(query, domains=domains, stacks=stacks, versions=versions)), "")

    @staticmethod
    def format_context(result: KnowledgeResult, max_chars: int = 12_000) -> str:
        if result.status == "NOT_AVAILABLE":
            return ""
        sections: list[str] = []
        if result.core_rules:
            lines = ["MANDATORY PRINCIPLES"] + [f"- [{row['knowledge_id']}|{row['source']}] {row['content']}" for row in result.core_rules]
            sections.append("\n".join(lines))
        if result.records:
            lines = ["RETRIEVED IMPLEMENTATION KNOWLEDGE"] + [f"- [{row['knowledge_id']}|{row['source']}] {row['content']}" for row in result.records]
            sections.append("\n".join(lines))
        return "\n\n".join(sections)[:max_chars]
