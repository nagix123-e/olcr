import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.coding_tasks import (coding_candidate, extract_plan_json, manager_review_prompt, model_slot,
                                   normalize_manager_decision, plan_schema, report_has_authoritative_failure, classify_execution_mode,
                                   validate_plan, evaluate_phase, classify_waiting_input, coding_action_intent,
                                   compact_normal_plan, classify_coding_request, coding_classification_diagnostics,
                                   classify_mutation_mode, plan_prompt, canonical_coding_requirements,
                                   normalize_coding_requirements)
from olcr_api.db import Database
from olcr_api.models import Route, Task, TaskState


def plan(goal: str) -> dict:
    return {"schema_version":1,"original_goal":goal,"scope":{"allowed":["backend"],"forbidden":[]},"assumptions":[],
            "phases":[{"id":f"p{i}","goal":f"phase {i}","status":"pending","done":["done"],"verify":["manual check"],"dependencies":[] if i==1 else [f"p{i-1}"],"risks":[]} for i in range(1,4)],
            "max_retries_per_phase":2,"requires_user_approval":True}


class CodingTaskDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp")
        self.db=Database(str(Path(self.tmp.name)/"coding.sqlite"));self.db.initialize()
        self.db.create_project("project",self.tmp.name,time.time(),"project")
        for conversation in ("a","b","c"): self.db.create_conversation(conversation,time.time(),conversation,"project")
    def tearDown(self): self.tmp.cleanup()
    def task(self, task_id: str, conversation: str):
        return self.db.create_coding_task(task_id,conversation,"fix sample","RESUMABLE","NONE",plan("fix sample"),time.time())
    def test_mutation_mode_distinguishes_existing_repair_from_new_capability(self):
        self.assertEqual(("FIX", "EXISTING_BEHAVIOR_REPAIR"), classify_mutation_mode("開始ボタンを押してもゲームが始まりません。直してください"))
        self.assertEqual("IMPLEMENTATION", classify_mutation_mode("ログイン機能を追加してください")[0])
        self.assertEqual("FIX", classify_mutation_mode("既存のHeroアニメーションが途中で止まるので直して")[0])
        self.assertEqual("IMPLEMENTATION", classify_mutation_mode("HeroにAnime.jsのアニメーションを新しく追加して")[0])
        self.assertIn("exactly one phase", plan_prompt("ボタンが動かないので直して", mutation_mode="FIX"))

    def test_requirement_normalization_ignores_future_fix_and_descriptive_architecture(self):
        request="""# Current Task
This initial task is IMPLEMENTATION, not FIX. ReactでOLCRの新しいmarketing websiteを作成する。
Anime.js MCP、shadcn MCP、Playwright MCPを使用する。Anime.js v4を導入する。
backend/databaseは実装しない。不要なdependencyは追加しない。
# Product Description
サイト上でOLCRのbackendとdatabase architectureを説明する。
# Future FIX Benchmark
Later user requests such as 「ボタンを直して」と言われたらFIXにする。
# Regression Tests
backend/databaseの失敗も検証する。"""
        requirements=canonical_coding_requirements(request)
        self.assertEqual("IMPLEMENTATION", requirements["mutation_mode"])
        self.assertEqual({"frontend", "dependencies"}, set(requirements["required_capabilities"]))
        self.assertEqual({"backend", "database"}, set(requirements["forbidden_capabilities"]))
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", requirements["task_profile"])
        self.assertEqual("NORMAL", requirements["execution_mode"])
        self.assertEqual(["animejs", "shadcn", "playwright"], requirements["required_mcps"])

    def test_normalizer_structured_input_isolates_typed_project_and_history(self):
        normalized = normalize_coding_requirements({
            "current_user_text": "Reactの静的サイトを作成。backend/databaseは実装しない。Anime.jsを使用する。",
            "project_metadata": {"target_kind": "website", "description": "backend database frontend dependencies"},
            "selected_workspace": "/tmp/project",
            "conversation_id": "conversation-1",
            "conversation_history": "以前の assistant: backend/database/frontend/dependencies を実装した。",
            "assistant_history": "backend database frontend dependencies",
            "model_context": {"prose": "backend database frontend dependencies"},
        })
        self.assertEqual(["dependencies", "frontend"], normalized["required_capabilities"])
        self.assertEqual(["backend", "database"], normalized["forbidden_capabilities"])
        diagnostics = normalized["normalization_diagnostics"]
        self.assertEqual(diagnostics["current_user_text_hash"], diagnostics["normalizer_control_input_hash"])
        self.assertFalse(diagnostics["project_context_added_before_normalization"])
        self.assertFalse(diagnostics["conversation_history_added_before_normalization"])
        self.assertFalse(diagnostics["assistant_history_added_before_normalization"])
        emitted = {(item["capability"], item["polarity"]) for item in diagnostics["capability_provenance"] if item["active_for_control"]}
        self.assertIn(("frontend", "required"), emitted)
        self.assertIn(("dependencies", "required"), emitted)
        self.assertIn(("backend", "forbidden"), emitted)
        self.assertIn(("database", "forbidden"), emitted)

    def test_unrelated_frontend_and_dependency_policies_are_scope_only(self):
        normalized = normalize_coding_requirements(
            "React frontendを実装する。do not modify unrelated frontend changes; "
            "do not add unnecessary dependencies. backendを実装しない。"
        )
        self.assertEqual(["frontend"], normalized["required_capabilities"])
        self.assertEqual(["backend"], normalized["forbidden_capabilities"])
        self.assertEqual("MINIMAL", normalized["dependency_policy"])

    def test_provenance_is_current_turn_only(self):
        normalized = normalize_coding_requirements("frontendを実装する。backendは実装しない。")
        provenance = normalized["normalization_diagnostics"]["capability_provenance"]
        self.assertTrue(provenance)
        self.assertTrue(all(item["source_scope"] == "current_user_text" and item["active_for_control"] for item in provenance))
        self.assertTrue(all(item["source_span_hash"] for item in provenance))
    def test_plan_validation_and_typed_failure_gate(self):
        self.assertEqual([],validate_plan(plan("fix sample"),"fix sample"))
        report={"status":"PASS","errors":[],"blockers":[],"test_fail":[],"test_executed":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN"}
        self.assertFalse(report_has_authoritative_failure(report,plan("fix sample")["phases"][0]))
        self.assertTrue(report_has_authoritative_failure({**report,"status":"FAIL"},plan("fix sample")["phases"][0]))
        self.assertTrue(report_has_authoritative_failure(report,{**plan("fix sample")["phases"][0],"verify":["run test"]}))

    def test_static_site_is_normal_and_empty_dedicated_verify_is_valid(self):
        request="Create a simple two-page static HTML website using index.html, links.html and styles.css, with responsive accessibility. No framework, backend, database, or dependencies."
        self.assertEqual("NORMAL",classify_execution_mode(request))
        compact=plan(request)
        compact["phases"]=compact["phases"][:2]
        compact["phases"][0]["verify"]=[]
        compact["phases"][1]["dependencies"]=["p1"]
        compact.pop("tasks",None)
        self.assertEqual([],validate_plan(compact,request))

    def test_static_site_plan_compaction_preserves_requirements(self):
        request="Create a simple static HTML website using index.html, links.html and styles.css, with responsive accessibility. No framework, backend, database, or dependencies."
        oversized=plan(request)
        oversized["phases"]=[
            {"id":"p1","goal":"inspect repository","status":"pending","done":["repo facts"],"verify":["paths checked"],"dependencies":[],"risks":["scope"]},
            {"id":"p2","goal":"create index.html","status":"pending","done":["home page"],"verify":["HTML structure"],"dependencies":["p1"],"risks":["markup"]},
            {"id":"p3","goal":"create links.html","status":"pending","done":["links page"],"verify":["links present"],"dependencies":["p2"],"risks":["navigation"]},
            {"id":"p4","goal":"add styles and accessibility","status":"pending","done":["responsive style"],"verify":["keyboard access"],"dependencies":["p3"],"risks":["layout"]},
            {"id":"p5","goal":"browser verification","status":"pending","done":["browser checked"],"verify":["browser verification"],"dependencies":["p4"],"risks":["manual check"]},
        ]
        oversized["tasks"]=[
            {"task_id":phase["id"],"phase_id":phase["id"],"goal":phase["goal"],"domain":"frontend","depends_on":phase["dependencies"],"required_context":[phase["id"]],"required_mcp":[],"change_scope":[phase["id"]],"verification":phase["verify"],"done_condition":phase["done"],"retry_state":{}}
            for phase in oversized["phases"]
        ]
        compact=compact_normal_plan(oversized,request)
        self.assertEqual(2,len(compact["phases"]))
        self.assertEqual([],validate_plan(compact,request))
        merged=compact["phases"][0]
        self.assertTrue({"repo facts","home page","links page","responsive style"} <= set(merged["done"]))
        self.assertTrue({"paths checked","HTML structure","links present","keyboard access"} <= set(merged["verify"]))
        self.assertEqual([merged["id"]],compact["phases"][1]["dependencies"])
        self.assertEqual(2,len(compact["tasks"]))
        self.assertTrue({"p1","p2","p3","p4"} <= set(compact["tasks"][0]["required_context"]))

    def test_web_only_execution_is_not_a_coding_implementation_success(self):
        execution=Task("implementation phase")
        execution.transition(TaskState.ROUTING)
        execution.route=Route.NEURAL
        execution.transition(TaskState.GENERATING)
        execution.transition(TaskState.COMPLETED)
        report=api._report_from_execution(plan("goal")["phases"][0],0,execution,"web answer")
        self.assertEqual("FAIL",report["status"])
        self.assertIn("CODING_IMPLEMENTATION_ZERO_WRITE_WEB_ROUTE",report["errors"])
    def test_plan_json_extraction_allows_one_fence_but_rejects_ambiguous_prose(self):
        value=plan("fix sample")
        parsed,error=extract_plan_json("```json\n"+__import__("json").dumps(value)+"\n```")
        self.assertEqual([],validate_plan(parsed,"fix sample")); self.assertEqual("",error)
        parsed,error=extract_plan_json("before "+__import__("json").dumps(value))
        self.assertIsNone(parsed); self.assertTrue(error)
        parsed,error=extract_plan_json(__import__("json").dumps(value)+"\n"+__import__("json").dumps(value))
        self.assertIsNone(parsed); self.assertIn("exactly one",error)
    def test_fifo_dequeue_and_resume_to_tail(self):
        for task_id,conversation in (("a","a"),("b","b"),("c","c")):
            self.task(task_id,conversation);self.db.enqueue_coding_task(task_id)
        self.assertEqual("a",self.db.next_queued_coding_task()["id"])
        self.assertEqual(2,self.db.coding_task_queue_position("b"))
        self.db.dequeue_coding_task("b")
        self.assertEqual("RESUMABLE",self.db.coding_task("b")["status"])
        self.db.enqueue_coding_task("b")
        self.assertEqual(["a","c","b"],[self.db.next_queued_coding_task()["id"],self.db.coding_tasks("c")[0]["id"],self.db.coding_tasks("b")[0]["id"]])
        self.assertEqual(3,self.db.coding_task_queue_position("b"))
    def test_restart_recovers_without_requeue(self):
        self.task("a","a");self.db.enqueue_coding_task("a")
        self.db.recover_interrupted_coding_tasks()
        task=self.db.coding_task("a")
        self.assertEqual("RESUMABLE",task["status"])
        self.assertIsNone(self.db.next_queued_coding_task())
    def test_waiting_for_plan_approval_does_not_block_the_next_queued_task(self):
        self.task("a","a")
        self.db.update_coding_task("a",status="WAITING_FOR_PLAN_APPROVAL",activity="NONE")
        self.task("b","b");self.db.enqueue_coding_task("b")
        self.assertEqual("b",self.db.next_queued_coding_task()["id"])
    def test_phase_report_uuid_round_trip_and_update(self):
        self.task("a","a")
        report={"phase_id":"p1","attempt":0,"status":"PASS","implemented":[],"changed_files":[],"test_executed":[],"test_pass":[],"test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[]}
        self.db.add_coding_phase_report("a","p1",0,report,"PASS",time.time())
        saved=self.db.coding_phase_reports("a")[0]
        self.assertIsInstance(saved["id"],str)
        saved["structured_report"]["manager_decision"]={"decision":"PASS","reason":"verified"}
        self.db.update_coding_phase_report(saved["id"],saved["structured_report"],"PASS")
        self.assertEqual("PASS",self.db.coding_phase_reports("a")[0]["structured_report"]["manager_decision"]["decision"])
    def test_model_slot_serializes_threads(self):
        concurrent=0;maximum=0;guard=threading.Lock()
        def work():
            nonlocal concurrent,maximum
            with model_slot():
                with guard: concurrent+=1;maximum=max(maximum,concurrent)
                time.sleep(.02)
                with guard: concurrent-=1
        threads=[threading.Thread(target=work) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1,maximum)

    def test_manager_decision_normalizes_bounded_diagnosis_from_typed_report(self):
        phase=plan("fix sample")["phases"][0]
        report={"phase_id":"p1","plan_revision":1,"attempt":2,"status":"FAIL",
                "done":[],"errors":["typed write failed"],"blockers":[],"test_fail":[],
                "build_executed":"NOT_RUN"}
        typed={"state":"failed","error":"typed write failed","operations":[{"tool":"workspace_write","status":"error"}]}
        decision=normalize_manager_decision({"decision":"RETRY","reason":"repair the write"},phase,report,typed)
        self.assertEqual("RETRY",decision["decision"])
        diagnosis=decision["diagnosis"]
        self.assertEqual({"phase_id":"p1","plan_revision":1,"attempt":2},
                         {key:diagnosis[key] for key in ("phase_id","plan_revision","attempt")})
        self.assertTrue(diagnosis["unmet_done"])
        self.assertTrue(diagnosis["evidence"])
        self.assertTrue(diagnosis["retry_instruction"])

    def test_review_prompt_requires_structured_diagnosis(self):
        prompt=manager_review_prompt("fix sample",plan("fix sample")["phases"][0],
                                     {"phase_id":"p1"},{"state":"completed"},[],0,[],None)
        self.assertIn('"diagnosis"',prompt)
        self.assertIn("unmet_done",prompt)

    def _evidence_report(self, *, attempt=0, changed=("index.html",), raw_state="completed"):
        return {"phase_id":"p1","plan_revision":0,"attempt":attempt,"status":"PASS","implemented":[],
                "changed_files":list(changed),"test_executed":["typed readback"],"test_pass":["typed readback"],
                "test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[],
                "typed_execution_summary":{"state":raw_state,"error":None,"operations":[
                    {"tool":"workspace_write","status":"success","output":{"path":path}} for path in changed
                ] + [{"tool":"workspace_read","status":"success","output":{"path":path}} for path in changed]}}

    def test_complete_evidence_forces_pass_over_gemma_retry(self):
        phase=plan("fix sample")["phases"][0]; report=self._evidence_report()
        evaluation=evaluate_phase(phase,report)
        self.assertTrue(evaluation["phase_complete"])
        self.assertEqual(["PASS"],evaluation["allowed_decisions"])
        self.assertNotEqual("RETRY",evaluation["allowed_decisions"][0])

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_review_rejects_contradictory_gemma_retry_after_complete_evidence(self):
        conversation="a"; task_id="authoritative-pass"; goal="fix sample"; task_plan=plan(goal)
        self.db.create_coding_task(task_id,conversation,goal,"RUNNING","NONE",task_plan,time.time())
        old_db=api.db; api.db=self.db
        report_row=api._save_report(task_id,"p1",0,self._evidence_report(),"PASS")
        class Model:
            def generate(self,messages,model,think=False):
                if "Gemma review" in messages[0]["content"]:
                    return {"text":'{"decision":"RETRY","reason":"contradictory model advice"}'}
                raise AssertionError("only the review call is expected")
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            decision=api._review_phase(task_id,api.db.coding_task(task_id),task_plan["phases"][0],report_row,[])
        finally: api.runtime=old_runtime
        api.db=old_db
        self.assertEqual("PASS",decision)
        saved=self.db.coding_phase_reports(task_id)[0]["structured_report"]
        self.assertEqual("PASS",saved["manager_decision"]["decision"])
        self.assertTrue(saved["phase_evaluation"]["phase_complete"])
        self.assertEqual({"raw_decision":None,"decision_valid":True,"effective_decision":"PASS","phase_complete":True},saved["manager_diagnostics"])

    def test_cross_attempt_evidence_accumulates_and_delete_invalidates(self):
        phase={**plan("fix sample")["phases"][0],"done":["index.html exists","style.css exists"],"verify":["typed readback"]}
        first=self._evidence_report(changed=("index.html",)); second=self._evidence_report(attempt=1,changed=("style.css",))
        evaluation=evaluate_phase(phase,second,[first])
        self.assertEqual(2,evaluation["done_satisfied"])
        removed={**second,"typed_execution_summary":{"state":"completed","error":None,"operations":[{"tool":"workspace_delete","status":"success","output":{"path":"index.html","operation":"delete"}}]},"changed_files":[]}
        invalidated=evaluate_phase(phase,removed,[first])
        self.assertIn("index.html exists",invalidated["done_unmet"])


class CodingTaskApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db,self.old_settings=api.db,api.settings
        api.db=Database(str(Path(self.tmp.name)/"api.sqlite"));api.db.initialize()
        # Coding Task Manager API tests exercise the existing manager path
        # explicitly; production defaults are covered by the OFF tests.
        api.rebuild(self.old_settings.with_overrides({
            "db_path": str(Path(self.tmp.name) / "api.sqlite"),
            "task_manager_enabled": True,
        }))
        self.client=TestClient(api.app)
        self.project=self.client.post("/api/projects",json={"name":"A","workspace_path":self.tmp.name}).json()["id"]
    def tearDown(self):
        api.db=self.old_db
        api.rebuild(self.old_settings)
        self.tmp.cleanup()
    def test_tetris_implementation_request_is_managed_candidate_before_runtime(self):
        api.rebuild(api.settings.with_overrides({"task_manager_enabled": True}))
        request="このCoding Task Managerの実GUI E2Eテストとして、使い捨ての新しい作業フォルダに、ブラウザで遊べるシンプルなテトリスを作成してください。"
        self.assertTrue(coding_candidate(request))
        with patch.object(api._coding_scheduler_wake,"set") as wake, patch.object(api.runtime,"execute",side_effect=AssertionError("normal runtime must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":request})
        self.assertEqual(200,response.status_code)
        self.assertTrue(response.json().get("coding_task_id"))
        self.assertEqual("Coding Task を登録しました。計画を作成中です。",response.json()["response"])
        self.assertTrue(wake.called)
        self.assertEqual("QUEUED",api.db.coding_task(response.json()["coding_task_id"])["status"])

    def test_ambiguous_reply_stays_with_waiting_plan_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_PLAN_APPROVAL","NONE",plan(goal),time.time())
        with patch.object(api.runtime,"execute",side_effect=AssertionError("waiting input must not use normal runtime")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"なるほど"})
        self.assertEqual(200,response.status_code)
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("WAITING_FOR_PLAN_APPROVAL",api.db.coding_task(task_id)["status"])

    def test_waiting_input_classifier_separates_question_from_revision(self):
        self.assertEqual("CLARIFICATION", classify_waiting_input("確認が必要とはどういうこと"))
        self.assertEqual("CLARIFICATION", classify_waiting_input("何が不足していますか？"))
        self.assertEqual("REVISION", classify_waiting_input("Phase 3 の確認条件を変更してください"))
        self.assertEqual("CONTROL", classify_waiting_input("再開"))

    def test_coding_action_intent_is_independent_from_candidate_and_preserves_explanations(self):
        self.assertTrue(coding_action_intent("テトリスを作成してください"))
        self.assertTrue(coding_action_intent("このrepoに認証機能を実装してください"))
        self.assertTrue(coding_action_intent("fix this code"))
        self.assertFalse(coding_action_intent("JavaScriptのPromiseを説明して"))
        self.assertFalse(coding_action_intent("テトリスの実装方法を教えて"))
        self.assertFalse(coding_action_intent("JavaScriptのイベントリスナーの短い例を見せて"))

    def test_design_planning_and_explicit_non_coding_never_activate_orchestrator(self):
        planning = (
            "実装計画を作ってください", "ボタン構成とUIを設計してください",
            "Reactで作る場合の画面構成を考えて", "このAPIの設計をレビューして",
            "テトリスの実装計画とUI案を作って",
            "React、API、Database、UI の技術設計だけしてください",
            "Do not modify code; planning only.",
        )
        for request in planning:
            with self.subTest(request=request):
                self.assertEqual("NON_CODING", classify_coding_request(request))
                self.assertFalse(coding_candidate(request))
        exact = "テトリスの実装計画を、ボタン構成、操作対応、UIまで含めて細かく策定してください。\nこれはコーディングタスクではありません。"
        facts = coding_classification_diagnostics(exact)
        self.assertEqual("NON_CODING", facts["classification"])
        self.assertTrue(facts["planning_intent"])
        self.assertTrue(facts["explicit_non_coding"])
        self.assertFalse(facts["mutation_intent"])

    def test_mutation_requests_remain_coding_and_contradictions_are_ambiguous(self):
        self.assertEqual("CODING", classify_coding_request("この機能を実装して"))
        self.assertEqual("CODING", classify_coding_request("このReact画面を修正して"))
        self.assertEqual("CODING", classify_coding_request("Execute the implementation plan."))
        contradictory = "コーディングはしないでください。ただしコードを修正してください"
        facts = coding_classification_diagnostics(contradictory)
        self.assertEqual("AMBIGUOUS", facts["classification"])
        self.assertTrue(facts["mutation_intent"])
        self.assertTrue(facts["explicit_non_coding"])

    def test_explicit_execution_intent_preserves_fixed_benchmark_text(self):
        request = "Fix the existing add function so tests/test_math.py passes. Change only src/math.py, then run the focused test command."
        facts = coding_classification_diagnostics(request, project_scoped=True, execution_intent="coding_mutation")
        self.assertEqual("CODING", facts["classification"])
        self.assertTrue(facts["mutation_intent"])
        self.assertEqual("EXPLICIT_EXECUTION_INTENT", facts["reason"])

    def test_unknown_execution_intent_does_not_activate_coding(self):
        request = "Explain the existing add function and its test."
        facts = coding_classification_diagnostics(request, project_scoped=True, execution_intent="read_only")
        self.assertNotEqual("CODING", facts["classification"])

    def test_planning_request_uses_normal_brain_without_task_or_scheduler(self):
        request = "テトリスの実装計画を、ボタン構成、操作対応、UIまで含めて細かく策定してください。\nこれはコーディングタスクではありません。"
        execution = Task(request, route=Route.DIRECT, state=TaskState.COMPLETED)
        with patch.object(api._coding_scheduler_wake, "set") as wake, \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api.runtime, "execute", return_value=(execution, "テトリスのUI計画です")):
            response = self.client.post("/api/chat", json={"project_id": self.project, "message": request})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("coding_task_id", response.json())
        self.assertEqual("テトリスのUI計画です", response.json()["response"])
        self.assertEqual([], api.db.coding_tasks(response.json()["conversation_id"]))
        wake.assert_not_called()

    def test_plan_then_implement_creates_task_on_the_later_mutation_message(self):
        planning = "実装計画を作って"
        execution = Task(planning, route=Route.DIRECT, state=TaskState.COMPLETED)
        with patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api.runtime, "execute", return_value=(execution, "計画")):
            first = self.client.post("/api/chat", json={"project_id": self.project, "message": planning})
        self.assertNotIn("coding_task_id", first.json())
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            second = self.client.post("/api/chat", json={"project_id": self.project,
                                                            "conversation_id": first.json()["conversation_id"],
                                                            "message": "この計画を実装してください"})
        self.assertTrue(second.json().get("coding_task_id"))
        wake.assert_called_once()

    def test_contradictory_scope_does_not_create_task_before_clarification(self):
        with patch.object(api._coding_scheduler_wake, "set") as wake, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("ambiguous scope must not run Brain or implementation")):
            response = self.client.post("/api/chat", json={"project_id": self.project,
                                                              "message": "コーディングはしないでください。ただしコードを修正してください"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("coding_task_id", response.json())
        self.assertIn("明確ではありません", response.json()["response"])
        self.assertEqual([], api.db.coding_tasks(response.json()["conversation_id"]))
        wake.assert_not_called()

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_candidate_false_actionable_request_is_blocked_before_normal_brain(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        request="このCoding Task Managerの実GUI E2Eテストとして、使い捨ての新しい作業フォルダにテトリスを作成してください"
        with patch.object(api,"coding_candidate",return_value=False), \
             patch.object(api.runtime,"execute",side_effect=AssertionError("normal Brain must not run")), \
             patch.object(api.runtime,"execute_image",side_effect=AssertionError("vision Brain must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":request})
        self.assertEqual(200,response.status_code)
        body=response.json()["response"]
        self.assertIn("実装を開始できませんでした",body)
        self.assertIn("ファイル変更や実装は行っていません",body)
        self.assertIn("ルーティングに失敗",body)
        self.assertNotIn("<html",body.lower())
        self.assertNotIn("実装しました",body)

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_candidate_false_image_action_is_also_guarded(self):
        with patch.object(api,"coding_candidate",return_value=False), \
             patch.object(api.runtime,"execute_image",side_effect=AssertionError("vision Brain must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"このUIを実装してください",
                                                         "image":{"name":"ui.png","mime_type":"image/png","data_url":"data:image/png;base64,AA=="}})
        self.assertEqual(200,response.status_code)
        self.assertIn("実装を開始できませんでした",response.json()["response"])

    def test_explanatory_coding_question_remains_on_normal_runtime_path(self):
        class RuntimeTask:
            def __init__(self):
                self.id="normal-explanation"
                self.raw_request="テトリスの実装方法を教えて"
                self.route=TaskState.ROUTING
                self.state=TaskState.COMPLETED
                self.authorization_state="NOT_REQUIRED"
                self.reason_category=""
                self.selected_context=[]
                self.created_at=time.time(); self.updated_at=self.created_at
                self.error=None; self.tool_executions=[]; self.model_calls=[]
        with patch.object(api,"coding_candidate",return_value=False), \
             patch.object(api,"router_decision",return_value=None), \
             patch.object(api,"route_external_tool",return_value=None), \
             patch.object(api.runtime,"execute",return_value=(RuntimeTask(),"テトリスの実装方法を説明します")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"テトリスの実装方法を教えて"})
        self.assertEqual(200,response.status_code)
        self.assertIn("実装方法を説明します",response.json()["response"])

    def test_replan_ineffective_clarification_does_not_mutate_or_enqueue(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-clarification"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",plan(goal),time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",pending_authorization={
            "type":"REPLAN_INEFFECTIVE","reason":"Phase replan did not change the failing phase structure.",
            "unmet_criteria":["manual verification"],"state":"PENDING"},
            pending_user_confirmation=1,recovery_reason="REPLAN_INEFFECTIVE")
        with patch.object(api._coding_scheduler_wake,"set") as wake, patch.object(api.runtime,"model") as model:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,
                                                         "message":"確認が必要とはどういうこと"})
        self.assertEqual(200,response.status_code)
        saved=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])
        self.assertEqual(1,saved["pending_user_confirmation"])
        self.assertFalse(wake.called)
        self.assertFalse(model.generate.called)
        self.assertIn("確認が必要",response.json()["response"])

    def test_explicit_waiting_revision_queues_replan_without_resetting_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-revision"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",plan(goal),time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",pending_authorization={
            "type":"REPLAN_INEFFECTIVE","unmet_criteria":["manual verification"],"state":"PENDING"},
            pending_user_confirmation=1,recovery_reason="REPLAN_INEFFECTIVE")
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,
                "message":"Phase 3 の確認条件を、静的確認で判定できる内容に変更してください"})
        self.assertEqual(200,response.status_code)
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual("USER_REVISION",saved["recovery_reason"])
        self.assertEqual(1,saved["pending_authorization"]["unmet_criteria"].__len__())
        self.assertTrue(wake.called)

    def test_user_guided_replan_preserves_completed_prefix_and_selects_failing_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="replan-preserve-prefix"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)
        replacement["phases"][0]["status"]="pass"
        replacement["phases"][1]["status"]="pass"
        replacement["phases"][2]["goal"]="revised phase 3"
        class Model:
            def generate(self,messages,model,think=False,**kwargs):
                return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1","p2"},"user supplied phase revision")
        finally:
            api.runtime=old_runtime
        self.assertEqual("Coding Task was replanned and requeued.",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual(["pass","pass","pending"],[p["status"] for p in saved["plan"]["phases"]])

    def test_planning_and_one_repair_use_the_same_ollama_json_schema_mode(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="structured-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        valid=plan(goal)
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False,**kwargs):
                self.calls.append(kwargs.get("format"))
                return {"text":'{"schema_version":1}' if len(self.calls)==1 else __import__("json").dumps(valid)}
        class Runtime: pass
        fake=Runtime();fake.model=Model()
        old_runtime=api.runtime;api.runtime=fake
        try: parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally: api.runtime=old_runtime
        self.assertEqual([],validate_plan(parsed,goal))
        self.assertEqual(2,len(fake.model.calls));self.assertEqual(plan_schema(),fake.model.calls[0]);self.assertEqual(plan_schema(),fake.model.calls[1])
    def test_task_id_is_returned_before_scheduler_model_work_and_pause_is_resumable(self):
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"このリポジトリを修正して"})
            self.assertEqual(200,response.status_code)
            task_id=response.json()["coding_task_id"]
            self.assertTrue(task_id)
            task=self.client.get(f"/api/coding-tasks/{task_id}").json()
            self.assertEqual("QUEUED",task["status"])
            self.assertTrue(wake.called)
            paused=self.client.patch(f"/api/coding-tasks/{task_id}",json={"pause_requested":True}).json()
            self.assertEqual("RESUMABLE",paused["status"])
            resumed=self.client.patch(f"/api/coding-tasks/{task_id}",json={"resume":True}).json()
            self.assertEqual("QUEUED",resumed["status"])
    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_retry_reexecutes_the_same_phase_and_never_attempts_three(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="retry-task";goal="fix sample";task_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        class Model:
            def __init__(self): self.reviews=0;self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"]
                self.calls.append(system)
                if "Gemma review" in system:
                    self.reviews+=1
                    return {"text":('{"decision":"RETRY","reason":"retry once",'
                                     '"diagnosis":{"unmet_done":["done"],"unmet_verify":["manual check"],'
                                     '"evidence_used":["typed report lacks verification"],'
                                     '"retry_instruction":"satisfy done and rerun manual check",'
                                     '"verification_instruction":"rerun manual check"}}'
                              if self.reviews==1 else '{"decision":"PASS","reason":"verified"}')}
                if "completion check" in system:return {"text":'{"decision":"PASS","reason":"complete"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[];self.contexts=[]
            def execute(self,text,**kwargs):
                self.executions.append(text)
                self.contexts.append(kwargs.get("managed_context"))
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try: api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        reports=api.db.coding_phase_reports(task_id)
        self.assertEqual([0],[row["attempt"] for row in reports if row["phase_id"]=="p1"])
        self.assertEqual(3,len(fake.executions))
        self.assertEqual([], fake.model.calls)
        self.assertEqual("COMPLETED",api.db.coding_task(task_id)["status"])
        self.assertTrue(all(c and c.get("managed_coding_task") and c.get("operation_intent")=="IMPLEMENTATION" for c in fake.contexts))

    def test_invalid_phase_report_is_repaired_once_without_changing_typed_facts(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="report-repair";goal="fix sample";task_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        valid={"phase_id":"p1","attempt":0,"status":"PASS","implemented":[],"changed_files":[],"test_executed":[],"test_pass":[],"test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[]}
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"];self.calls.append(system)
                if "report JSON format" in system: return {"text":__import__("json").dumps(valid)}
                if "Gemma review" in system: return {"text":'{"decision":"PASS","reason":"verified"}'}
                if "completion check" in system: return {"text":'{"decision":"PASS","reason":"complete"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model()
            def execute(self,text,**kwargs):
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        original_report=api._report_from_execution
        def invalid_report(phase,attempt,execution,response):
            return {"phase_id":phase["id"],"attempt":attempt,"status":"PASS","typed_execution_summary":{"state":"completed","error":None,"operations":[]}}
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try:
            with patch.object(api,"_report_from_execution",side_effect=invalid_report):
                api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        report=api.db.coding_phase_reports(task_id)[0]["structured_report"]
        self.assertEqual("completed",report["typed_execution_summary"]["state"])
        self.assertTrue(any("report JSON format" in call for call in fake.model.calls))

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_retry_exhaustion_replans_and_scope_expansion_waits_for_user(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="scope-replan"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)
        replacement["scope"]["allowed"].append("new-safe-fixture")
        replacement["phases"][0]["status"]="pass"
        class Model:
            def __init__(self): self.reviews=0
            def generate(self,messages,model,think=False):
                system=messages[0]["content"]
                if "Gemma review" in system:
                    self.reviews+=1
                    return {"text":'{"decision":"RETRY","reason":"verification failed"}'}
                if "Planning Mode" in system:
                    return {"text":__import__("json").dumps(replacement)}
                return {"text":""}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[]
            def execute(self,text,**kwargs):
                self.executions.append(text)
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try: api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        reports=api.db.coding_phase_reports(task_id)
        self.assertEqual([0],[row["attempt"] for row in reports if row["phase_id"]=="p1"])
        task=api.db.coding_task(task_id)
        self.assertIn(task["status"], {"COMPLETED", "RESUMABLE", "WAITING_FOR_USER"})
        self.assertEqual([], fake.model.calls)

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_pause_after_execution_prevents_review_then_resume_reviews_saved_report_first(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="pause-resume";goal="fix sample";task_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"];self.calls.append(system)
                if "Gemma review" in system or "completion check" in system:
                    return {"text":'{"decision":"PASS","reason":"verified"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[];self.pause_once=True
            def execute(self,text,**kwargs):
                self.executions.append(text)
                if self.pause_once:
                    self.pause_once=False
                    api.db.update_coding_task(task_id,pause_requested=True)
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try:
            api.run_managed_task(task_id,self.tmp.name)
            self.assertEqual("RESUMABLE",api.db.coding_task(task_id)["status"])
            self.assertEqual([],fake.model.calls)
            self.assertEqual(1,len(api.db.coding_phase_reports(task_id)))
            api.db.enqueue_coding_task(task_id)
            api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        self.assertEqual([],fake.model.calls)
        self.assertEqual("COMPLETED",api.db.coding_task(task_id)["status"])

    def test_replan_rejects_a_plan_that_reopens_a_completed_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="preserve-completed";goal="fix sample";current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)  # p1 is incorrectly reopened as pending.
        class Model:
            def generate(self,messages,model,think=False): return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1"},"controlled failure")
        finally: api.runtime=old_runtime
        self.assertEqual("Coding Task replan did not preserve completed phases.",result)
        self.assertEqual("RESUMABLE",api.db.coding_task(task_id)["status"])

    def test_replan_revalidates_only_completed_phase_with_changed_contract(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="revalidate-changed-completed"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)
        replacement["phases"][0]["goal"]="revised completed phase 1"
        replacement["phases"][0]["status"]="pending"
        replacement["phases"][1]["status"]="pass"
        class Model:
            def generate(self,messages,model,think=False): return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1","p2"},"user changed phase 1 acceptance")
        finally: api.runtime=old_runtime
        self.assertEqual("Coding Task was replanned and requeued.",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("p1",saved["current_phase_id"])
        self.assertEqual(["pending","pass","pending"],[p["status"] for p in saved["plan"]["phases"]])

    def test_waiting_for_user_state_survives_database_restart(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-restart"; goal="fix sample"; task_plan=plan(goal)
        task_plan["phases"][0]["status"]="pass"
        task_plan["phases"][1]["status"]="pass"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",task_plan,time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",recovery_reason="REPLAN_INEFFECTIVE",
                                  pending_authorization={"type":"REPLAN_INEFFECTIVE","unmet_criteria":["manual verification"]},
                                  pending_user_confirmation=1)
        reopened=Database(api.db.path); reopened.initialize()
        persisted=reopened.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",persisted["status"])
        self.assertEqual("p3",persisted["current_phase_id"])
        self.assertEqual(["pass","pass","pending"],[p["status"] for p in persisted["plan"]["phases"]])

    def test_ineffective_structural_replan_is_resumable_before_an_identical_cycle(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="ineffective-replan"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        class Model:
            def generate(self,messages,model,think=False):
                return {"text":__import__("json").dumps(current)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),
                                    "verification criteria are structurally unsuitable",
                                    {"unmet_verify":["browser behavior cannot be observed"],"replan_instruction_source":"model"})
        finally: api.runtime=old_runtime
        self.assertIn("再計画では問題を解消できませんでした",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])

    def test_continuous_authorized_plan_requeues_without_plan_approval(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="continuous-plan"; goal="このrepoを確認して実装と検証まで連続して進めてください。途中確認は不要です。"
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","QWEN_PLANNING",None,time.time())
        with patch.object(api,"_generate_plan",return_value=(plan(goal),"{}")), \
             patch.object(api._coding_scheduler_wake,"set"):
            api._run_coding_planning(api.db.coding_task(task_id))
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertIsNone(saved["pending_authorization"])
        self.assertTrue(saved["approved_scopes"])
        messages=api.db.conversation(conversation)["messages"]
        self.assertNotIn("承認します",messages[-1]["content"])

    def test_protected_plan_waits_then_approval_binds_and_resumes(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="protected-plan"; goal="repoを修正してください"
        protected=plan(goal); protected["scope"]["allowed"]=["frontend", "git commit"]
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","QWEN_PLANNING",None,time.time())
        with patch.object(api,"_generate_plan",return_value=(protected,"{}")), \
             patch.object(api._coding_scheduler_wake,"set"):
            api._run_coding_planning(api.db.coding_task(task_id))
        waiting=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",waiting["status"])
        self.assertEqual(["git commit"],waiting["pending_authorization"]["requested_scope"])
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"承認します"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_execute_this_plan_recovers_resumable_task_before_normal_brain(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="recover-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"RESUMABLE","NONE",plan(goal),time.time())
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime,"execute",side_effect=AssertionError("normal Brain must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"この計画を実行してください"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_heavy_batch_checkpoint_is_not_authorization_and_continue_reuses_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="heavy-batch"; task_plan=plan("React frontend + FastAPI backend + SQLite database CRUD")
        api.db.create_coding_task(task_id,conversation,"React frontend + FastAPI backend + SQLite database CRUD","RUNNING","NONE",task_plan,time.time())
        api.db.update_coding_task(task_id,execution_mode="HEAVY_BATCHED")
        api.db.add_coding_phase_report(task_id,"p1",0,{"phase_id":"p1","status":"PASS","manager_decision":{"decision":"PASS"},"changed_files":[],"errors":[],"blockers":[],"test_fail":[]},"PASS",time.time())
        result=api._heavy_batch_checkpoint(task_id,task_plan,{"p1"})
        saved=api.db.coding_task(task_id)
        self.assertIn("続行",result)
        self.assertEqual("RESUMABLE",saved["status"])
        self.assertEqual("RESOURCE_CHECKPOINT",saved["recovery_reason"])
        self.assertIsNone(saved["pending_authorization"])
        self.assertEqual(1,saved["batch_cursor"])
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"続行"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)


if __name__ == "__main__": unittest.main()
