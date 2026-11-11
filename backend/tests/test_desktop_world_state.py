import sqlite3
import tempfile
import unittest
from pathlib import Path

from olcr_api.db import Database, SCHEMA_VERSION


class DesktopWorldStateDatabaseTests(unittest.TestCase):
    def test_state_is_bounded_persistent_ttl_limited_and_session_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "olcr.db")
            db = Database(path)
            db.initialize()
            state = {"schema_version": 1, "session_id": "macos-ax-7", "ui_elements": [{"role": "AXButton", "label": "Continue"}], "scene_epoch": 1}
            db.save_desktop_world_state("macos-ax-7", state, 100, ttl_seconds=600)

            reopened = Database(path)
            reopened.initialize()
            self.assertEqual(state, reopened.load_desktop_world_state("macos-ax-7", 699)["state"])
            self.assertIsNone(reopened.load_desktop_world_state("macos-ax-7", 700))
            self.assertIsNone(reopened.load_desktop_world_state("other-session", 101))
            for index in range(33):
                db.save_desktop_world_state(f"session-{index}", {"scene_epoch": index}, 800 + index)
            with db.connect() as connection:
                self.assertEqual(32, connection.execute("SELECT COUNT(*) FROM desktop_world_states").fetchone()[0])
            self.assertIsNone(db.load_desktop_world_state("session-0", 900))
            self.assertIsNotNone(db.load_desktop_world_state("session-32", 900))

    def test_world_state_storage_rejects_oversized_payload_and_migrates_v30(self):
        with tempfile.TemporaryDirectory() as root:
            legacy_path = str(Path(root) / "legacy.db")
            with sqlite3.connect(legacy_path) as connection:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES(30)")
                # coding_tasks is core schema from v7 onward; v30 databases
                # may lack optional scraper tables, but must retain this table
                # for the v34→v35 response_language migration.
                connection.execute("CREATE TABLE coding_tasks(id TEXT PRIMARY KEY)")
            db = Database(legacy_path)
            db.initialize()
            with db.connect() as connection:
                self.assertEqual(SCHEMA_VERSION, connection.execute("SELECT version FROM schema_version").fetchone()[0])
                self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_world_states'").fetchone())
                self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_scrape_runs'").fetchone())
                run_columns = {row[1] for row in connection.execute("PRAGMA table_info(web_scrape_runs)")}
                self.assertIn("recovery_json", run_columns)
            with self.assertRaisesRegex(ValueError, "256 KiB"):
                db.save_desktop_world_state("session", {"large": "x" * (256 * 1024)}, 1)

    def test_perception_policy_updates_only_its_matching_observation_and_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(str(Path(root) / "olcr.db"))
            db.initialize()
            db.save_desktop_world_state("session-1", {"observation_id": "obs-1", "perception_state": "IDLE"}, 100)
            policy = {"controller_id": "a1b2c3d4e5f60718", "revision": 2, "state": "HIGH", "reason": "significant_pixel_motion",
                      "target_fps": 20, "capture_fps_ceiling": 2, "achievable_fps": 2,
                      "compute_level_target": 2, "burst_available": True,
                      "state_durations_ms": {"idle_ms": 1000, "active_ms": 200, "high_ms": 30, "burst_ms": 0}}

            updated = db.update_desktop_world_state_perception("session-1", "obs-1", policy, 101)
            self.assertEqual("HIGH", updated["state"]["perception_state"])
            self.assertEqual(policy, updated["state"]["perception_policy"])
            stale_revision = {**policy, "revision": 1}
            self.assertIsNone(db.update_desktop_world_state_perception("session-1", "obs-1", stale_revision, 102))
            self.assertIsNone(db.update_desktop_world_state_perception("session-1", "obs-old", policy, 103))
            self.assertEqual(2, db.load_desktop_world_state("session-1", 104)["state"]["perception_policy"]["revision"])


if __name__ == "__main__":
    unittest.main()
