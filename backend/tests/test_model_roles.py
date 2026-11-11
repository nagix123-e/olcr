import os
import json
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import olcr_api.app as app
from olcr_api import external_tools
from olcr_api.config import DEFAULT_MAIN_MODEL, DEFAULT_ROUTER_MODEL, Settings
from olcr_api.api_acceptance import PRODUCTION_ACCEPTANCE_CASES
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.model_registry import ModelRegistry
from olcr_api.ollama import ModelFailure


class ModelRoleTests(unittest.TestCase):
    def test_max_overrides_internal_off_and_reserves_space_for_json(self):
        from olcr_api.brain_models import brain_scope
        provider=RoleAwareModelProvider(Settings(context_auto_compress=False))
        with brain_scope({'mode':'unified','efforts':{'brain':'on'}}), \
             patch.object(provider.ollama,'generate',return_value={'text':'{}'}) as generate:
            result=provider.generate([{'role':'user','content':'hi'}],'role:long_implementation',
                                     think=False,format={'type':'object'},max_tokens=256)
        self.assertTrue(generate.call_args.kwargs['think'])
        self.assertGreaterEqual(generate.call_args.kwargs['options']['num_predict'],4096)
        self.assertEqual({'mode':'on','enabled':True,'reason':'user_mode'},result['thinking_decision'])

    def test_auto_uses_original_request_instead_of_inflated_context(self):
        from olcr_api.brain_models import brain_scope
        provider=RoleAwareModelProvider(Settings(context_auto_compress=False))
        with brain_scope({'mode':'unified','efforts':{'brain':'auto'}}), \
             patch.object(provider.ollama,'generate',return_value={'text':'hello'}) as generate:
            provider.generate([{'role':'user','content':'こんにちは\nReference evidence: '+('x'*1000)}],
                              'role:normal_chat',thinking_text='こんにちは')
        self.assertFalse(generate.call_args.kwargs['think'])

    def test_actual_verification_failure_selects_existing_debug_role_after_new_app_implementation(self):
        goal='空のプロジェクトからWebアプリを作成してください。失敗したテストは修正してください。'
        phase={'goal':'最終検証を行う'}
        self.assertEqual(app.settings.long_implementation_model,app._coding_phase_model(phase,task_goal=goal))
        self.assertEqual(app.settings.short_coding_model,
            app._coding_phase_model(phase,task_goal=goal,repairing_verification=True))
        self.assertEqual(app.settings.long_implementation_model,
            app._coding_phase_model(phase,task_goal='Use role:long_implementation for this task.',repairing_verification=True))

    def test_new_application_with_test_remediation_does_not_become_a_short_fix(self):
        goal='空のプロジェクトから昼食ルーレットを新規作成してください。実装後にテストを実行し、失敗があれば修正してください。'
        self.assertEqual(app.settings.long_implementation_model,
            app._coding_phase_model({'goal':'Create the UI and logic'},task_goal=goal))
        self.assertEqual(app.settings.short_coding_model,
            app._coding_phase_model({'goal':'Fix the observed syntax error'},task_goal=goal))

    def test_bonsai_forwards_structured_format_to_native_worker(self):
        provider = RoleAwareModelProvider(Settings())
        schema = {'type':'object','properties':{'operations':{'type':'array'}},'required':['operations']}
        with patch.object(provider,'_run_mlx',return_value={'text':'{}'}) as run:
            provider.generate([], 'role:short_coding_fix_debug', format=schema)
        self.assertEqual(schema,run.call_args.args[1]['format'])

    def test_complete_json_boundary_does_not_accept_prose_or_partial_code(self):
        from olcr_api.mlx_worker import complete_json_response
        schema={'required':['operations']}
        raw=json.dumps({'operations':[{'op':'write','path':'a.js','content':'const value = {label: "}"};\n'}]})
        self.assertEqual(raw,complete_json_response(raw+' trailing prose',schema))
        self.assertEqual(raw,complete_json_response('```json\n'+raw,schema))
        self.assertIsNone(complete_json_response('Here is code: '+raw,schema))
        self.assertIsNone(complete_json_response(raw[:-3],schema))
        self.assertIsNone(complete_json_response('{}',schema))

    def test_ornith_mlx_forwards_schema_and_explicit_token_budget(self):
        from dataclasses import replace
        provider = RoleAwareModelProvider(Settings())
        binding = replace(provider._binding("role:long_implementation"),
                          model_id="mlx:ornith_1_5_9b_8bit", runtime="mlx")
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        with patch.object(provider, "_binding", return_value=binding), \
             patch.object(provider, "_run_mlx", return_value={"text": "{}", "model": binding.deployment_model}) as run:
            provider.generate([], "role:long_implementation", think=False, format=schema, max_tokens=4096)
        self.assertEqual(4096, run.call_args.args[1]["max_tokens"])
        self.assertEqual(schema, run.call_args.args[1]["format"])
        self.assertFalse(run.call_args.args[1]["think"])

    def test_gui_vision_worker_is_reused_and_serializes_responses(self):
        provider = RoleAwareModelProvider(Settings())
        launched = []
        worker_code = (
            "import json,sys; "
            "[(lambda p: print(json.dumps({'text':json.load(open(p))['prompt']}),flush=True))(line.strip()) "
            "for line in sys.stdin]"
        )

        def start_worker():
            worker = subprocess.Popen([sys.executable, "-u", "-c", worker_code], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            launched.append(worker)
            return worker

        with patch.object(provider, "_start_vision_worker", side_effect=start_worker):
            first = provider._run_mlx_vision({"prompt": "first", "model": "ui_venus_mlx_6bit", "image": "unused"})
            second = provider._run_mlx_vision({"prompt": "second", "model": "ui_venus_mlx_6bit", "image": "unused"})

        self.assertEqual("first", first["text"])
        self.assertEqual("second", second["text"])
        self.assertEqual("dedicated_mlx_persistent", second["model_runtime"])
        self.assertEqual(1, len(launched))
        self.assertIsNone(launched[0].poll())
        provider.close()
        self.assertIsNotNone(launched[0].poll())

    def test_mlx_timeout_preserves_payload_free_worker_progress(self):
        provider = RoleAwareModelProvider(Settings())
        worker_progress = (
            'OLCR_MLX_PROGRESS={"event": "bonsai_generation_progress", '
            '"generated_tokens": 128, "max_tokens": 4096}'
        )
        timeout = subprocess.TimeoutExpired(["mlx_worker"], 750, stderr=worker_progress)
        with patch.object(provider, "_mlx_python", return_value=Path("/tmp/fake-mlx-python")), \
             patch("olcr_api.model_provider.subprocess.run", side_effect=timeout):
            with self.assertRaises(ModelFailure) as raised:
                provider._run_mlx("bonsai", {"messages": [], "max_tokens": 4096})
        self.assertEqual("timeout", raised.exception.category)
        self.assertIn("bonsai_generation_progress", str(raised.exception))
        self.assertIn("generated_tokens", str(raised.exception))

    def test_managed_mlx_wait_respects_explicit_coding_deadline(self):
        from olcr_api.ollama import inference_timeout, unbounded_inference
        provider=RoleAwareModelProvider(Settings())
        completed=subprocess.CompletedProcess(['mlx_worker'],0,stdout='{"text":"ok"}',stderr='')
        with unbounded_inference(),inference_timeout(30),patch.object(provider,'_mlx_python',return_value=Path('/tmp/fake-mlx-python')),patch('olcr_api.model_provider.subprocess.run',return_value=completed) as run:
            self.assertEqual('ok',provider._run_mlx('bonsai',{'messages':[]})['text'])
        self.assertGreater(run.call_args.kwargs['timeout'],0)
        self.assertLessEqual(run.call_args.kwargs['timeout'],30)

    def test_managed_structured_plan_has_room_for_complete_json(self):
        from olcr_api.ollama import unbounded_inference
        provider=RoleAwareModelProvider(Settings())
        with unbounded_inference(),patch.object(provider,'_run_mlx',return_value={'text':'{}'}) as run:
            provider.generate([], 'role:planner_replanner', format={'properties':{'phases':{}}})
        self.assertEqual(8192,run.call_args.args[1]['max_tokens'])

    def test_managed_source_batch_budget_is_not_clipped_to_short_chat_default(self):
        from olcr_api.ollama import unbounded_inference
        from contextlib import nullcontext
        provider=RoleAwareModelProvider(Settings())
        operations={'type':'object','properties':{'operations':{'type':'array'}}}
        for managed,schema,requested,expected in ((True,operations,10240,10240),
                (True,operations,32768,16384),(False,operations,10240,4096),
                (True,{'properties':{'edits':{'type':'array'}}},10240,10240),
                (True,{'properties':{'answer':{}}},10240,4096)):
            with self.subTest(managed=managed,requested=requested,schema=schema):
                with (unbounded_inference() if managed else nullcontext()),\
                     patch.object(provider,'_run_mlx',return_value={'text':'{}'}) as run:
                    provider.generate([], 'role:short_coding_fix_debug',format=schema,max_tokens=requested)
                self.assertEqual(expected,run.call_args.args[1]['max_tokens'])
                self.assertEqual(schema,run.call_args.args[1]['format'])

    def test_mlx_workers_do_not_inherit_backend_python_environment(self):
        provider = RoleAwareModelProvider(Settings())
        command = ["/tmp/fake-mlx-python", "mlx_worker.py"]
        completed = subprocess.CompletedProcess(command, 0, stdout='{"text":"ok"}', stderr="")
        with patch.dict(os.environ, {"PYTHONPATH": "/venv312/site-packages", "PYTHONHOME": "/venv312"}), \
             patch.object(provider, "_mlx_python", return_value=Path(command[0])), \
             patch("olcr_api.model_provider.subprocess.run", return_value=completed) as run:
            provider._run_mlx("ornith", {"messages": []})
        worker_env = run.call_args.kwargs["env"]
        self.assertNotIn("PYTHONPATH", worker_env)
        self.assertNotIn("PYTHONHOME", worker_env)

        with patch.dict(os.environ, {"PYTHONPATH": "/venv312/site-packages", "PYTHONHOME": "/venv312"}), \
             patch.object(provider, "_mlx_python", return_value=Path(command[0])), \
             patch("olcr_api.model_provider.subprocess.Popen") as popen:
            provider._start_vision_worker()
        worker_env = popen.call_args.kwargs["env"]
        self.assertNotIn("PYTHONPATH", worker_env)
        self.assertNotIn("PYTHONHOME", worker_env)

    def test_replanner_uses_smaller_bounded_generation_budget_than_short_repair(self):
        provider = RoleAwareModelProvider(Settings())
        for role, expected_tokens in (("role:planner_replanner", 2048),
                                      ("role:short_coding_fix_debug", 4096)):
            binding = provider._binding(role)
            captured = {}
            def run_mlx(operation, payload):
                captured.update(operation=operation, payload=payload)
                return {"text":"{}", "model":binding.deployment_model}
            with patch.object(provider, "_run_mlx", side_effect=run_mlx):
                result = provider.generate([], role, think=False, format={"type":"object"})
            self.assertEqual(expected_tokens, captured["payload"]["max_tokens"])
            self.assertEqual("bonsai", captured["operation"])
            self.assertEqual(binding.deployment_model, result["model"])

    def test_short_repair_role_survives_neutral_planner_paraphrase(self):
        phase = {"goal": "Add the missing hashlib import and verify the checksum."}
        original_goal = "この短いバグを修正してください。hashlib の import を追加してください。"
        self.assertEqual(
            "role:short_coding_fix_debug",
            app._coding_phase_model(phase, task_goal=original_goal),
        )

    def test_structural_output_repair_switches_to_a_distinct_configured_model(self):
        configuration={"mode":"split","selections":{
            "coder":"ollama:ornith-1.5:9b","short_coding":"mlx:ternary_bonsai_2_27b"}}
        with patch.object(app.brain_models,"configuration",return_value=configuration):
            self.assertEqual("role:short_coding_fix_debug",
                app._alternate_coding_model("role:long_implementation","conversation"))
            self.assertEqual("role:long_implementation",
                app._alternate_coding_model("role:short_coding_fix_debug","conversation"))

    def test_structural_output_repair_does_not_repeat_the_same_resolved_model(self):
        configuration={"mode":"unified","selections":{"brain":"ollama:ornith-1.5:9b"}}
        with patch.object(app.brain_models,"configuration",return_value=configuration):
            self.assertIsNone(app._alternate_coding_model("role:long_implementation","conversation"))

    def test_long_implementation_role_remains_for_feature_work(self):
        phase = {"goal": "Implement the multi-file project feature."}
        self.assertEqual("role:long_implementation", app._coding_phase_model(phase))

    def test_runtime_retry_language_does_not_downgrade_long_implementation(self):
        phase = {"goal": "Implement the deterministic dependency-aware scheduler."}
        retry_request = (
            "Use role:short_coding_fix_debug for any short fixes mentioned in this "
            "runtime policy. Fix the failed checks and repair missing criteria."
        )
        self.assertEqual(
            "role:long_implementation",
            app._coding_phase_model(phase, retry_request),
        )

    def test_small_bounded_task_uses_short_coding_role_after_neutral_paraphrase(self):
        phase = {"goal": "Update E2E_SHORT_FIX_PROBE.py with calculate_discount and required assertions."}
        request = "Typed read-only verifier: python -B E2E_SHORT_FIX_PROBE.py"
        self.assertEqual(
            "role:short_coding_fix_debug",
            app._coding_phase_model(phase, request, task_size="SMALL"),
        )

    def test_explicit_long_role_overrides_small_task_policy(self):
        phase = {"goal": "Implement the bounded change with role:long_implementation."}
        self.assertEqual("role:long_implementation", app._coding_phase_model(phase, task_size="SMALL"))

    def test_user_selected_long_role_survives_conflicting_runtime_prompt(self):
        phase = {"goal": "Create the scheduler files."}
        task_goal = "Use the Ornith long_implementation role for this task."
        runtime_prompt = "Use role:short_coding_fix_debug for generic repair instructions."
        self.assertEqual(
            "role:long_implementation",
            app._coding_phase_model(phase, runtime_prompt, task_goal, task_size="SMALL"),
        )

    def test_explicit_short_coding_role_request_is_honored(self):
        phase = {"goal": "Create one isolated short implementation and verify it."}
        request = "Use the short_coding_fix_debug role (Bonsai) for the short implementation."
        self.assertEqual("role:short_coding_fix_debug", app._coding_phase_model(phase, task_goal=request))

    def test_explicit_long_implementation_role_overrides_repair_keywords(self):
        phase = {"goal": "Repair the test fixture and fix the failing scheduler tests."}
        request = "Use the long_implementation Ornith role for this multi-file repair."
        self.assertEqual("role:long_implementation", app._coding_phase_model(phase, task_goal=request))

    def test_short_explicit_planner_budget_is_capped_by_the_existing_role_limit(self):
        provider = RoleAwareModelProvider(Settings())
        captured = {}
        def run_mlx(operation, payload):
            captured.update(operation=operation, payload=payload)
            return {"text": "{}", "model": provider._binding("role:planner_replanner").deployment_model}
        with patch.object(provider, "_run_mlx", side_effect=run_mlx):
            provider.generate([], "role:planner_replanner", think=False, max_tokens=256)
        self.assertEqual("bonsai", captured["operation"])
        self.assertEqual(256, captured["payload"]["max_tokens"])

    def test_ornith_mlx_preserves_explicit_nonreasoning_control(self):
        from dataclasses import replace
        provider = RoleAwareModelProvider(Settings())
        binding = replace(provider._binding("role:long_implementation"),
                          model_id="mlx:ornith_1_5_9b_8bit", runtime="mlx")
        with patch.object(provider, "_binding", return_value=binding), \
                patch.object(provider, "_run_mlx", return_value={"text": "{}"}) as run:
            provider.generate([], "role:long_implementation", think=False)
        self.assertFalse(run.call_args.args[1]["think"])

    def test_long_implementation_invokes_ornith_and_records_provider_model(self):
        provider = RoleAwareModelProvider(Settings())
        provider.ollama.generate = lambda messages, model, **kwargs: {"text": "{}", "model": model}
        result = provider.generate([], "role:long_implementation", think=False, format={"type": "object"})
        self.assertEqual("ornith-1.5:9b", result["model"])
        self.assertEqual("long_implementation", result["model_role"])
        self.assertEqual("e5df7dcdd8a263994df62d610317e07be0d6af23f96fcbd8543273058fce575e", result["model_revision"])

    def test_ornith_structured_reasoning_budget_retries_same_model_without_thinking(self):
        provider=RoleAwareModelProvider(Settings())
        binding=provider._binding('role:long_implementation')
        schema={'type':'object','properties':{'answer':{'type':'string'}},
                'required':['answer'],'additionalProperties':False}
        for failure in ('reasoning_budget','output_limit'):
            with self.subTest(failure=failure), \
                 patch.object(provider,'_binding',return_value=binding), \
                 patch.object(provider.ollama,'generate',side_effect=[
                     ModelFailure(failure,'structured generation exhausted its thinking/output budget'),
                     {'text':'{"answer":"ok"}','model':binding.deployment_model}]) as generate:
                result=provider.generate([{'role':'user','content':'Return the required JSON.'}],
                    'role:long_implementation',think=True,format=schema,max_tokens=4096)
                self.assertEqual(2,generate.call_count)
                first,second=generate.call_args_list
                self.assertEqual(binding.deployment_model,first.args[1])
                self.assertEqual(binding.deployment_model,second.args[1])
                self.assertTrue(first.kwargs['think'])
                self.assertFalse(second.kwargs['think'])
                self.assertEqual(schema,first.kwargs['format'])
                self.assertEqual(schema,second.kwargs['format'])
                self.assertEqual('ok',json.loads(result['text'])['answer'])
                self.assertEqual({'after_failure':failure},result['structured_no_thinking_retry'])

    def test_manifest_revisions_match_live_ollama_digests(self):
        rows = [
            {"name": "embeddinggemma:latest", "digest": "82f094e4c0e19bc4208c692996d4f1ecf14e82a8dee579ee1d2ca1175260058e"},
            {"name": "ornith-1.5:9b", "digest": "e5df7dcdd8a263994df62d610317e07be0d6af23f96fcbd8543273058fce575e"},
            {"name": "qwen3-vl:2b-instruct", "digest": "ea422f1e73652a95479954d8572d3c8c6022f628ce2d38a1a04aae1b7f2d5300"},
        ]
        result = ModelRegistry().validate_ollama_inventory(rows)
        self.assertEqual("PASS", result["status"], result["errors"])
        self.assertEqual({"MATCH"}, {binding["status"] for binding in result["bindings"]})

    def test_manifest_rejects_installed_ollama_tag_with_different_digest(self):
        binding=ModelRegistry().binding('semantic_embedding')
        rows = [{"name": binding.deployment_model, "digest": "wrong" + "0" * 60}]
        result = ModelRegistry().validate_ollama_inventory(rows)
        self.assertEqual("FAIL", result["status"])
        self.assertIn(binding.model_id+":DIGEST_MISMATCH", result["errors"])

    def test_embedding_digest_prefix_is_normalized_but_real_mismatch_is_preserved(self):
        registry = ModelRegistry()
        digest = registry.binding('semantic_embedding').deployment_revision
        for actual, expected in [('sha256:' + digest.upper(), 'MATCH'), ('0' * 64, 'DIGEST_MISMATCH')]:
            result = registry.validate_ollama_inventory([{'name': 'embeddinggemma:latest', 'digest': actual}])
            row = next(row for row in result['bindings'] if row['model_id'] == 'embeddinggemma')
            self.assertEqual(expected, row['status'])

    def test_model_status_reports_live_manifest_digest_mismatch(self):
        binding=ModelRegistry().binding('semantic_embedding')
        payload = {"models": [{"name": binding.deployment_model, "digest": "wrong" + "0" * 60}]}
        with patch("olcr_api.app.urllib_request.urlopen", return_value=BytesIO(json.dumps(payload).encode())):
            status = app.model_status()
        self.assertEqual("FAIL", status["runtime_manifest_validation"]["status"])
        self.assertIn(binding.model_id+":DIGEST_MISMATCH", status["runtime_manifest_validation"]["errors"])

    def test_health_exposes_runtime_manifest_validation(self):
        rows = [
            {"name": "embeddinggemma:latest", "digest": "82f094e4c0e19bc4208c692996d4f1ecf14e82a8dee579ee1d2ca1175260058e"},
            {"name": "ornith-1.5:9b", "digest": "e5df7dcdd8a263994df62d610317e07be0d6af23f96fcbd8543273058fce575e"},
            {"name": "qwen3-vl:2b-instruct", "digest": "ea422f1e73652a95479954d8572d3c8c6022f628ce2d38a1a04aae1b7f2d5300"},
        ]
        with patch.object(app, "_live_ollama_models", return_value=rows):
            status = app.health()
        self.assertEqual("PASS", status["runtime_manifest_validation"]["status"])

    def test_long_implementation_rejects_provider_model_mismatch(self):
        provider = RoleAwareModelProvider(Settings())
        provider.ollama.generate = lambda messages, model, **kwargs: {
            "text": "{}", "model": "ternary_bonsai_2_27b"
        }
        with self.assertRaises(ModelFailure) as raised:
            provider.generate([], "role:long_implementation", think=False, format={"type": "object"})
        self.assertEqual("model_mismatch", raised.exception.category)

    def test_default_roles_use_one_primary_and_one_router(self):
        settings = Settings()
        self.assertEqual("ornith-1.5:9b", DEFAULT_MAIN_MODEL)
        self.assertEqual("laya-choice", DEFAULT_ROUTER_MODEL)
        self.assertEqual(DEFAULT_MAIN_MODEL, settings.main_model)
        self.assertEqual(DEFAULT_ROUTER_MODEL, settings.router_model)

    def test_environment_roles_are_independently_configurable(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "brain-test", "OLLAMA_ROUTER_MODEL": "router-test"}, clear=False):
            settings = Settings.from_env()
        self.assertEqual("brain-test", settings.main_model)
        self.assertEqual("router-test", settings.router_model)

    def test_legacy_coding_setting_and_primary_model_are_migrated(self):
        settings = Settings().with_overrides({"coding_model": "qwen3:14b", "main_model": "qwen3:14b"})
        self.assertEqual("ornith-1.5:9b", settings.main_model)
        self.assertNotIn("coding_model", settings.public_dict())

    def test_legacy_primary_model_is_not_used_without_coding_key(self):
        self.assertEqual("ornith-1.5:9b", Settings().with_overrides({"main_model": "qwen3:14b"}).main_model)

    def test_router_candidate_excludes_normal_chat(self):
        self.assertFalse(app._external_router_candidate("Explain recursion in simple terms"))
        self.assertTrue(app._external_router_candidate("find the latest exchange rate"))
        self.assertFalse(app._external_router_candidate("search forced-failure and summarize"))
        self.assertTrue(app._external_router_candidate("check Rosneft using an available API"))

    def test_explicit_tool_execution_prohibition_blocks_candidate_routing(self):
        prompt = "API/tool候補を検索して、何も実行・変更・インストールしないでください。"
        self.assertTrue(app._tool_execution_prohibited(prompt))
        self.assertFalse(app._external_router_candidate(prompt))
        self.assertIsNone(app.router_decision(prompt))
        self.assertTrue(app._tool_execution_prohibited("Find an API candidate, but do not execute any tools."))

        # Canonical E2E-G wording: candidate retrieval is allowed, execution is not.
        acceptance_prompt = (
            "Use EmbeddingGemma for repo/document, API/tool, and Skill candidate retrieval only. "
            "Do not execute anything."
        )
        self.assertTrue(app._tool_execution_prohibited(acceptance_prompt))
        self.assertFalse(app._external_router_candidate(acceptance_prompt))
        self.assertIsNone(app.router_decision(acceptance_prompt))

        canonical_g_prompt = (
            "Search this Tetris project's source and local documentation semantically for the definition of "
            "format_total and its tests. Use local repo/document retrieval only; do not call external APIs. "
            "Return up to 3 candidate paths and supporting passages. Do not edit files or run commands."
        )
        self.assertTrue(app._tool_execution_prohibited(canonical_g_prompt))
        self.assertTrue(app._candidate_retrieval_only_requested(canonical_g_prompt))

    def test_workspace_candidate_retrieval_is_semantic_read_only_and_returns_passages(self):
        prompt = "Search this Tetris project's source and local documentation semantically for format_total and its tests."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src" / "ledger.py"
            source.parent.mkdir()
            source.write_text("def format_total(items):\n    return sum(item['amount'] for item in items)\n\nclass TestFormatTotal:\n    def test_format_total_empty(self):\n        assert format_total([]) == 0\n")
            (root / "README.md").write_text("A small Tetris project with a score panel.\n")

            class NoShellVectorStore:
                def search(self, *_args, **_kwargs):
                    raise AssertionError("workspace candidate retrieval must not call the indexed/global search")

            with patch.object(app, "settings", app.settings.with_overrides({"allowed_roots": (temporary,)})), \
                 patch.object(app, "vectors", NoShellVectorStore()), \
                 patch.object(app, "_api_embedding_provider") as provider, \
                 patch.object(app.subprocess, "run", side_effect=AssertionError("retrieval must not run commands")), \
                 patch.object(app.skills, "search", return_value=[]), \
                 patch.object(app.skills, "cli_inventory", return_value=[]), \
                 patch.object(app.laya_provider, "choice", side_effect=AssertionError("retrieval-only must not call Laya")):
                provider.embed.side_effect = lambda texts, _model: [[1.0, 0.0] for _ in texts]
                results = app._retrieve_candidates_only(prompt, workspace_root=temporary)
                response = app._candidate_retrieval_only_response(prompt, workspace_root=temporary)

        self.assertEqual("PASS", results["embedding_status"]["documents"])
        self.assertTrue(results["document_candidates"])
        source_candidates = [candidate for candidate in results["document_candidates"]
                             if Path(candidate["source"]).resolve() == source.resolve()]
        self.assertTrue(source_candidates)
        self.assertEqual(str(source.resolve()), str(Path(results["document_candidates"][0]["source"]).resolve()))
        self.assertIn("def format_total", results["document_candidates"][0]["snippet"])
        self.assertIn("def test_format_total", results["document_candidates"][0]["snippet"])
        self.assertIn("line 1", response["response"])
        self.assertIn("format_total", response["response"])

    def test_candidate_only_retrieval_ranks_api_and_skill_candidates_without_loading(self):
        prompt = "Use EmbeddingGemma for repo/document, API/tool, and Skill candidate retrieval only. Do not execute anything."

        class FakeVectors:
            state = "ready"
            def search(self, query, limit):
                return [SimpleNamespace(source="docs/guide.md", snippet="local document candidate", score=.9,
                                        line_start=3, method="semantic")]

        def embed(texts, model):
            return [[1.0, 0.0] if index == 0 else ([1.0, 0.0] if index == 1 else [0.0, 1.0])
                    for index, _ in enumerate(texts)]

        active = {"name": "active-skill", "description": "candidate skill", "source": "project", "state": "ACTIVE", "enabled": True}
        cli = {"name": "cli-skill", "description": "candidate skill from local CLI", "source": "skills-cli-local",
               "state": "AVAILABLE_FOR_QUARANTINE_INTAKE", "enabled": False}
        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "settings", app.settings.with_overrides({"vector_enabled": True})), \
             patch.object(app, "_api_embedding_provider") as provider, \
             patch.object(app.skills, "search", return_value=[active]), \
             patch.object(app.skills, "cli_inventory", return_value=[cli]), \
             patch.object(app.skills, "load", side_effect=AssertionError("candidate retrieval must not load a Skill")), \
             patch.object(app.laya_provider, "choice", side_effect=AssertionError("candidate retrieval must not select with Laya")):
            provider.embed.side_effect = embed
            results = app._retrieve_candidates_only(prompt)

        self.assertEqual(1, len(results["document_candidates"]))
        self.assertEqual("PASS", results["embedding_status"]["api"])
        self.assertEqual(8, len(results["api_candidates"]))
        self.assertEqual("PASS", results["embedding_status"]["skills"])
        self.assertEqual({"active-skill"}, {row["name"] for row in results["skill_candidates"]},
                         "the orthogonal CLI skill must be removed by the configured relevance threshold")
        self.assertEqual(2, provider.embed.call_count)

    def test_router_abstains_without_a_manifest_candidate(self):
        self.assertIsNone(app.router_decision("find an external data source"))

    def test_registry_inventory_is_typed_and_explicit(self):
        rows = external_tools.status(True)
        self.assertEqual(set(external_tools.REGISTRY), {row["tool_id"] for row in rows})
        self.assertEqual(len(PRODUCTION_ACCEPTANCE_CASES), len(rows))
        # Only the providers that passed the current production gate are
        # exposed in this inventory; excluded historical fixture entries stay hidden.
        self.assertEqual(len(PRODUCTION_ACCEPTANCE_CASES), sum(1 for row in rows if row["external_network"]))
        self.assertEqual(0, sum(1 for row in rows if not row["external_network"]))
        self.assertFalse(external_tools.REGISTRY["math.symbolic"].external)
        self.assertFalse(external_tools.REGISTRY["math.numeric"].external)
        for row in rows:
            with self.subTest(tool_id=row["tool_id"]):
                self.assertIn(row["availability"], {"READY", "DISABLED", "UNAVAILABLE", "CONFIG_REQUIRED", "IMPLEMENTATION_REQUIRED"})
                self.assertIn(row["credential"], {"none", "optional", "required", "Configured"})
                self.assertIn(row["execution_type"], {"HTTP_GET", "LOCAL_TOOL"})

    def test_manifest_acceptance_prompts_route_to_their_canonical_tools(self):
        for case in PRODUCTION_ACCEPTANCE_CASES:
            with self.subTest(tool_id=case["tool_id"]):
                decision = external_tools.route(case["user_prompt"])
                self.assertIsNotNone(decision)
                self.assertEqual(case["tool_id"], decision[0])

    def test_router_no_candidate_is_safe(self):
        self.assertIsNone(app.router_decision("Japanについて調べて"))
        self.assertIsNone(app.router_decision("こんにちは"))
        self.assertFalse(app._external_router_candidate("こんにちは"))
        self.assertFalse(app._external_router_candidate("Pythonのfor文を説明して"))
