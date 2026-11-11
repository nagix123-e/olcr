import unittest
import tempfile
import hashlib
import json
from pathlib import Path

from olcr_api.auth import AuthorizationPolicy
from olcr_api.db import Database
from olcr_api.models import Risk
from olcr_api.procedures import (Procedure, ProcedureRunner, ProcedureStore,
                                ProcedureVerificationError, routine_goal_signature)
from olcr_api.tools import Tool


def make_tool(name, effect):
    return Tool(name, "1", Risk.SAFE, lambda value: value, effect)


class ProcedureSafetyTests(unittest.TestCase):
    def test_verified_action_trace_is_bounded_persisted_and_never_executable(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/"trace.sqlite")
            database=Database(path); database.initialize()
            store=ProcedureStore(database)
            trace={"trace_version":1,"goal_signature":"a"*64,
                "action":{"kind":"open_disclosure","mechanism":"accessibility"},
                "verification":{"result":"CONFIRMED"},"context":{"source":"AX"}}
            captured=store.capture_verified_trace(trace)
            self.assertEqual("CAPTURED",captured.status)
            self.assertFalse(captured.validated)
            self.assertEqual((),captured.steps)
            self.assertEqual(trace,{key:value for key,value in captured.captured_trace.items()
                                    if key!="captured_at"})
            self.assertEqual([],store.list())
            reopened=ProcedureStore(Database(path))
            loaded=reopened.get(captured.id)
            self.assertEqual("CAPTURED",loaded.status)
            self.assertFalse(loaded.validated)
            with self.assertRaisesRegex(ValueError,"not active and validated"):
                ProcedureRunner({},AuthorizationPolicy()).run(loaded,{})

    def test_trace_capture_rejects_unconfirmed_or_oversized_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"trace.sqlite")); database.initialize()
            store=ProcedureStore(database)
            with self.assertRaisesRegex(ValueError,"only confirmed"):
                store.capture_verified_trace({"action":{"kind":"open_find"},
                    "verification":{"result":"INCONCLUSIVE"}})
            with self.assertRaisesRegex(ValueError,"size limit"):
                store.capture_verified_trace({"action":{"kind":"open_find"},
                    "verification":{"result":"CONFIRMED"},"extra":"x"*20_000})

    def test_trace_capture_has_a_fixed_candidate_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"trace-cap.sqlite")); database.initialize()
            store=ProcedureStore(database)
            trace={"action":{"kind":"open_find"},"verification":{"result":"CONFIRMED"}}
            for _ in range(64):
                store.capture_verified_trace(trace)
            with self.assertRaisesRegex(ValueError,"capacity reached"):
                store.capture_verified_trace(trace)

    def test_repeated_identical_traces_compile_only_to_a_non_runnable_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"candidate.sqlite")); database.initialize()
            store=ProcedureStore(database)
            trace={"trace_version":1,"goal_signature":"b"*64,
                "context":{"source":"AX","session_id":"macos-ax-123","application_pid":123,
                           "application_bundle_id":"com.example.demo",
                           "coordinate_space":"ax_global_screen_coordinates"},
                "preconditions":[{"role":"AXDisclosureTriangle","label":"Project Core Context",
                                   "expanded":False}],
                "action":{"kind":"open_disclosure","mechanism":"accessibility"},
                "actions":[{"kind":"open_disclosure","mechanism":"accessibility"}],
                "expected_effects":[{"role":"disclosure","expanded":True}],
                "verification":{"result":"CONFIRMED"},
                "provenance":{"semantic_source":"AX","action_source":"accessibility"}}
            first=store.capture_verified_trace(trace)
            with self.assertRaisesRegex(ValueError,"two to eight"):
                store.compile_candidate([first.id])
            second=store.capture_verified_trace({**trace,"context":{**trace["context"],
                "session_id":"macos-ax-456","application_pid":456}})
            candidate=store.compile_candidate([first.id,second.id])
            self.assertEqual("CANDIDATE",candidate.status)
            self.assertFalse(candidate.validated)
            self.assertEqual(1,len(candidate.steps))
            self.assertEqual(trace["actions"][0], candidate.steps[0]["declarative_action"])
            self.assertEqual(2,candidate.captured_trace["source_trace_count"])
            self.assertEqual({"source":"AX","application_bundle_id":"com.example.demo",
                              "coordinate_space":"ax_global_screen_coordinates"},
                             candidate.captured_trace["context"])
            expected_environment=hashlib.sha256(json.dumps(
                candidate.captured_trace["context"],sort_keys=True,ensure_ascii=False
            ).encode("utf-8")).hexdigest()
            self.assertEqual(expected_environment,candidate.environment_signature)
            self.assertEqual(trace["actions"],candidate.captured_trace["actions"])
            self.assertEqual([trace["provenance"],trace["provenance"]],
                             candidate.captured_trace["verification_provenance"])
            self.assertFalse(any(item.id==candidate.id for item in store.list()))
            with self.assertRaisesRegex(ValueError,"not active and validated"):
                ProcedureRunner({candidate.steps[0]["tool"]:make_tool(candidate.steps[0]["tool"],lambda x:x)},
                                AuthorizationPolicy()).run(candidate,{})
            with self.assertRaisesRegex(ValueError,"already compiled"):
                store.compile_candidate([first.id,second.id])

    def test_candidate_review_is_persisted_without_activating_or_validating(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/"review.sqlite")
            database=Database(path); database.initialize()
            store=ProcedureStore(database)
            trace={"trace_version":1,"goal_signature":"c"*64,
                "context":{"source":"AX","session_id":"macos-ax-123","application_pid":123,
                           "application_bundle_id":"com.example.demo",
                           "coordinate_space":"ax_global_screen_coordinates"},
                "preconditions":[{"role":"AXDisclosureTriangle","label":"Details","expanded":False}],
                "action":{"kind":"open_disclosure","mechanism":"accessibility"},
                "actions":[{"kind":"open_disclosure","mechanism":"accessibility"}],
                "expected_effects":[{"role":"disclosure","expanded":True}],
                "verification":{"result":"CONFIRMED"}}
            first=store.capture_verified_trace(trace)
            second=store.capture_verified_trace(trace)
            candidate=store.compile_candidate([first.id,second.id])
            approved=store.review_candidate(candidate.id,"APPROVED")
            self.assertEqual("APPROVED",approved.review["decision"])
            self.assertEqual([approved.review],list(approved.review_history))
            self.assertEqual("CANDIDATE",approved.status)
            self.assertFalse(approved.validated)
            reopened=ProcedureStore(Database(path)).get(candidate.id)
            self.assertEqual(approved.review,reopened.review)
            self.assertEqual(approved.review_history,reopened.review_history)
            with self.assertRaisesRegex(ValueError,"not active and validated"):
                ProcedureRunner({},AuthorizationPolicy()).run(reopened,{})
            rejected=store.review_candidate(candidate.id,"REJECTED")
            self.assertEqual("REJECTED",rejected.review["decision"])
            self.assertEqual([approved.review,rejected.review],list(rejected.review_history))
            reopened_after_rejection=ProcedureStore(Database(path)).get(candidate.id)
            self.assertEqual(rejected.review_history,reopened_after_rejection.review_history)
            with self.assertRaisesRegex(ValueError,"APPROVED or REJECTED"):
                store.review_candidate(candidate.id,"ACTIVE")

    def test_routine_match_requires_approval_exact_goal_fresh_world_state_and_unique_target(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"match.sqlite")); database.initialize()
            store=ProcedureStore(database)
            target={"role":"AXDisclosureTriangle","label":"Details","enabled":True,"expanded":False}
            goal="Open the Details section"
            trace={"trace_version":1,"goal_signature_version":1,
                "goal_signature":routine_goal_signature(goal),
                "context":{"source":"AX","session_id":"old-session","application_pid":123,
                           "application_bundle_id":"com.example.demo",
                           "coordinate_space":"ax_global_screen_coordinates"},
                "preconditions":[target],"action":{"kind":"open_disclosure","mechanism":"accessibility"},
                "actions":[{"kind":"open_disclosure","mechanism":"accessibility","target":target}],
                "expected_effects":[{"role":"disclosure","expanded":True}],
                "verification":{"result":"CONFIRMED"}}
            first=store.capture_verified_trace(trace)
            second=store.capture_verified_trace({**trace,"context":{**trace["context"],
                "session_id":"another-session","application_pid":456}})
            candidate=store.compile_candidate([first.id,second.id])
            state={"source":"AX","coordinate_space":"ax_global_screen_coordinates",
                   "observation_id":"live-observation","observed_at":99.0,
                   "active_window":{"application_bundle_id":"com.example.demo"},
                   "ui_elements":[{**target,"id":"live-element"}]}
            record={"session_id":"live-session","state":state,"updated_at":99.0}
            self.assertEqual([],store.match_candidates(goal,record,now=100.0))
            store.review_candidate(candidate.id,"APPROVED")
            self.assertEqual(routine_goal_signature(goal),routine_goal_signature("  OPEN   THE Details section!? "))
            matches=store.match_candidates("  OPEN   THE Details section!? ",record,now=100.0)
            self.assertEqual(1,len(matches))
            self.assertEqual(candidate.id,matches[0]["procedure_id"])
            self.assertEqual("live-element",matches[0]["element_id"])
            self.assertFalse(matches[0]["executable"])
            self.assertEqual([],store.match_candidates(goal,{**record,"state":{**state,"observed_at":60.0}},now=100.0))
            self.assertEqual([],store.match_candidates(goal,{**record,"state":{**state,
                "active_window":{"application_bundle_id":"com.other.demo"}}},now=100.0))
            ambiguous={**record,"state":{**state,"ui_elements":[
                {**target,"id":"one"},{**target,"id":"two"}]}}
            self.assertEqual([],store.match_candidates(goal,ambiguous,now=100.0))

    def test_routine_candidate_rejects_traces_from_different_contexts(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"candidate-context.sqlite")); database.initialize()
            store=ProcedureStore(database)
            trace={"goal_signature":"c"*64,
                "context":{"source":"AX","session_id":"macos-ax-1","application_pid":123,
                           "application_bundle_id":"com.example.demo",
                           "coordinate_space":"ax_global_screen_coordinates"},
                "preconditions":[{"role":"AXDisclosureTriangle","label":"Details","expanded":False}],
                "action":{"kind":"open_disclosure","mechanism":"accessibility"},
                "actions":[{"kind":"open_disclosure","mechanism":"accessibility"}],
                "expected_effects":[{"expanded":True}],"verification":{"result":"CONFIRMED"}}
            first=store.capture_verified_trace(trace)
            second=store.capture_verified_trace({**trace,"context":{**trace["context"],
                "application_bundle_id":"com.other.demo"}})
            with self.assertRaisesRegex(ValueError,"same bounded action"):
                store.compile_candidate([first.id,second.id])

    def test_routine_candidate_rejects_different_action_route_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"candidate-route.sqlite")); database.initialize()
            store=ProcedureStore(database)
            trace={"goal_signature":"d"*64,
                "context":{"source":"AX","session_id":"macos-ax-1","application_pid":123,
                           "application_bundle_id":"com.example.demo",
                           "coordinate_space":"ax_global_screen_coordinates"},
                "preconditions":[{"role":"AXSearchField","label":"Find","focused":False}],
                "action":{"kind":"open_find","mechanism":"keyboard_shortcut"},
                "actions":[{"kind":"open_find","mechanism":"keyboard_shortcut",
                             "shortcut":{"key_code":3,"key":"f","modifiers":8,
                                         "provenance":"application_menu"}}],
                "expected_effects":[{"role":"AXSearchField","focused":True}],
                "verification":{"result":"CONFIRMED"}}
            first=store.capture_verified_trace(trace)
            different_route={**trace,"actions":[{"kind":"open_find","mechanism":"keyboard_shortcut",
                "shortcut":{"key_code":4,"key":"f","modifiers":8,
                            "provenance":"application_menu"}}]}
            second=store.capture_verified_trace(different_route)
            with self.assertRaisesRegex(ValueError,"same bounded action"):
                store.compile_candidate([first.id,second.id])

    def test_procedure_roundtrip_and_failure_stats_use_existing_sqlite_table(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"routines.sqlite")); database.initialize()
            store=ProcedureStore(database)
            procedure=Procedure("routine", "1", "test", (), (
                {"tool":"one","input":{},"expected_effect":{"state":"saved"}},
            ), "document is open", True, environment_signature="app-v1",
                preconditions=({"kind":"document_open"},))
            store.save(procedure)
            self.assertEqual(procedure,store.get("routine"))
            self.assertEqual("ACTIVE",store.list()[0].status)
            runner=ProcedureRunner({"one":make_tool("one",lambda value:{"ok":True})},
                AuthorizationPolicy(),environment_signature=lambda:"wrong",
                precondition_check=lambda *_args:True,run_recorder=store.record_run)
            for _ in range(2):
                with self.assertRaisesRegex(ProcedureVerificationError,"ENVIRONMENT_SIGNATURE_MISMATCH"):
                    runner.run(store.get("routine"),{})
            stored=database.procedure("routine")
            self.assertEqual(2,stored["run_stats"]["failures"])
            self.assertEqual(2,stored["run_stats"]["consecutive_failures"])
            self.assertEqual("SUSPECT",stored["status"])
            self.assertEqual([],store.list())
            store.save(procedure)
            self.assertEqual("ACTIVE",store.get("routine").status)
            self.assertEqual(0,database.procedure("routine")["run_stats"]["consecutive_failures"])

    def test_sqlite_distinguishes_verified_success_from_execution_only(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"verification.sqlite")); database.initialize()
            store=ProcedureStore(database)
            procedure=Procedure("verified", "1", "test", (), (
                {"tool":"one","input":{},"expected_effect":{"state":"done"}},
            ), "", True)
            store.save(procedure)
            store.record_run(procedure,True,[{"verification":{"result":"CONFIRMED"}}])
            stats=database.procedure("verified")["run_stats"]
            self.assertEqual(1,stats["successes"])
            self.assertEqual(1,stats["verified_successes"])
            self.assertEqual("CONFIRMED",stats["last_result"])
            self.assertIsInstance(stats["last_verified"],float)

    def test_repeated_unvalidated_candidate_failures_mark_suspect_without_validation(self):
        with tempfile.TemporaryDirectory() as root:
            database=Database(str(Path(root)/"candidate-failures.sqlite")); database.initialize()
            store=ProcedureStore(database)
            candidate=Procedure("candidate", "1", "candidate", (), (), "same app", False,
                status="CANDIDATE", review={"decision":"APPROVED"})
            store.save(candidate)
            failure=[{"tool":"olcr.gui.verified_action",
                      "verification":{"result":"NOT_CONFIRMED"}}]
            store.record_run(candidate,False,failure)
            store.record_run(candidate,False,failure)
            saved=database.procedure(candidate.id)
            self.assertEqual("SUSPECT",saved["status"])
            self.assertFalse(saved["validated"])
            self.assertEqual(2,saved["run_stats"]["failures"])
            self.assertEqual(2,saved["run_stats"]["consecutive_failures"])

    def test_environment_mismatch_blocks_before_first_action(self):
        calls = []
        procedure = Procedure("routine", "1", "test", (), ({"tool": "one", "input": {}},),
                              "same app", True, environment_signature="app-v1")
        runner = ProcedureRunner({"one": make_tool("one", lambda value: calls.append(value))},
                                 AuthorizationPolicy(), environment_signature=lambda: "app-v2")
        with self.assertRaisesRegex(ProcedureVerificationError, "ENVIRONMENT_SIGNATURE_MISMATCH"):
            runner.run(procedure, {})
        self.assertEqual([], calls)

    def test_precondition_mismatch_blocks_before_first_action(self):
        calls = []
        procedure = Procedure("routine", "1", "test", (), ({"tool": "one", "input": {}},),
                              "document is open", True, preconditions=({"kind": "document_open"},))
        runner = ProcedureRunner({"one": make_tool("one", lambda value: calls.append(value))},
                                 AuthorizationPolicy(), precondition_check=lambda _checks, _params: False)
        with self.assertRaisesRegex(ProcedureVerificationError, "PRECONDITION_MISMATCH"):
            runner.run(procedure, {})
        self.assertEqual([], calls)

    def test_failed_effect_verification_stops_later_steps_and_records_failure(self):
        calls, recorded = [], []
        procedure = Procedure("routine", "1", "test", (), (
            {"tool": "one", "input": {}, "expected_effect": {"state": "saved"}},
            {"tool": "two", "input": {}},
        ), "document is open", True)
        runner = ProcedureRunner(
            {"one": make_tool("one", lambda value: calls.append("one") or {"ok": True}),
             "two": make_tool("two", lambda value: calls.append("two"))},
            AuthorizationPolicy(), effect_verifier=lambda *_args: False,
            run_recorder=lambda _procedure, success, observations: recorded.append((success, observations)),
        )
        with self.assertRaises(ProcedureVerificationError) as raised:
            runner.run(procedure, {})
        self.assertEqual(["one"], calls)
        self.assertEqual("EXPECTED_EFFECT_NOT_CONFIRMED", raised.exception.reason)
        self.assertEqual(1, len(raised.exception.observations))
        self.assertEqual([(False, raised.exception.observations)], recorded)

    def test_effect_verification_requires_confirmed_result(self):
        procedure = Procedure("routine", "1", "test", (), (
            {"tool": "one", "input": {}, "expected_effect": {"state": "saved"}},
        ), "document is open", True)
        runner = ProcedureRunner({"one": make_tool("one", lambda value: {"ok": True})},
                                 AuthorizationPolicy(),
                                 effect_verifier=lambda *_args: {"result": "CONFIRMED"})
        result = runner.run(procedure, {})
        self.assertEqual("CONFIRMED", result[0]["verification"]["result"])

    def test_explicit_nonconfirmed_result_cannot_be_overridden_by_legacy_verified_flag(self):
        for result in ("NOT_CONFIRMED", "INCONCLUSIVE", "UNKNOWN"):
            with self.subTest(result=result):
                calls, recorded = [], []
                procedure = Procedure("routine", "1", "test", (), (
                    {"tool": "one", "input": {}, "expected_effect": {"state": "saved"}},
                    {"tool": "two", "input": {}},
                ), "document is open", True)
                runner = ProcedureRunner(
                    {"one": make_tool("one", lambda value: calls.append("one") or {"ok": True}),
                     "two": make_tool("two", lambda value: calls.append("two"))},
                    AuthorizationPolicy(),
                    effect_verifier=lambda *_args: {"result": result, "verified": True},
                    run_recorder=lambda _procedure, success, observations:
                        recorded.append((success, observations)),
                )

                with self.assertRaises(ProcedureVerificationError) as raised:
                    runner.run(procedure, {})

                self.assertEqual(["one"], calls)
                self.assertEqual("EXPECTED_EFFECT_NOT_CONFIRMED", raised.exception.reason)
                self.assertEqual(result, raised.exception.observations[0]["verification"]["result"])
                self.assertEqual([(False, raised.exception.observations)], recorded)

    def test_staged_gui_outcome_uses_runner_recorder_and_requires_confirmed_effect(self):
        def candidate():
            return Procedure("routine", "1", "Find", (), ({
                "tool": "olcr.gui.open_find", "input": {"target": {}},
                "expected_effect": {"focused": True},
            },), "same application", False, status="CANDIDATE",
                review={"decision": "APPROVED"})

        recorded = []
        runner = ProcedureRunner({}, AuthorizationPolicy(),
            run_recorder=lambda procedure, success, observations:
                recorded.append((procedure.id, success, observations)))

        self.assertTrue(runner.record_staged_outcome(candidate(),
            {"result": "CONFIRMED", "verified": True}, execution_attempted=True))
        self.assertEqual(1, len(recorded))
        self.assertEqual(("routine", True, [{"tool": "olcr.gui.open_find",
                         "verification": {"result": "CONFIRMED"}}]), recorded[0])

        for verification, attempted in (
            ({"result": "INCONCLUSIVE", "verified": True}, True),
            ({"result": "NOT_CONFIRMED"}, False),
        ):
            with self.subTest(verification=verification, attempted=attempted):
                self.assertFalse(runner.record_staged_outcome(candidate(), verification,
                    execution_attempted=attempted))
        self.assertEqual(1, len(recorded))

        self.assertTrue(runner.record_staged_outcome(candidate(),
            {"result": "NOT_CONFIRMED"}, execution_attempted=True))
        self.assertEqual(2, len(recorded))
        self.assertFalse(recorded[-1][1])

    def test_staged_gui_outcome_rejects_unapproved_or_unbounded_candidates(self):
        recorded = []
        runner = ProcedureRunner({}, AuthorizationPolicy(),
            run_recorder=lambda *args: recorded.append(args))
        procedure = Procedure("routine", "1", "Find", (), ({
            "tool": "olcr.gui.open_find", "input": {}, "expected_effect": {"focused": True},
        },), "same application", False, status="CANDIDATE",
            review={"decision": "REJECTED"})
        self.assertFalse(runner.record_staged_outcome(procedure,
            {"result": "CONFIRMED"}, execution_attempted=True))
        rejected_tool = Procedure("routine", "1", "unsafe", (), ({
            "tool": "shell", "input": {}, "expected_effect": {"finished": True},
        },), "", False, status="CANDIDATE", review={"decision": "APPROVED"})
        self.assertFalse(runner.record_staged_outcome(rejected_tool,
            {"result": "CONFIRMED"}, execution_attempted=True))
        self.assertEqual([], recorded)

    def test_inactive_or_unvalidated_routine_is_rejected(self):
        runner = ProcedureRunner({}, AuthorizationPolicy())
        for validated, status in ((False, "ACTIVE"), (True, "SUSPECT"), (True, "DISABLED")):
            procedure = Procedure("routine", "1", "test", (), (), "", validated, status=status)
            with self.assertRaisesRegex(ValueError, "not active and validated"):
                runner.run(procedure, {})


if __name__ == "__main__":
    unittest.main()
