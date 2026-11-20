import tempfile
import time
import unittest
from pathlib import Path

from olcr_api.db import Database
from olcr_api.provider_research import ProviderEvidence


class CodingTaskReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.database = Database(str(Path(self.tmp.name) / "references.sqlite"))
        self.database.initialize()
        now = time.time()
        self.database.create_project("Project", self.tmp.name, now, "project")
        self.database.create_conversation("Conversation", now, "conversation", "project")
        self.database.create_coding_task(
            "task-1", "conversation", "Release a package", "RUNNING", "NONE", None, now
        )

    def tearDown(self):
        self.tmp.cleanup()

    def evidence(self, *, phase_id="phase-1", accessed_at=123.5, summary="署名付き成果物が必要です。"):
        return ProviderEvidence(
            task_id="task-1",
            provider_id="github",
            category="publishing",
            title="GitHub release requirements",
            url="https://docs.github.com/releases",
            host="docs.github.com",
            officiality="VERIFIED",
            accessed_at=accessed_at,
            applicability="Current publishing requirements",
            summary=summary,
            phase_id=phase_id,
            plan_revision=2,
        )

    def test_save_and_list_task_references_preserves_provenance(self):
        reference = self.evidence()

        saved = self.database.save_coding_task_reference(reference)
        listed = self.database.coding_task_references("task-1")

        self.assertEqual(1, len(listed))
        self.assertEqual(saved["id"], listed[0]["id"])
        for field in (
            "task_id", "provider_id", "category", "title", "url", "host",
            "officiality", "accessed_at", "applicability", "summary", "phase_id",
            "plan_revision",
        ):
            self.assertEqual(getattr(reference, field), listed[0][field])
        self.assertEqual([listed[0]], self.database.coding_task_references("task-1", phase_id="phase-1"))
        self.assertEqual([], self.database.coding_task_references("task-1", phase_id="other-phase"))

    def test_initialize_adds_reference_table_to_existing_database(self):
        now = time.time()
        self.database.add_coding_phase_report(
            "task-1", "phase-1", 1, {"phase_id": "phase-1", "status": "PASS"}, "PASS", now
        )
        task_before = self.database.coding_task("task-1")
        reports_before = self.database.coding_phase_reports("task-1")

        # Model a v39 database that predates the reference table.
        with self.database.connect() as db:
            db.execute("DROP TABLE IF EXISTS coding_task_references")
            db.execute("UPDATE schema_version SET version=39")

        self.database.initialize()

        with self.database.connect() as db:
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
            table_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coding_task_references'"
            ).fetchone()
        self.assertEqual(41, version)
        self.assertIsNotNone(table_exists)
        self.assertEqual(task_before, self.database.coding_task("task-1"))
        self.assertEqual(reports_before, self.database.coding_phase_reports("task-1"))

    def test_duplicate_task_url_is_idempotent_and_phase_links_are_distinct(self):
        first = self.database.save_coding_task_reference(self.evidence())
        refreshed = self.database.save_coding_task_reference(
            self.evidence(accessed_at=456.0, summary="公開前に署名を確認します。")
        )
        other_phase = self.database.save_coding_task_reference(
            self.evidence(phase_id="phase-2", accessed_at=789.0)
        )

        self.assertEqual(first["id"], refreshed["id"])
        self.assertNotEqual(first["id"], other_phase["id"])
        all_references = self.database.coding_task_references("task-1")
        self.assertEqual(2, len(all_references))
        self.assertEqual({"phase-1", "phase-2"}, {row["phase_id"] for row in all_references})
        self.assertEqual(1, len(self.database.coding_task_references("task-1", phase_id="phase-1")))
        self.assertEqual("公開前に署名を確認します。",
                         self.database.coding_task_references("task-1", phase_id="phase-1")[0]["summary"])

    def test_reference_table_does_not_store_page_bodies_or_search_queries(self):
        with self.database.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(coding_task_references)")}

        self.assertTrue({"task_id", "provider_id", "category", "title", "url", "host",
                         "officiality", "accessed_at", "applicability", "summary", "phase_id",
                         "plan_revision"} <= columns)
        self.assertFalse({"body", "page_body", "search_query", "query", "credentials", "token"} & columns)


if __name__ == "__main__":
    unittest.main()
