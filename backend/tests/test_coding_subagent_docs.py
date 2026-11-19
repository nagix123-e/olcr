from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CodingSubagentDocsTests(unittest.TestCase):
    def test_user_help_describes_separate_opt_in_authorization_and_pinned_model(self):
        japanese = (ROOT / "backend/olcr_api/help_knowledge/coding-ja.md").read_text()
        english = (ROOT / "backend/olcr_api/help_knowledge/coding-en.md").read_text()
        for phrase in ("Coding Orchestratorとは独立した設定", "初期状態はOFF", "自動起動しません", "タスク専用承認",
                       "現在のCoding Task実装モデル", "ローカルモデルは推奨しません"):
            self.assertIn(phrase, japanese)
        for phrase in ("independent from Coding Orchestrator", "defaults to OFF", "never starts workers automatically",
                       "separate task-specific approval", "current Coding Task implementation model",
                       "Local models are not recommended"):
            self.assertIn(phrase, english)

    def test_settings_and_workflow_help_warn_about_local_models_and_separate_gate(self):
        files = (
            ROOT / "backend/olcr_api/help_knowledge/settings-ja.md",
            ROOT / "backend/olcr_api/help_knowledge/settings-en.md",
            ROOT / "docs/LOCAL_CODING_WORKFLOW.md",
        )
        contents = [path.read_text() for path in files]
        for content in contents:
            self.assertTrue("ローカルモデル" in content or "local models" in content.lower())
            self.assertTrue("個別" in content or "task-specific" in content or "タスク専用" in content)


if __name__ == "__main__":
    unittest.main()
