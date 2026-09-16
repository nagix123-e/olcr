from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("OLCR_DB_PATH", "/private/tmp/olcr-coding-knowledge-tests.sqlite")

from olcr_api.knowledge import (
    CodingKnowledge, KnowledgeBuildError, KnowledgeIndex, build_production_index,
    content_hash, determine_domains, discover_repository_stacks, near_duplicate_pairs,
    stable_knowledge_id,
    validate_source_manifest,
)
import olcr_api.app as api

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "packaging" / "coding-knowledge"


class DeterministicEmbedding:
    """Local deterministic test seam; production builder always calls Ollama."""
    def model_identity(self, model):
        self.model = model
        return "sha256:knowledge-test"

    def embed(self, texts, model):
        self.model = model
        vectors = []
        for text in texts:
            lower = text.lower()
            vectors.append([
                float("react" in lower), float("fastapi" in lower), float("sqlite" in lower),
                float("postgres" in lower), float("rust" in lower), float("tauri" in lower),
                float("accessib" in lower or "keyboard" in lower), float("security" in lower or "authoriz" in lower),
                1.0,
            ])
        return vectors


class CodingKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.tmp.name)
        self.source_manifest = json.loads((INPUTS / "sources.json").read_text(encoding="utf-8"))
        self.seeds = json.loads((INPUTS / "records.json").read_text(encoding="utf-8"))
        self.index_path = self.root / "coding-knowledge-index.json"
        self.provider = DeterministicEmbedding()
        self.metadata = build_production_index(
            source_manifest=self.source_manifest, seeds=self.seeds, provider=self.provider,
            output=self.index_path, model_identity="sha256:knowledge-test",
            created_at="2026-09-11T00:00:00Z", target_olcr_version="0.6.0",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_stable_ids_and_content_hash(self):
        index = KnowledgeIndex.load(self.index_path)
        self.assertTrue(index.available)
        record = index.entries[0]
        self.assertEqual(record["knowledge_id"], stable_knowledge_id(record))
        self.assertEqual(record["content_hash"], content_hash(record["content"]))
        self.assertNotEqual(content_hash(record["content"]), content_hash(record["content"] + " changed"))
        self.assertEqual(self.metadata["record_count"], len(index.entries))
        self.assertEqual("embeddinggemma:latest", self.provider.model)

    def test_unapproved_source_exact_duplicates_near_duplicates_and_conflicts_fail(self):
        bad_sources = json.loads(json.dumps(self.source_manifest))
        bad_sources["sources"][0]["review_status"] = "NEEDS_REVIEW"
        bad = json.loads(json.dumps(self.seeds)); bad["records"][0]["source"] = bad_sources["sources"][0]["source_id"]
        with self.assertRaisesRegex(KnowledgeBuildError, "unapproved"):
            build_production_index(source_manifest=bad_sources, seeds=bad, provider=self.provider, output=self.root / "bad.json", model_identity="x", created_at="2026-09-11", target_olcr_version="0.6.0")
        duplicate = json.loads(json.dumps(self.seeds)); duplicate["records"].append(dict(duplicate["records"][0]))
        with self.assertRaisesRegex(KnowledgeBuildError, "duplicate"):
            build_production_index(source_manifest=self.source_manifest, seeds=duplicate, provider=self.provider, output=self.root / "duplicate.json", model_identity="x", created_at="2026-09-11", target_olcr_version="0.6.0")
        pairs = near_duplicate_pairs([{"knowledge_id":"a","content":"validate untrusted input at boundary"}, {"knowledge_id":"b","content":"validate untrusted input at boundary"}])
        self.assertEqual([("a", "b")], pairs)
        conflict = json.loads(json.dumps(self.seeds)); conflict["records"][0]["conflicts_with"] = conflict["records"][1]["topic"]
        with self.assertRaisesRegex(KnowledgeBuildError, "conflict"):
            build_production_index(source_manifest=self.source_manifest, seeds=conflict, provider=self.provider, output=self.root / "conflict.json", model_identity="x", created_at="2026-09-11", target_olcr_version="0.6.0")
        mutated = json.loads(json.dumps(self.source_manifest)); mutated["sources"][0]["title"] += " (mutated)"
        with self.assertRaisesRegex(KnowledgeBuildError, "provenance"):
            validate_source_manifest(mutated)

    def test_metadata_filter_versions_top_five_and_no_reranker(self):
        index = KnowledgeIndex.load(self.index_path)
        query = self.provider.embed(["React controlled form accessibility"], "embeddinggemma:latest")[0]
        rows = index.search(query, domains={"uiux", "architecture"}, stacks={"react"}, versions={"react":"18.3.0"}, limit=999)
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(rows)
        self.assertTrue(all(row["stack"] == "react" for row in rows))
        incompatible = index.search(query, domains={"uiux", "architecture"}, stacks={"react"}, versions={"react":"17.0.0"})
        self.assertEqual([], incompatible)
        self.assertFalse(any("rerank" in name.lower() for name in dir(index)))

    def test_core_rules_zero_match_missing_corrupt_and_read_only(self):
        knowledge = CodingKnowledge(self.index_path)
        before = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
        result = knowledge.context_for("Use Tauri commands with authorization", None, self.provider, "sha256:knowledge-test")
        self.assertEqual("AVAILABLE", result.status)
        self.assertTrue(result.core_rules)
        self.assertLessEqual(len(result.records), 5)
        zero = KnowledgeIndex.load(self.index_path).search([1.0] * 9, domains={"testing"}, stacks={"tauri"})
        self.assertEqual([], zero)
        missing = CodingKnowledge(self.root / "absent.json").context_for("test", None, self.provider, "sha256:knowledge-test")
        self.assertEqual("NOT_AVAILABLE", missing.status)
        corrupt_path = self.root / "corrupt.json"; corrupt_path.write_text("{not json", encoding="utf-8")
        corrupt = CodingKnowledge(corrupt_path).context_for("test", None, self.provider, "sha256:knowledge-test")
        self.assertEqual("NOT_AVAILABLE", corrupt.status)
        self.assertFalse(hasattr(knowledge.index, "write"))
        self.assertEqual(before, hashlib.sha256(self.index_path.read_bytes()).hexdigest())

    def test_benchmark_and_adversarial_stack_selection(self):
        benchmark = json.loads((INPUTS / "benchmark.json").read_text(encoding="utf-8"))["queries"]
        self.assertEqual(16, len(benchmark))
        self.assertEqual({"architecture", "security", "testing", "uiux"}, {item["expected_domain"] for item in benchmark})
        self.assertTrue({"html_css", "javascript", "typescript", "react", "nodejs", "python", "fastapi", "sql", "sqlite", "postgresql", "rust", "tauri"} <= {item["expected_stack"] for item in benchmark})
        benchmark = [
            ("React controlled form state", {"react"}), ("FastAPI request validation", {"fastapi"}),
            ("SQLite transaction behavior", {"sqlite"}), ("PostgreSQL index design", {"postgresql"}),
            ("Rust Result error propagation", {"rust"}), ("Tauri command boundary", {"tauri"}),
            ("CSS responsive table keyboard accessibility", {"html_css"}),
        ]
        for query, expected in benchmark:
            stacks, _ = discover_repository_stacks(None, query)
            self.assertTrue(expected <= stacks, query)
        stacks, _ = discover_repository_stacks(None, "backend-only phase: update api.py; frontend/src/App.tsx is background context")
        self.assertNotIn("react", stacks)
        self.assertIn("security", determine_domains("enforce authorization at the API boundary"))

    def test_managed_subtask_injects_only_compatible_immutable_context(self):
        knowledge = CodingKnowledge(self.index_path)
        with patch.object(api, "coding_knowledge", knowledge):
            context = api.coding_knowledge_context_for_subtask("Implement FastAPI request validation", None, self.provider)
        self.assertIn("MANDATORY PRINCIPLES", context)
        self.assertIn("RETRIEVED IMPLEMENTATION KNOWLEDGE", context)
        self.assertIn("cki-", context)
        with patch.object(api, "coding_knowledge", knowledge):
            unavailable = api.coding_knowledge_context_for_subtask("Implement FastAPI request validation", None, type("Bad", (), {"model_identity": lambda self, model: "wrong", "embed": self.provider.embed})())
        self.assertEqual("", unavailable)


if __name__ == "__main__":
    unittest.main()
