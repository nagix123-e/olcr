import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
    def test_direct_calculator_ignores_supported_answer_formatting_clause(self):
        task,response=self.runtime.execute("Calculate 6 * 7 and return only the result.")
        self.assertEqual("42",json.loads(response)["result"])
        self.assertEqual("DIRECT",task.route.value)
        self.assertEqual("success",task.tool_executions[0]["status"])
        self.assertEqual([],self.model.calls)
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
    def test_orchestrator_off_accepts_coding_plans_and_code_without_operations(self):
        self.runtime.settings = self.runtime.settings.with_overrides({'task_manager_enabled': False})
        request = ('Implement a dinner picker in HTML, CSS and JavaScript. Create files in the workspace. '
                   'First propose a plan and wait for approval. Do not use the network.')
        answers = ['First, separate the picker logic from the UI. Shall I proceed?',
                   '```js\nexport const pick = items => items[Math.floor(Math.random() * items.length)];\n```']
        for workspace in (None, str(self.root)):
            for answer in answers:
                with self.subTest(workspace=workspace, answer=answer), \
                     patch.object(self.model, 'generate', return_value={'text': answer}) as generate, \
                     patch.object(self.runtime, '_execute_implementation', side_effect=AssertionError('structured executor entered')):
                    task, response = self.runtime.execute(request, workspace_root=workspace)
                self.assertEqual('completed', task.state.value)
                self.assertEqual('NEURAL', task.route.value)
                self.assertEqual(answer, response)
                self.assertIsNone(task.error)
                self.assertEqual([], task.tool_executions)
                generate.assert_called_once()
                self.assertNotIn('format', generate.call_args.kwargs)
                self.assertNotIn('IMPLEMENTATION_FILE_OPERATION_SCHEMA', str(generate.call_args.args[0]))

    def test_final_inference_bypasses_retrieval_and_discloses_unverified_current_facts(self):
        with patch.object(self.model, 'generate', return_value={'text': 'His current season statistics are unverified.'}) as model, \
             patch.object(self.runtime.retrieval, 'retrieve', side_effect=AssertionError('retrieval must not run again')):
            task, response = self.runtime.infer_without_sources('What are his latest statistics?',
                                                               failure_reason='WEB_SEARCH_FAILED')
        self.assertEqual('completed', task.state.value)
        self.assertIn('unverified', response)
        self.assertIn('current facts', str(model.call_args.args[0]))
        self.assertEqual(self.runtime.settings.planner_model, model.call_args.args[1])
        self.assertIsNone(task.error)

    def test_final_inference_failure_is_bounded_and_is_not_success(self):
        from olcr_api.ollama import ModelFailure
        with patch.object(self.model, 'generate', side_effect=ModelFailure('timeout', 'unavailable')) as model:
            task, response = self.runtime.infer_without_sources('Tell me about baseball',
                                                               failure_reason='WEB_SEARCH_FAILED')
        model.assert_called_once()
        self.assertEqual('failed', task.state.value)
        self.assertEqual('timeout', task.error)
        self.assertNotIn('一般知識に基づく回答です', response)

    def test_authorized_implementation_writes_and_records_tools(self):
        class ImplementationModel:
            def generate(self,*_args,**_kwargs): return {"text":json.dumps({"operations":[{"op":"write","path":"index.html","content":"<h1>Game</h1>"}]}),"latency_ms":1}
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,ImplementationModel())
        task,response=runtime.execute("Implement the complete game. Create files in the authorized workspace.")
        self.assertEqual("IMPLEMENTATION",task.route.value); self.assertEqual("completed",task.state.value)
        self.assertEqual("<h1>Game</h1>",(self.root/"index.html").read_text())
        self.assertIn("workspace_write",[x["tool"] for x in task.tool_executions]); self.assertIn("Write: PASS; read-back: PASS; structural validation: PASS",response)
    def test_implementation_path_traversal_is_denied(self):
        class ImplementationModel:
            def generate(self,*_args,**_kwargs): return {"text":json.dumps({"operations":[{"op":"write","path":"../outside.txt","content":"no"}]}),"latency_ms":1}
        task,response=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,ImplementationModel()).execute("Implement and create files")
        self.assertEqual("failed",task.state.value); self.assertIn("outside allowed roots",response)
    def test_confirmation_and_deny(self):
        self.assertEqual("waiting_for_confirmation",self.runtime.execute("rename this file")[0].state.value)
        self.assertEqual("denied",self.runtime.execute("sudo rm -rf /tmp/x")[0].state.value)
    def test_procedure_fresh_binding(self):
        runner=ProcedureRunner(registry(),AuthorizationPolicy())
        self.assertEqual("one",runner.run(LOWERCASE_PROCEDURE,{"text":"ONE"})[0]["output"]["text"])
        self.assertEqual("two",runner.run(LOWERCASE_PROCEDURE,{"text":"TWO"})[0]["output"]["text"])

    def test_runtime_loads_and_records_builtin_procedure_in_sqlite(self):
        task,_response=self.runtime.execute("procedure lowercase: ONE")
        self.assertEqual("completed",task.state.value)
        self.assertEqual("one",task.tool_executions[0]["output"]["text"])
        stored=self.db.procedure("builtin.lowercase")
        self.assertTrue(stored["validated"])
        self.assertEqual(1,stored["run_stats"]["successes"])
        self.assertEqual("SUCCEEDED_UNVERIFIED",stored["run_stats"]["last_result"])
        self.assertEqual(0,stored["run_stats"]["verified_successes"])
        self.assertIsNone(stored["run_stats"]["last_verified"])


if __name__ == "__main__": unittest.main()
