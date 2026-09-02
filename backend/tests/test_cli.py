from pathlib import Path
import tempfile
import unittest

from olcr_cli.main import banner, command, main, request_text
from olcr_cli.state import State


class CliTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.state=State(Path(self.tmp.name)/"support"); self.workspace=Path(self.tmp.name)/"workspace"; self.workspace.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def test_banner_color_and_plain_fallback(self):
        self.assertIn("OLCR",banner(False));self.assertNotIn("\033[",banner(False));self.assertIn("149;227;41",banner(True));self.assertIn("OLCR",banner(False,True))
    def test_workspace_and_context_snapshots_are_isolated(self):
        self.state.set_workspace(str(self.workspace));self.state.set_context("rules","direct");self.assertEqual("rules",self.state.context()["content"])
        source=self.workspace/"context.txt";source.write_text("file rules");self.state.load_context_file(str(source));source.write_text("changed")
        self.assertTrue(self.state.context_changed());self.assertEqual("file rules",self.state.context()["content"]);self.state.reload_context();self.assertEqual("changed",self.state.context()["content"])
    def test_outside_context_is_rejected_and_clear_is_scoped(self):
        self.state.set_workspace(str(self.workspace));outside=Path(self.tmp.name)/"outside.txt";outside.write_text("no")
        with self.assertRaises(PermissionError): self.state.load_context_file(str(outside))
        self.state.set_context("rules","direct");self.state.clear_context();self.assertIsNone(self.state.context())
    def test_one_shot_commands_do_not_enter_repl(self):
        self.state.set_workspace(str(self.workspace)); lines=[];self.assertEqual(0,command(self.state,["workspace","show"],output=lines.append));self.assertIn(str(self.workspace),lines[0]);self.assertEqual(2,command(self.state,["unknown"],output=lambda _:None))
    def test_context_is_empty_without_workspace(self): self.assertIsNone(self.state.context())
    def test_parser_version(self):
        with self.assertRaises(SystemExit) as caught: main(["--version"])
        self.assertEqual(0,caught.exception.code)
    def test_capability_query_is_grounded_and_non_mutating(self):
        self.state.set_workspace(str(self.workspace))
        answer=request_text(self.state,"workspace内のファイルを自力で作成・更新できますか？")
        self.assertIn("認可されたworkspace内",answer)
        self.assertNotIn("できません",answer)
        self.assertEqual([],list(self.workspace.iterdir()))
    def test_capability_query_without_workspace_guides_authorization(self):
        answer=request_text(self.state,"Can you create and update files in the current workspace?")
        self.assertIn("/workspace set",answer)
        self.assertEqual([],list(self.workspace.iterdir()))

if __name__=="__main__": unittest.main()
