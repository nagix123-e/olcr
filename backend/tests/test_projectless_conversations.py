import sqlite3
import tempfile
import unittest
from pathlib import Path

from olcr_api.db import Database, SCHEMA_VERSION


class ProjectlessConversationDatabaseTests(unittest.TestCase):
    def test_v33_migration_preserves_existing_projected_conversations_and_children(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "v33.db")
            with sqlite3.connect(path) as raw:
                raw.executescript("""
                    CREATE TABLE schema_version(version INTEGER NOT NULL);
                    INSERT INTO schema_version VALUES(33);
                    CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace_path TEXT,
                      archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    INSERT INTO projects VALUES('p','Project',NULL,0,1,1);
                    CREATE TABLE conversations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                      title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    INSERT INTO conversations VALUES('c','p','Existing',1,1);
                    CREATE TABLE coding_tasks(id TEXT PRIMARY KEY);
                    CREATE TABLE messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                      task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at REAL NOT NULL, blocks_json TEXT);
                    INSERT INTO messages VALUES('m','c',NULL,'user','preserved',0,1,NULL);
                """)

            db = Database(path)
            db.initialize()
            migrated = db.conversation("c")
            self.assertEqual("p", migrated["project_id"])
            self.assertEqual("preserved", migrated["messages"][0]["content"])
            self.assertEqual("free", db.create_conversation("Unattached", 2, "free", None))
            self.assertIsNone(db.conversation("free")["project_id"])
            with db.connect() as conn:
                self.assertEqual(SCHEMA_VERSION, conn.execute("SELECT version FROM schema_version").fetchone()[0])
                self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
                project_id = next(row for row in conn.execute("PRAGMA table_info(conversations)") if row[1] == "project_id")
                self.assertEqual(0, project_id[3])


if __name__ == "__main__":
    unittest.main()
