from pathlib import Path
import tempfile
import unittest
import zipfile

from olcr_cli.main import banner, command, main, request_text, _explicit_implementation_request
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
    def test_explicit_japanese_implementation_takes_precedence_over_capability_words(self):
        prompt = """現在ロードされているcore contextのTetris開発計画を仕様として使って、
現在のworkspaceに実際に動作するTetrisを作ってください。
Vanilla HTML/CSS/JavaScriptで実装してください。
必要なファイルはすべて現在のworkspace内だけに実際に作成してください。
workspace外にはファイルを作成・変更しないでください。
実装が完了したら、作成して再読込確認できたファイル名だけ報告してください。"""
        self.assertTrue(_explicit_implementation_request(prompt))
        self.assertFalse(_explicit_implementation_request("workspace内のファイルを作成・更新できますか？"))
    def test_zip_context_load_reads_text_without_extraction(self):
        self.state.set_workspace(str(self.workspace)); archive=self.workspace/"context.zip"
        with zipfile.ZipFile(archive,"w") as z: z.writestr("README.md","BOARD=10x20")
        self.state.load_context_file(str(archive)); self.assertIn("BOARD=10x20",self.state.context()["content"]); self.assertFalse((self.workspace/"README.md").exists())
    def test_zip_unsafe_and_binary_members_are_skipped(self):
        self.state.set_workspace(str(self.workspace)); archive=self.workspace/"mixed.zip"
        with zipfile.ZipFile(archive,"w") as z: z.writestr("../outside.txt","bad"); z.writestr("image.bin",b"\x00\x01"); z.writestr("ok.txt","safe")
        self.state.load_context_file(str(archive)); self.assertIn("safe",self.state.context()["content"]); self.assertNotIn("bad",self.state.context()["content"])

if __name__=="__main__": unittest.main()
