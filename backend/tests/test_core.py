import json
import os
from pathlib import Path
import tempfile
import unittest

from olcr_api.auth import AuthorizationPolicy
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import Risk, SearchResult
from olcr_api.procedures import LOWERCASE_PROCEDURE, ProcedureRunner
from olcr_api.retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, PathGuard, RetrievalRouter
from olcr_api.runtime import ContextManager, Runtime
from olcr_api.tools import ToolValidationError, registry


class FakeModel:
    def __init__(self): self.calls=[]
    def generate(self, messages, model, stream=False): self.calls.append(messages); return {"text":"synthesized","prompt_tokens":3,"completion_tokens":1,"latency_ms":2}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.db=Database(str(self.root/"olcr.db")); self.db.initialize()
        self.model=FakeModel(); self.files=FileRetriever([str(self.root)])
        self.runtime=Runtime(Settings(allowed_roots=(str(self.root),),db_path=self.db.path,main_model="mock").validated(),self.db,RetrievalRouter(self.files,FTSRetriever(self.db),DisabledVectorStore(),False),self.model)
    def tearDown(self): self.tmp.cleanup()
    def test_direct_zero_model_calls(self):
        task,response=self.runtime.execute("calculate 2 + 3 * 4"); self.assertEqual(json.loads(response)["result"],"14"); self.assertEqual([],self.model.calls); self.assertEqual("DIRECT",task.route.value)
    def test_tool_validation(self):
        with self.assertRaises(ToolValidationError): registry()["sort_ascending"].run({"items":[1,"2"]})
    def test_authorization(self):
        self.assertEqual("waiting_for_confirmation",AuthorizationPolicy().decide(Risk.CONFIRM).state)
        self.assertFalse(AuthorizationPolicy().decide(Risk.DENY_DEFAULT,True).allowed)
    def test_path_traversal(self):
        with self.assertRaises(PermissionError): PathGuard([str(self.root)]).resolve(str(self.root/".."/"secret"))
    def test_fts_and_lexical_order(self):
        (self.root/"note.txt").write_text("distinctive-literal here")
        self.db.index_document("db://other","other","distinctive-literal in db",{},1)
        rows,method=self.runtime.retrieval.retrieve("distinctive-literal",10)
        self.assertTrue(rows); self.assertIn(method,("ripgrep","python_fallback"))
    def test_fts_source_filter(self):
        self.db.index_document("a","a","alpha zebra",{},1); self.assertEqual("a",FTSRetriever(self.db,"a").search("zebra",3)[0].source)
    def test_context_budget(self):
        messages,selected=ContextManager(12).build("hello",[SearchResult("a","12345"),SearchResult("b","67890")]); self.assertEqual(1,len(selected))
    def test_neural(self):
        task,response=self.runtime.execute("Write a tiny poem"); self.assertEqual("synthesized",response); self.assertEqual(1,len(self.model.calls)); self.assertEqual("NEURAL",task.route.value)
    def test_confirmation_and_deny(self):
        self.assertEqual("waiting_for_confirmation",self.runtime.execute("rename this file")[0].state.value)
        self.assertEqual("denied",self.runtime.execute("sudo rm -rf /tmp/x")[0].state.value)
    def test_procedure_fresh_binding(self):
        runner=ProcedureRunner(registry(),AuthorizationPolicy())
        self.assertEqual("one",runner.run(LOWERCASE_PROCEDURE,{"text":"ONE"})[0]["output"]["text"])
        self.assertEqual("two",runner.run(LOWERCASE_PROCEDURE,{"text":"TWO"})[0]["output"]["text"])


if __name__ == "__main__": unittest.main()
