import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FTSRetriever, FileRetriever, RetrievalRouter
from olcr_api.runtime import ContextManager, Runtime, _validate_operation_path_shape


class ExplicitSkillContextTests(unittest.TestCase):
    def test_optional_provider_arguments_preserve_frozen_role_and_schema(self):
        calls=[]
        class LegacyProvider:
            def generate(self,messages,model,think=None,format=None):
                calls.append((model,think,format))
                return {'text':'{}'}
        runtime=object.__new__(Runtime)
        runtime.settings=SimpleNamespace(main_model='normal-chat')
        runtime.model=LegacyProvider()
        schema={'type':'object'}
        runtime._generate_brain([], 'Implement fixture', structured_schema=schema,
                                thinking_override=False,model_name='role:long_implementation',max_tokens=8192)
        self.assertEqual([('role:long_implementation',False,schema)],calls)

    def test_internal_provider_typeerror_does_not_repeat_or_change_model(self):
        calls=[]
        class Provider:
            def generate(self,messages,model,**kwargs):
                calls.append(model)
                raise TypeError('internal provider failure')
        runtime=object.__new__(Runtime)
        runtime.settings=SimpleNamespace(main_model='normal-chat')
        runtime.model=Provider()
        with self.assertRaisesRegex(TypeError,'internal provider failure'):
            runtime._generate_brain([], 'Implement fixture', structured_schema={'type':'object'},
                thinking_override=False,model_name='role:long_implementation',max_tokens=8192)
        self.assertEqual(['role:long_implementation'],calls)

    def test_implementation_contract_does_not_trigger_unrelated_self_grounding(self):
        runtime=object.__new__(Runtime)
        runtime.settings=SimpleNamespace(main_model='normal-chat')
        runtime.model=SimpleNamespace(generate=lambda *args,**kwargs:{'text':'{}'})
        with patch.object(runtime,'_self_context',side_effect=AssertionError('coding context already supplied')):
            for field in ('operations','edits'):
                runtime._generate_brain([], 'OLCR will run the acceptance checks.',
                    structured_schema={'properties':{field:{'type':'array'}}},
                    thinking_override=False,model_name='role:long_implementation')

    def test_commonjs_sources_are_checked_without_executing_them(self):
        diagnostics=[]
        self.assertTrue(Runtime._structural_ok(Path('module.cjs'),'module.exports={value:1};',{},diagnostics))
        self.assertFalse(Runtime._structural_ok(Path('module.cjs'),'module.exports={value:};',{},diagnostics))
        self.assertIn('SyntaxError',diagnostics[0])

    def test_commonjs_binding_diagnostics_use_actual_named_imports_and_writable_sources(self):
        sources={'test.test.js':"const {Engine}=require('./logic');",'logic.js':'class Engine {}'}
        facts=Runtime._commonjs_binding_facts(sources,{'logic.js'})
        self.assertEqual(1,len(facts))
        self.assertIn('Engine',facts[0])
        self.assertIn('module.exports',facts[0])
        self.assertEqual([],Runtime._commonjs_binding_facts(sources,set()))
        sources['logic.js']='class Engine {};module.exports={Engine};'
        self.assertEqual([],Runtime._commonjs_binding_facts(sources,{'logic.js'}))

    def test_normal_chat_context_reinforces_explicit_length_and_count_limits(self):
        request = "日本語で、提案を一つだけ2文以内で返してください。"

        messages, _ = ContextManager(4000).build(request, [])

        system_text = "\n".join(
            message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Follow explicit response length and sentence-count limits exactly", system_text)
        self.assertIn("when asked for one suggestion, give only one", system_text)

    def test_workspace_citations_are_rendered_relative_to_the_selected_project(self):
        root = Path("/private/tmp/olcr-project")
        answer = "See /private/tmp/olcr-project/backend/olcr_api/app.py:5685 for the function."
        self.assertEqual(
            "See backend/olcr_api/app.py:5685 for the function.",
            Runtime._workspace_relative_citations(answer, str(root)),
        )

    def test_normal_chat_workspace_citations_are_also_rendered_relative(self):
        class CitationModel:
            def generate(self, messages, model, **kwargs):
                return {"text": f"See {root}/backend/olcr_api/app.py:5685.", "latency_ms": 0}

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            db = Database(str(Path(temporary) / "olcr.sqlite"))
            db.initialize()
            settings = Settings(allowed_roots=(str(root),), db_path=db.path,
                                main_model="citation-test").validated()
            retrieval = RetrievalRouter(FileRetriever([str(root)]), FTSRetriever(db),
                                        DisabledVectorStore(), False)
            runtime = Runtime(settings, db, retrieval, CitationModel())

            task, response = runtime.execute(
                "Explain the selected local source briefly.", workspace_root=str(root))

            self.assertEqual("completed", task.state.value)
            self.assertNotEqual("RETRIEVAL", task.route.value)
            self.assertEqual("See backend/olcr_api/app.py:5685.", response)

    def test_local_source_summary_context_names_declarations_and_separates_helpers(self):
        request = (
            "Search for the function that retrieves Skill candidates and summarize its ranking stages. "
            "Use only this local repository."
        )
        evidence = [SimpleNamespace(
            source="/workspace/backend/olcr_api/app.py", line=5685,
            snippet="def _automatic_skill_selection(message):\\n    rows = skills.search()",
            method="ranked_source", score=4.25,
        )]
        messages, _ = ContextManager(4000).build(request, evidence)
        system_text = "\\n".join(
            message["content"] for message in messages if message["role"] == "system")

        self.assertIn("Use the complete numbered function source when present", system_text)
        self.assertIn("workspace-relative source path", system_text)
        self.assertIn("keep its ranking stages separate from error handling", system_text)
        self.assertIn("Preserve the actual operands in comparisons", system_text)
        self.assertIn("only line numbers shown beside the source lines", system_text)

    def test_local_source_summary_expands_complete_target_function_from_workspace(self):
        request = (
            "Search for the function that retrieves Skill candidates and summarize its ranking stages. "
            "Use only this local repository."
        )
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            source_path = root / "backend" / "olcr_api" / "app.py"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "def _laya_request_language(message):\n    return 'english'\n\n"
                "def _automatic_skill_selection(message):\n"
                "    \"\"\"Select Skills after candidate retrieval.\"\"\"\n"
                "    rows = skills.search()\n"
                "    shortlist = lexical_shortlist(message, rows)\n"
                "    vectors = embed([message, *descriptions(shortlist)])\n"
                "    ranked = cosine_against_each_candidate(vectors)\n"
                "    if ranked[0].score < 0.4:\n        return None\n"
                "    selected = laya_provider.choice(ranked)\n"
                "    padding = '" + "x" * 6500 + "'\n"
                "    return selected\n\n"
                "def _retrieve_candidates_only(message):\n    return []\n",
                encoding="utf-8",
            )
            snippet = (
                "def _laya_request_language(message):\n    return 'english'\n"
                "\ndef _automatic_skill_selection(message):\n"
                "    \"\"\"Select Skills after candidate retrieval.\"\"\"\n"
                "    rows = skills.search()\n"
            )
            evidence = [SimpleNamespace(
                source=str(source_path), line=1, snippet=snippet,
                method="ranked_source", score=4.25,
            )]

            messages, selected = ContextManager(8000).build(
                request, evidence, core_context="Skill candidate ranking context. " * 300,
                workspace_root=str(root))

        self.assertEqual(1, len(selected))
        self.assertEqual("backend/olcr_api/app.py", selected[0]["source"])
        self.assertEqual("ranked_source_complete_function", selected[0]["method"])
        self.assertIn("cosine_against_each_candidate", selected[0]["text"])
        self.assertIn("laya_provider.choice", selected[0]["text"])
        self.assertGreater(len(selected[0]["text"]), 6500)
        self.assertIn("    return selected", selected[0]["text"])
        self.assertNotIn("_retrieve_candidates_only", selected[0]["text"])
        self.assertFalse(any("Workspace core context" in item["content"] for item in messages))
        self.assertIn("Describe that function's behavior in execution order", messages[-2]["content"])

    def test_unprocessed_tool_control_gets_bounded_plain_text_retries(self):
        class SequenceModel:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {"text": '<tool_call><function=search_files>{}</function></tool_call>'},
                    {"text": '<search><query>rank skills</query></search>'},
                    {"text": "The function ranks bounded Skill candidates by lexical and embedding relevance."},
                ]

            def generate(self, messages, model, **kwargs):
                self.calls.append((messages, model, kwargs))
                return self.responses.pop(0)

        runtime = object.__new__(Runtime)
        runtime.settings = SimpleNamespace(main_model="ornith-1.5:9b")
        runtime.model = SequenceModel()
        result = runtime._generate_brain(
            [{"role": "user", "content": "Summarize the local function."}],
            "Summarize the local function.",
            thinking_override=False,
        )

        self.assertEqual(3, len(runtime.model.calls))
        self.assertIn("emit XML, tags, commands", runtime.model.calls[1][0][0]["content"])
        self.assertIn("lexical and embedding relevance", result["text"])

    def test_prohibited_file_actions_do_not_route_skill_discovery_as_implementation(self):
        request = "Find an installed skill that helps with GUI design. Do not install anything or modify files."
        self.assertFalse(Runtime._implementation_intent(request.lower()))
        self.assertTrue(Runtime._implementation_intent("Build the UI and update its stylesheet."))

    def test_explicit_skill_body_is_not_cut_to_ambient_memory_quarter_budget(self):
        body = "# Find Skills\n" + ("Skill discovery guidance.\n" * 240)
        core = f"[ACTIVE_SKILL name=find-skills hash=verified]\n{body}\n[/ACTIVE_SKILL]"
        messages, _ = ContextManager(8000).build("Describe the loaded Skill.", [], core)
        supplied = "\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn(body, supplied)
        self.assertIn("[/ACTIVE_SKILL]", supplied)

    def test_skill_context_excludes_unrelated_project_facts(self):
        skill = "[ACTIVE_SKILL name=calculator]\nUse arithmetic only.\n[/ACTIVE_SKILL]"
        combined = "Project facts: Tetris drop interval is 800 ms.\n" + skill
        selected = ContextManager(8000)._select_core("Choose a calculator Skill.", combined)
        self.assertIn(skill, selected)
        self.assertNotIn("Tetris", selected)
        self.assertNotIn("800 ms", selected)

    def test_ordinary_project_context_keeps_existing_quarter_budget(self):
        context = "ordinary project memory " * 200
        selected = ContextManager(8000)._select_core("question", context)
        self.assertLessEqual(len(selected), 2000)


class OperationPathShapeTests(unittest.TestCase):
    def test_rejects_instruction_prose_without_mistaking_it_for_a_file(self):
        with self.assertRaisesRegex(RuntimeError, "descriptive prose"):
            _validate_operation_path_shape("Implement add, list, total, delete, and input validation")

    def test_accepts_concrete_and_extensionless_file_names(self):
        for path in ("ledger_core.py", "docs/README.md", "Makefile", "my notes.py"):
            with self.subTest(path=path):
                _validate_operation_path_shape(path)

class DeterministicFileModel:
    """Returns the production typed-file operation payload and nothing else."""

    def __init__(self):
        self.calls = []

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {
            "text": json.dumps(
                {
                    "change_required": True,
                    "source_inspected": True,
                    "condition_evaluated": True,
                    "reason_code": "runtime_probe",
                    "operations": [
                        {
                            "op": "write",
                            "path": "runtime_probe.txt",
                            "content": "OLCR_REAL_RUNTIME_PROBE",
                        }
                    ],
                    "verification": "read the created probe file",
                }
            ),
            "latency_ms": 0,
        }


class OutOfScopeFileModel(DeterministicFileModel):
    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {
            "text": json.dumps({
                "change_required": True,
                "source_inspected": True,
                "condition_evaluated": True,
                "reason_code": "scope_probe",
                "operations": [
                    {"op": "write", "path": "src/math.py", "content": "def add(a, b):\n    return a + b\n"},
                    {"op": "write", "path": "tests/test_math.py", "content": "changed\n"},
                ],
                "verification": "read back the authorized file",
            }),
            "latency_ms": 0,
        }


class OutsideRootFileModel(DeterministicFileModel):
    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [
            {"op": "write", "path": "../sentinel-b.txt", "content": "must not write"},
        ]}), "latency_ms": 0}


class PatchFileModel(DeterministicFileModel):
    def __init__(self, old="return a - b", new="return a + b"):
        super().__init__()
        self.old, self.new = old, new

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [{
            "op": "patch", "path": "src/math.py",
            "expected_old_fragment": self.old, "replacement_fragment": self.new,
        }]}), "latency_ms": 0}


class OperationBatchModel(DeterministicFileModel):
    def __init__(self, operations):
        super().__init__()
        self.operations = operations

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": self.operations}), "latency_ms": 0}


class PackageInstallModel(DeterministicFileModel):
    def __init__(self, workspace_root, packages=None):
        super().__init__()
        self.workspace_root = str(workspace_root)
        self.packages = packages or ["react"]

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [{
            "op": "package_install", "package_manager": "npm", "packages": self.packages,
            "workspace_root": self.workspace_root,
            "dependency_kind": "dependencies",
        }]}), "latency_ms": 0}


class PackageJsonPatchModel(DeterministicFileModel):
    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [{
            "op": "patch", "path": "package.json",
            "expected_old_fragment": "{}", "replacement_fragment": "{\"scripts\":{\"build\":\"vite build\"}}",
        }]}), "latency_ms": 0}


class PackageJsonScriptPatchModel(DeterministicFileModel):
    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [{
            "op": "patch", "path": "package.json",
            "expected_old_fragment": "{}", "replacement_fragment": "{\"scripts\":{\"build\":\"vite build\"}}",
        }]}), "latency_ms": 0}


class FakePackageExecutor:
    def __init__(self):
        self.calls = []

    def install(self, **kwargs):
        self.calls.append(kwargs)
        return {"operation_type": "PACKAGE_INSTALL", "package_manager": kwargs.get("package_manager") or "npm",
                "packages_requested": kwargs["packages"], "packages_authorized": kwargs["packages"],
                "process_started": True, "exit_code": 0, "timeout": False, "cancelled": False,
                "package_json_after": {"exists": True}, "lockfile_after": {}, "elapsed_ms": 1,
                "status": "PASS"}


class FailingPackageExecutor(FakePackageExecutor):
    def install(self, **kwargs):
        self.calls.append(kwargs)
        from olcr_api.package_manager import PackageManagerError
        raise PackageManagerError("registry failed", failure_class="PACKAGE_MANAGER_NONZERO_EXIT",
                                  evidence={"process_started": True, "exit_code": 7, "status": "FAIL"})


class RealRuntimeManagedContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace-a"
        self.workspace.mkdir()
        self.sentinel = self.base / "sentinel-b.txt"
        self.sentinel.write_text("OUTSIDE_SENTINEL_ORIGINAL", encoding="utf-8")
        self.db = Database(str(self.base / "olcr.sqlite"))
        self.db.initialize()
        settings = Settings(
            allowed_roots=(str(self.workspace),),
            db_path=self.db.path,
            main_model="deterministic-runtime-test",
        ).validated()
        retrieval = RetrievalRouter(
            FileRetriever([str(self.workspace)]),
            FTSRetriever(self.db),
            DisabledVectorStore(),
            False,
        )
        self.model = DeterministicFileModel()
        self.runtime = Runtime(settings, self.db, retrieval, self.model)
        self.managed_context = {
            "managed_coding_task": True,
            "operation_intent": "IMPLEMENTATION",
            "global_no_write": False,
            "workspace_root": str(self.workspace),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_skill_reaches_generation_with_instructions_and_real_project_evidence(self):
        from olcr_api.explicit_skills import invocation
        (self.workspace / 'README.md').write_text('Fixture project stores meals offline.')
        loaded = {'skill': {'name': 'reviewer'}, 'content': 'Give one concrete storage recommendation.'}
        request, core = invocation(loaded, '', {'project_id': 'fixture'}, str(self.workspace))
        class AnswerModel:
            def generate(inner, messages, model, **kwargs):
                supplied = '\n'.join(item['content'] for item in messages)
                self.assertIn('Give one concrete storage recommendation.', supplied)
                self.assertIn('Fixture project stores meals offline.', supplied)
                self.assertIn('[/ACTIVE_SKILL]', supplied)
                return {'text': '保存失敗を画面で通知する処理を追加してください。', 'latency_ms': 0}
        self.runtime.model = AnswerModel()
        task, answer = self.runtime.execute(request, core_context=core, workspace_root=str(self.workspace),
                                            suppress_web=True)
        self.assertEqual('completed', task.state.value)
        self.assertEqual('保存失敗を画面で通知する処理を追加してください。', answer)

    def test_cancelled_buffered_generation_returns_cancelled_without_error_answer(self):
        from olcr_api.generation_control import Generation, generation_scope, check_cancelled
        generation = Generation()
        def stop(*args, **kwargs):
            generation.cancel()
            check_cancelled()
        with generation_scope(generation), patch.object(self.model, 'generate', side_effect=stop):
            task, answer = self.runtime.execute('Explain the project.', suppress_web=True,
                                                workspace_root=str(self.workspace))
        self.assertEqual('cancelled', task.state.value)
        self.assertEqual('', answer)
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_explicit_skill_can_execute_a_file_change_in_the_current_workspace(self):
        from olcr_api.explicit_skills import invocation
        loaded = {'skill': {'name': 'editor'}, 'content': 'Write the requested file in the current workspace.'}
        request, core = invocation(loaded, 'Create the file runtime_probe.txt in the workspace with content OLCR_REAL_RUNTIME_PROBE.',
                                   {}, str(self.workspace))
        task, answer = self.runtime.execute(request, approved=True, core_context=core,
                                            workspace_root=str(self.workspace))
        self.assertEqual('completed', task.state.value, answer)
        self.assertEqual('OLCR_REAL_RUNTIME_PROBE', (self.workspace / 'runtime_probe.txt').read_text())
        supplied = '\n'.join(item['content'] for item in self.model.calls[0]['messages'])
        self.assertIn('Write the requested file in the current workspace.', supplied)
        self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL', self.sentinel.read_text())

    def test_unchanged_patch_repair_uses_complete_writes_only_for_small_authorized_files(self):
        for paths in [['probe.py'],['probe.py','other.py']]:
            with self.subTest(paths=paths):
                for name in paths:(self.workspace/name).write_text('answer = 1\n')
                model=OperationBatchModel([{'op':'write','path':'probe.py','content':'answer = 2\n'}])
                runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
                task,response=runtime.execute('Fix the observed test failure',workspace_root=str(self.workspace),
                    managed_context={**self.managed_context,'implementation_plan_artifact':{
                        'file_manifest':[{'path':name,'action':'modify'} for name in paths]},
                        'repair_diagnostics':{'verification_failures':[{'detail':'error: expected 2'}],
                            'unchanged_paths':[str(self.workspace/'probe.py'),str(self.sentinel)]}})
                self.assertEqual('completed',task.state.value,response)
                self.assertEqual('answer = 2\n',(self.workspace/'probe.py').read_text())
                self.assertEqual(2,len(model.calls))
                self.assertIsNone(model.calls[0]['format'])
                call=model.calls[-1]
                self.assertEqual(model.calls[0]['model'],call['model'])
                self.assertIn('REPAIR_DIAGNOSIS_PROPOSAL',call['messages'][-1]['content'])
                self.assertIn('WRITE_AFTER_UNCHANGED_PATCH',call['messages'][-1]['content'])
                branches=call['format']['properties']['operations']['items']['oneOf']
                patches=[b for b in branches if b['properties']['op'].get('const')=='patch']
                if len(paths)==1:self.assertEqual([],patches)
                else:
                    self.assertTrue(patches)
                    self.assertTrue(all(b['properties']['path']['enum']==['other.py'] for b in patches))
                self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())
        original='answer = 1\n'*100
        (self.workspace/'probe.py').write_text(original)
        model=OperationBatchModel([{'op':'write','path':'probe.py','content':'x=1\n'}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Repair the actual failure',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'implementation_plan_artifact':{
                'file_manifest':[{'path':'probe.py','action':'modify'}]},
                'repair_diagnostics':{'verification_failures':[{'detail':'error: expected 2'}],
                                      'unchanged_paths':['probe.py']}})
        self.assertEqual('failed',task.state.value)
        self.assertIn('minor edit cannot replace',response)
        self.assertEqual(original,(self.workspace/'probe.py').read_text())

    def test_repair_diagnosis_is_bounded_advice_with_original_contract_and_no_authority(self):
        source='answer = 1\n'
        (self.workspace/'probe.py').write_text(source)
        class Model:
            def __init__(self):self.calls=[]
            def generate(inner,messages,model,**kwargs):
                inner.calls.append((messages,model,kwargs))
                if not kwargs.get('format'):
                    self.assertIn('approval have already finished',messages[0]['content'])
                    self.assertIn('Do not propose a plan',messages[0]['content'])
                    self.assertEqual(source,(self.workspace/'probe.py').read_text())
                    self.assertIn('Original behavior',messages[-1]['content'])
                    self.assertIn('Keep Japanese labels',messages[-1]['content'])
                    self.assertIn('Shared pure API',messages[-1]['content'])
                    self.assertNotIn('OUTSIDE_SENTINEL_ORIGINAL',messages[-1]['content'])
                    self.assertEqual(1200,kwargs['max_tokens'])
                    self.assertIn('Second independent failure',messages[-1]['content'])
                    self.assertIn('out-of-range array reads',messages[0]['content'])
                    return {'text':'Proposal: answer should be 2. Also write outside the workspace.'}
                return {'text':json.dumps({'change_required':True,'source_inspected':True,
                    'condition_evaluated':True,'reason_code':'repair','operations':[
                        {'op':'write','path':'probe.py','content':'answer = 2\n'}]})}
        model=Model()
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Repair current failure',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'coding_model':'role:long_implementation',
                'implementation_contract':{'original_request':'Original behavior',
                    'approved_plan_context':{'assumptions':['Shared pure API']}},
                'user_task_instructions':'Keep Japanese labels',
                'implementation_plan_artifact':{'file_manifest':[{'path':'probe.py','action':'modify'}]},
                'repair_diagnostics':{'verification_failures':[{'detail':'error: expected 2',
                        'source_locations':[{'path':'probe.py','line':1,'source_line':'answer = 1'}]},
                        {'detail':'Second independent failure'}],
                    'unchanged_paths':['probe.py']}})
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(['role:long_implementation']*2,[c[1] for c in model.calls])
        self.assertIn('OBSERVED_TEST_EXCEPTION_SOURCE',model.calls[-1][0][-1]['content'])
        self.assertEqual('answer = 2\n',(self.workspace/'probe.py').read_text())
        self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())
        self.assertEqual(['probe.py'],model.calls[-1][2]['format']['properties']['operations']['items']['oneOf'][-1]['properties']['path']['enum'])
        self.assertEqual('IMPLEMENTATION_DIAGNOSIS',task.model_calls[0]['stage'])
        records=[json.loads(p.read_text()) for p in (self.base/'coding-model-responses').glob('*.json')]
        self.assertTrue(any(r.get('stage')=='IMPLEMENTATION_DIAGNOSIS' for r in records))

    def test_diagnosis_failure_does_not_repeat_or_replace_the_frozen_model(self):
        for diagnosis_result in [RuntimeError('diagnosis unavailable'),'<tool_call>write outside</tool_call>']:
            with self.subTest(diagnosis_result=diagnosis_result):
                (self.workspace/'probe.py').write_text('answer = 1\n')
                calls=[]
                class Model:
                    def generate(inner,messages,model,**kwargs):
                        calls.append((model,kwargs.get('format')))
                        if not kwargs.get('format'):
                            if isinstance(diagnosis_result,Exception):raise diagnosis_result
                            return {'text':diagnosis_result}
                        return {'text':json.dumps({'operations':[{'op':'write','path':'probe.py','content':'answer = 2\n'}]})}
                runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,Model())
                task,response=runtime.execute('Repair failure',workspace_root=str(self.workspace),
                    managed_context={**self.managed_context,'coding_model':'role:long_implementation',
                        'implementation_plan_artifact':{'file_manifest':[{'path':'probe.py','action':'modify'}]},
                        'repair_diagnostics':{'verification_failures':[{'detail':'expected 2'}],
                            'unchanged_paths':['probe.py']}})
                self.assertEqual('completed',task.state.value,response)
                self.assertEqual(['role:long_implementation']*2,[c[0] for c in calls])
                self.assertEqual('answer = 2\n',(self.workspace/'probe.py').read_text())
                self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())

    def test_stalled_repair_uses_actual_assertion_and_explicit_repair_role_without_encoding_handoff(self):
        (self.workspace/'logic.js').write_text('const answer = 1;\nmodule.exports={answer};\n')
        (self.workspace/'logic.test.js').write_text("const {answer}=require('./logic.js');\nassert.equal(answer,2);\n")
        (self.workspace/'index.html').write_text('<p>UNRELATED_UI_SOURCE</p>')
        calls=[]
        class Model:
            def generate(inner,messages,model,**kwargs):
                calls.append((model,messages,kwargs))
                return {'text':json.dumps({'reason':'The actual assertion expects 2',
                    'edits':[{'path':'logic.js','start_line':1,'end_line':1,
                              'replacement':'const answer = 2;'}]})}
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,Model())
        task,response=runtime.execute('Repair current failure',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'coding_model':'role:long_implementation',
                'coding_diagnostic_model':'role:short_coding_fix_debug',
                'implementation_plan_artifact':{'file_manifest':[
                    {'path':p,'action':'modify'} for p in ('logic.js','logic.test.js','index.html')]},
                'repair_diagnostics':{'stalled_acceptance':True,
                    'previous_failed_repairs':[{'attempt':1,'failures':[{'test':'actual failing assertion','error':'Recorded earlier fix still failed'}]}],
                    'verification_failures':[{'name':'actual failing assertion','detail':'error: Expected 2, actual 1'}],
                    'acceptance_failures':[{'ids':['DONE:1'],'error':'LONG_WRAPPER_SUCCESS_TAP'}]}})
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(['role:long_implementation'],[c[0] for c in calls])
        diagnosis=calls[0][1][-1]['content']
        self.assertIn('actual failing assertion',diagnosis)
        self.assertIn('1: const answer = 1;',diagnosis)
        self.assertIn("2: assert.equal(answer,2);",diagnosis)
        self.assertNotIn('LONG_WRAPPER_SUCCESS_TAP',diagnosis)
        self.assertNotIn('UNRELATED_UI_SOURCE',diagnosis)
        self.assertIn('TEST_ORACLE_RULE',diagnosis)
        self.assertIn('The original user requirements define correct behavior',diagnosis)
        writer=calls[0][1][-1]['content']
        self.assertIn('TEST_ORACLE_RULE',writer)
        self.assertIn('Never delete, skip, or weaken a failing assertion',writer)
        self.assertNotIn('A finite sample need not contain each candidate',writer)
        self.assertIn('Recorded earlier fix still failed',writer)
        self.assertIn('Recorded earlier fix still failed',diagnosis)
        records=[json.loads(p.read_text()) for p in (self.base/'coding-model-responses').glob('*.json')]
        self.assertTrue(any(r.get('requested_model')=='role:long_implementation' and
                            r.get('stage')=='IMPLEMENTATION_SOURCE_REPAIR' for r in records))
        self.assertEqual('<p>UNRELATED_UI_SOURCE</p>',(self.workspace/'index.html').read_text())

    def test_greenfield_managed_contract_does_not_treat_failure_repair_as_existing_file_edit(self):
        model = OperationBatchModel([
            {"op": "write", "path": "index.html", "content": '<textarea id="dishes"></textarea>\n'},
            {"op": "write", "path": "app.js", "content": 'const dishes = document.getElementById("dishes");\n'},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        request = 'Execute the approved coding phase now.\n' + json.dumps({
            "goal": "Create the UI layer: index.html and app.js",
            "original_request": "空のプロジェクトから作成してください。実装後はテストを実行し、失敗があれば修正してください。",
        }, ensure_ascii=False)
        context = {**self.managed_context, "implementation_plan_artifact": {
            "file_manifest": [{"path": "index.html", "action": "create"}, {"path": "app.js", "action": "create"}],
        }}
        task, response = runtime.execute(request, workspace_root=str(self.workspace), managed_context=context)
        self.assertEqual("completed", task.state.value, response)
        self.assertEqual(1, len(model.calls))
        self.assertTrue((self.workspace / "index.html").is_file())
        self.assertTrue((self.workspace / "app.js").is_file())
        self.assertEqual(2, sum(op["tool"] == "workspace_write" and op["status"] == "success" for op in task.tool_executions))
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text())

    def test_new_files_are_not_all_forced_by_a_task_wide_legacy_scope(self):
        for artifact in [None,{'file_manifest':[{'path':'logic.js','action':'create'},
                                               {'path':'index.html','action':'create'}]}]:
            with self.subTest(artifact=artifact):
                (self.workspace/'logic.js').unlink(missing_ok=True)
                model=OperationBatchModel([{'op':'write','path':'logic.js','content':'module.exports={value:1};'}])
                runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
                context={**self.managed_context,
                    'approved_scopes':[{'requested_scope':['logic.js','index.html']}],
                    'implementation_contract':{'phase_id':'logic'},
                    'implementation_plan_artifact':artifact}
                task,response=runtime.execute('Create the logic module now. The UI belongs to a later phase.',
                    workspace_root=str(self.workspace),managed_context=context)
                self.assertEqual('completed',task.state.value,response)
                operations=model.calls[-1]['format']['properties']['operations']
                self.assertNotIn('prefixItems',operations)
                self.assertNotIn('minItems',operations)
                self.assertFalse((self.workspace/'index.html').exists())
                self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())

    def test_source_snapshot_records_real_reads_without_fabricating_extra_writes(self):
        from olcr_api.coding_tasks import evaluate_phase
        sources={'index.html':'<h1>Old</h1>','app.js':'const app=1;','style.css':'body {margin:0;}'}
        for path,source in sources.items():
            (self.workspace/path).write_text(source)
        model=OperationBatchModel([{'op':'write','path':'index.html','content':'<h1>New</h1>'}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Prepare the UI files.',workspace_root=str(self.workspace),managed_context={
            **self.managed_context,'implementation_plan_artifact':{'file_manifest':[
                {'path':path,'action':'modify'} for path in sources]}})
        self.assertEqual('completed',task.state.value,response)
        reads=[op for op in task.tool_executions if (op.get('output') or {}).get('source_snapshot')]
        self.assertEqual(set(sources),{Path(op['output']['path']).name for op in reads})
        self.assertEqual(1,len([op for op in task.tool_executions if op['tool']=='workspace_write']))
        self.assertEqual(sources['app.js'],(self.workspace/'app.js').read_text())
        self.assertEqual(sources['style.css'],(self.workspace/'style.css').read_text())
        phase={'id':'ui','execution_mode':'IMPLEMENTATION','done':[path+' exists' for path in sources],
               'verify':['Typed workspace writes and read-back succeed for the changed files.']}
        report={'phase_id':'ui','status':'PASS','final_worktree_mutations':1,
                'typed_execution_summary':{'operations':task.tool_executions,'state':'completed'}}
        self.assertTrue(evaluate_phase(phase,report,workspace_root=str(self.workspace))['phase_complete'])
        # Reads establish file facts, never executed application behavior.
        phase['verify']=['Click the button in the browser']
        self.assertFalse(evaluate_phase(phase,report,workspace_root=str(self.workspace))['phase_complete'])

    def test_rejected_small_source_patch_recovers_with_a_scoped_complete_write(self):
        (self.workspace/'app.js').write_text('const answer=1;')
        calls=[]
        class Model:
            def generate(self,messages,model,**kwargs):
                calls.append(kwargs)
                if not kwargs.get('format'):
                    return {'text':'The previous patch duplicated the file tail. Write the complete valid file once.'}
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'Repair rejected patch encoding','operations':[
                        {'op':'write','path':'app.js','content':'const answer=2;'}],'verification':'Host verifies'})}
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,Model())
        task,response=runtime.execute('Fix the observed source problem.',workspace_root=str(self.workspace),managed_context={
            **self.managed_context,'implementation_plan_artifact':{'file_manifest':[{'path':'app.js','action':'modify'}]},
            'repair_diagnostics':{'invalid_source_paths':['app.js','../outside.js'],
                                  'typed_error':"SyntaxError: Unexpected token '}'"}})
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(2,len(calls))
        branches=calls[-1]['format']['properties']['operations']['items']['oneOf']
        self.assertFalse(any(branch['properties']['op'].get('const')=='patch' for branch in branches))
        self.assertEqual('const answer=2;',(self.workspace/'app.js').read_text())
        self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())

    def test_managed_snapshot_includes_more_than_eight_authorized_sources(self):
        sources={f'module_{i:02}.ts':f'export const value{i} = {i};\n' for i in range(12)}
        for name, source in sources.items():(self.workspace/name).write_text(source)
        model=OperationBatchModel([{'op':'patch','path':'module_11.ts',
            'expected_old_fragment':sources['module_11.ts'],'replacement_fragment':'export const value11 = 42;\n'}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Update the last module.',workspace_root=str(self.workspace),managed_context={
            **self.managed_context,'implementation_plan_artifact':{'file_manifest':[
                {'path':name,'action':'modify'} for name in sources]}})
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('export const value11 = 11;',model.calls[-1]['messages'][-1]['content'])
        reads=[op for op in task.tool_executions if (op.get('output') or {}).get('source_snapshot')]
        self.assertEqual(12,len(reads))
        self.assertEqual('export const value11 = 42;\n',(self.workspace/'module_11.ts').read_text())

    def test_preimage_failure_uses_current_numbered_source_without_test_failure(self):
        source='export const value = 1;\nexport const keep = 2;\n'
        (self.workspace/'logic.ts').write_text(source)
        class LineRepairModel(OperationBatchModel):
            def generate(inner,messages,model,**kwargs):
                inner.calls.append({'messages':messages,'format':kwargs.get('format')})
                return {'text':json.dumps({'reason':'Apply the requested value change against live source',
                    'edits':[{'action':'replace','path':'logic.ts','start_line':1,'end_line':1,
                              'replacement':'export const value = 3;\n'}]})}
        model=LineRepairModel([])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Change value to 3.',workspace_root=str(self.workspace),managed_context={
            **self.managed_context,'implementation_plan_artifact':{'file_manifest':[{'path':'logic.ts','action':'modify'}]},
            'repair_diagnostics':{'typed_failure_class':'PREIMAGE_MISMATCH',
                'preimage_diagnostics':[{'path':'logic.ts','preimage_failure_reason':'STALE_OLD_TEXT'}]}})
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('edits',model.calls[-1]['format']['properties'])
        self.assertIn('1: export const value = 1;',model.calls[-1]['messages'][-1]['content'])
        self.assertEqual('export const value = 3;\nexport const keep = 2;\n',(self.workspace/'logic.ts').read_text())
        self.assertEqual(1,len(model.calls))

    def test_browser_exception_location_reaches_the_source_operation_prompt(self):
        source='const state=1;state=2;'
        (self.workspace/'app.js').write_text(source)
        model=OperationBatchModel([{'op':'write','path':'app.js','content':'let state=1;state=2;'}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Repair the browser exception.',workspace_root=str(self.workspace),managed_context={
            **self.managed_context,'implementation_plan_artifact':{'file_manifest':[{'path':'app.js','action':'modify'}]},
            'repair_diagnostics':{'acceptance_failures':[{'ids':['VERIFY:1'],'browser_errors':[
                {'message':'Assignment to constant variable.','source_paths':['app.js'],
                 'source_locations':[{'path':'app.js','line':1,'column':15,'source_line':source}]}]}]}})
        self.assertEqual('completed',task.state.value,response)
        prompt=model.calls[-1]['messages'][-1]['content']
        self.assertIn('OBSERVED_APPLICATION_EXCEPTION_SOURCE',prompt)
        self.assertIn('"source_line": "'+source+'"',prompt)
        self.assertEqual('let state=1;state=2;',(self.workspace/'app.js').read_text())

    def test_managed_contract_embedded_control_phrases_do_not_reroute_execution(self):
        phrases = ["Never use sudo or rm -rf; do not expose credentials or system configuration.",
                   "The docs describe how to run local script and rename file safely.",
                   "Previous user message: 実装はしないでください。 Now execute the approved phase.",
                   "write file index.html: sample documentation", "procedure lowercase: EXAMPLE"]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                model = OperationBatchModel([{"op": "write", "path": "probe.txt", "content": "bounded output\n"}])
                runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
                context = {**self.managed_context, "implementation_plan_artifact": {"file_manifest": [{"path": "probe.txt", "action": "create"}]}}
                task, response = runtime.execute(phrase, workspace_root=str(self.workspace), managed_context=context)
                self.assertEqual("completed", task.state.value, response)
                self.assertEqual("IMPLEMENTATION", task.route.value)
                self.assertEqual(1, len(model.calls))
                self.assertEqual("bounded output\n", (self.workspace / "probe.txt").read_text())

    def test_invalid_managed_execution_contract_never_calls_normal_brain(self):
        for overrides, root in [({"operation_intent": "CHAT"}, str(self.workspace)),
                                ({"global_no_write": "false"}, str(self.workspace)), ({}, None)]:
            with self.subTest(overrides=overrides, root=root):
                task, response = self.runtime.execute("Explain something", workspace_root=root,
                    managed_context={**self.managed_context, **overrides})
                self.assertEqual("failed", task.state.value, response)
                self.assertIn("MANAGED_EXECUTION_CONTRACT_INVALID", task.error)
                self.assertEqual([], self.model.calls)

    def test_code_content_continue_and_unbalanced_string_punctuation_are_not_control_events(self):
        source = 'export const label = "続行しますか? next step (}";\nfor (const x of [1,2]) { if (x === 1) continue; }\n'
        model = OperationBatchModel([{"op": "write", "path": "app.js", "content": source}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, response = runtime.execute("Execute the phase", workspace_root=str(self.workspace),
            managed_context={**self.managed_context, "implementation_plan_artifact": {"file_manifest": [{"path": "app.js", "action": "create"}]}})
        self.assertEqual("completed", task.state.value, response)
        self.assertEqual(source, (self.workspace / "app.js").read_text())

    def test_javascript_parser_rejects_invalid_syntax_without_running_source(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("Node parser unavailable")
        self.assertFalse(Runtime._structural_ok(Path("app.js"), "export const value = {;", {}))
        self.assertTrue(Runtime._structural_ok(Path("app.js"), 'throw new Error("must not execute");', {}))
        self.assertTrue(Runtime._structural_ok(Path("app.js"),
            'document.getElementById("optional")?.remove();', {Path("index.html"): '<main id="root"></main>'}))

    def test_package_json_requires_one_valid_json_object_without_duplicate_tail(self):
        for invalid in ('{"name":"app"}\n"license":"MIT"\n}', '{"name":}', '[]', '{"value":NaN}'):
            with self.subTest(invalid=invalid):
                diagnostics=[]
                self.assertFalse(Runtime._structural_ok(Path('package.json'),invalid,{},diagnostics))
                self.assertTrue(diagnostics)
                self.assertIn('package.json',diagnostics[0])
        self.assertTrue(Runtime._structural_ok(Path('package.json'),'{"name":"app","type":"module"}',{}))
        self.assertTrue(Runtime._structural_ok(Path('tsconfig.json'),'{/* JSONC is valid for this config */}',{}))

    def test_package_suffix_normalization_preserves_data_and_dependency_classification(self):
        from olcr_api.runtime import _normalize_package_json_suffix,_package_json_changed_fields
        valid='  {"name":"app","dependencies":{"x":"1"}}'
        normalized,changed=_normalize_package_json_suffix(valid+'}\n}\n')
        self.assertTrue(changed)
        self.assertEqual(json.loads(valid),json.loads(normalized))
        self.assertEqual(valid+'\n',normalized)
        self.assertEqual(set(),_package_json_changed_fields(valid+'}}',valid))
        self.assertIn('DEPENDENCY_DECLARATION',_package_json_changed_fields('{}',valid+'}'))
        self.assertIn('DEPENDENCY_DECLARATION',_package_json_changed_fields('invalid old JSON','{}'))
        for source in (valid,valid+'\n"license":"MIT"}',valid+';process.exit()',valid+'{}','[1,2]]'):
            self.assertEqual((source,False),_normalize_package_json_suffix(source))

    def test_identical_invalid_package_write_becomes_an_audited_lossless_syntax_repair(self):
        raw='{"name":"app","scripts":{"test":"node --test"}}}\n'
        package=self.workspace/'package.json';package.write_text(raw)
        model=OperationBatchModel([{'op':'write','path':'package.json','content':raw}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Repair package.json syntax',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'implementation_plan_artifact':{
                'file_manifest':[{'path':'package.json','action':'modify'}]}})
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual({'name':'app','scripts':{'test':'node --test'}},json.loads(package.read_text()))
        writes=[operation for operation in task.tool_executions if operation.get('tool')=='workspace_write']
        self.assertEqual(1,len(writes))
        output=writes[0]['output']
        self.assertTrue(output['net_changed'])
        self.assertEqual('PACKAGE_JSON_REDUNDANT_CLOSERS_REMOVED',output['content_normalization'])
        self.assertTrue(output['preserved_json_object'])
        self.assertNotEqual(output['requested_content_sha256'],output['post_apply_hash'])

    def test_package_suffix_normalization_does_not_bypass_package_install_ownership(self):
        package=self.workspace/'package.json';package.write_text('{}')
        model=OperationBatchModel([{'op':'write','path':'package.json','content':'{"dependencies":{"unauthorized":"1"}}}'}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Repair only configuration syntax',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'authorized_package_requirements':['react'],
                'package_install_required':True,'package_install_satisfied':True,'allow_package_json_file_mutation':True,
                'package_json_dependency_owner':'PACKAGE_INSTALL','implementation_plan_artifact':{
                    'file_manifest':[{'path':'package.json','action':'modify'}]}})
        self.assertEqual('failed',task.state.value,response)
        rejection=next(operation for operation in task.tool_executions if operation.get('tool')=='operation_rejection')
        self.assertEqual('PACKAGE_JSON_OWNERSHIP_VIOLATION',rejection['input']['failure_class'])
        self.assertIn('DEPENDENCY_DECLARATION',str(rejection['output']))
        self.assertEqual('{}',package.read_text())

    def test_invalid_package_patch_rolls_back_the_entire_source_batch(self):
        package=self.workspace/'package.json';package.write_text('{"name":"original"}')
        (self.workspace/'probe.py').write_text('answer = 1\n')
        operations=[{'op':'write','path':'fresh.js','content':'const fresh=true;'},
                    {'op':'patch','path':'probe.py','expected_old_fragment':'answer = 1','replacement_fragment':'answer = 2'},
                    {'op':'write','path':'package.json','content':'{"name":"after"}\n"license":"MIT"\n}'}]
        model=OperationBatchModel(operations)
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,response=runtime.execute('Apply the approved source changes',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'implementation_plan_artifact':{'file_manifest':[
                {'path':'fresh.js','action':'create'},{'path':'probe.py','action':'modify'},
                {'path':'package.json','action':'modify'}]}})
        self.assertEqual('failed',task.state.value,response)
        self.assertIn('structural validation failed',response)
        self.assertEqual('{"name":"original"}',package.read_text())
        self.assertEqual('answer = 1\n',(self.workspace/'probe.py').read_text())
        self.assertFalse((self.workspace/'fresh.js').exists())
        self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.sentinel.read_text())

    def test_authorized_source_is_visible_beyond_inventory_limit_and_external_symlink_is_not(self):
        for i in range(201):
            (self.workspace / f"a{i:03}.txt").write_text("filler\n")
        source = self.workspace / "z-last.js"
        source.write_text("export const answer = 1;\n")
        (self.workspace / "outside.js").symlink_to(self.sentinel)
        model = OperationBatchModel([{"op": "patch", "path": "z-last.js", "old_text": "answer = 1", "new_text": "answer = 2"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, response = runtime.execute("Execute approved patch", workspace_root=str(self.workspace),
            managed_context={**self.managed_context, "implementation_plan_artifact": {"file_manifest": [{"path": "z-last.js", "action": "modify"}]}})
        self.assertEqual("completed", task.state.value, response)
        self.assertIn("export const answer = 1", str(model.calls[0]["messages"]))
        self.assertNotIn("OUTSIDE_SENTINEL_ORIGINAL", str(model.calls[0]["messages"]))
        self.assertEqual("export const answer = 2;\n", source.read_text())

    def test_direct_missing_file_edit_reports_typed_failure_without_calling_model(self):
        task, response = self.runtime.execute("index.html を修正してください", workspace_root=str(self.workspace))
        self.assertEqual("failed", task.state.value, response)
        self.assertIn("TARGET_NOT_FOUND", task.error)
        self.assertEqual([], self.model.calls)
        rejection = next(op for op in task.tool_executions if op["tool"] == "operation_rejection")
        self.assertEqual("TARGET_NOT_FOUND", rejection["output"]["failure_class"])

    def test_managed_greenfield_write_still_enforces_exact_manifest(self):
        model = OperationBatchModel([{"op": "write", "path": "not-approved.js", "content": "hello\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        context = {**self.managed_context, "implementation_plan_artifact": {"file_manifest": [{"path": "index.html", "action": "create"}]}}
        task, response = runtime.execute("Create index.html; 失敗があれば修正してください", workspace_root=str(self.workspace), managed_context=context)
        self.assertEqual(1, len(model.calls))
        self.assertEqual("failed", task.state.value, response)
        self.assertFalse((self.workspace / "not-approved.js").exists())

    def test_unmanaged_coding_with_lookup_words_cannot_finish_as_search(self):
        class AnswerModel:
            def generate(self,messages,model,**kwargs):
                return {'text':'Implementation plan for the requested app.','latency_ms':0}
        settings=self.runtime.settings.with_overrides({'task_manager_enabled':False,'web_mode':'auto','web_provider':'brave'})
        runtime=Runtime(settings,self.db,self.runtime.retrieval,AnswerModel())
        for request in ['最新ドキュメントを検索してから、検索APIを実装してください。',
                        'Build a weekly meal planner with a searchable recipe list.']:
            with self.subTest(request=request), \
                 patch('olcr_api.runtime.brave_search',side_effect=AssertionError('search cannot replace implementation')) as search, \
                 patch('olcr_api.runtime.web_search',side_effect=AssertionError('search fallback stole implementation')):
                task,response=runtime.execute(request,workspace_root=str(self.workspace))
            self.assertEqual('completed',task.state.value,response)
            self.assertEqual('Implementation plan for the requested app.',response)
            search.assert_not_called()

    def test_skill_guided_search_generates_answer_instead_of_exposing_retrieval_json(self):
        class AnswerModel:
            def __init__(self):
                self.calls = []
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append(messages)
                return {"text": "Use the apple-design skill for GUI design.", "latency_ms": 0}

        model = AnswerModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, response = runtime.execute(
            "Find an installed skill that helps with GUI design. Do not install anything or modify files.",
            core_context="[ACTIVE_SKILL name=find-skills]\nFind the best matching installed skill.\n[/ACTIVE_SKILL]",
        )
        self.assertEqual("completed", task.state.value)
        self.assertNotEqual("IMPLEMENTATION", task.route.value)
        self.assertEqual([], task.tool_executions)
        self.assertEqual("Use the apple-design skill for GUI design.", response)
        self.assertEqual(1, len(model.calls))
        self.assertIn("[ACTIVE_SKILL name=find-skills]", "\n".join(
            str(message.get("content", "")) for message in model.calls[0]
        ))

    def test_real_runtime_managed_write_readback_and_global_no_write(self):
        request = (
            "新しいTask用の作業領域に必要なファイルを作成してください。\n"
            "このTask以外の既存ファイルは変更しないでください。"
        )
        task, response = self.runtime.execute(
            request,
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )

        probe = self.workspace / "runtime_probe.txt"
        write_operations = [item for item in task.tool_executions if item["tool"] == "workspace_write"]
        successful_operations = [item for item in write_operations if item["status"] == "success"]
        readbacks = [item for item in task.tool_executions if item["tool"] == "workspace_read" and item["status"] == "success"]

        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertEqual("completed", task.state.value)
        self.assertTrue(self.model.calls)
        self.assertFalse(self.model.calls[0]["think"])
        self.assertIsInstance(self.model.calls[0]["format"], dict)
        schema=self.model.calls[0]['format']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(160,schema['properties']['reason_code']['maxLength'])
        self.assertEqual(600,schema['properties']['verification']['maxLength'])
        self.assertIn("Return ONLY JSON", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn('"operations"', self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("The operations array is ONLY for authorized workspace mutations", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("Never put op=run, command, execute, shell, test", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("Emit at most one op=write per path in a batch", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("For a target absent from Workspace files and SOURCE, use one complete op=write, never op=patch", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("A new-file write MUST have exactly the write fields including `content`", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("A failed operation batch is rolled back completely", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn("orchestrator runs the phase's separately approved verification_command", self.model.calls[0]["messages"][-1]["content"])
        self.assertEqual("OLCR_REAL_RUNTIME_PROBE", probe.read_text(encoding="utf-8"))
        self.assertIn("Write: PASS; read-back: PASS", response)
        self.assertGreater(len(write_operations), 0)
        self.assertGreater(len(successful_operations), 0)
        self.assertGreater(len(readbacks), 0)
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text(encoding="utf-8"))

        global_no_write_request = (
            "ワークスペースのファイルには何も書き込まないでください。\n"
            "ファイルを作成・変更しないでください。\n"
            "実装はしないでください。"
        )
        denied, _ = self.runtime.execute(
            global_no_write_request,
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context, "global_no_write": True},
        )

        self.assertEqual("denied", denied.state.value)
        self.assertEqual("explicit_no_write_request", denied.reason_category)
        self.assertFalse((self.workspace / "forbidden_probe.txt").exists())
        self.assertEqual(1, len(self.model.calls))

    def test_checkpoint_phase_persists_exact_unified_diff_in_typed_write_evidence(self):
        target=self.workspace/"src"/"probe.py"
        target.parent.mkdir()
        target.write_text("def value():\n    return 1\n",encoding="utf-8")
        model=OperationBatchModel([{"op":"patch","path":"src/probe.py",
                                    "expected_old_fragment":"return 1","replacement_fragment":"return 2"}])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,_=runtime.execute("Update src/probe.py.",workspace_root=str(self.workspace),
            managed_context={**self.managed_context,"capture_failure_diff":True,
                             "approved_scopes":[{"source":"original_request","requested_scope":["src/probe.py"]}]})
        self.assertEqual("completed",task.state.value)
        write=next(item for item in task.tool_executions if item.get("tool")=="workspace_write" and item.get("status")=="success")
        self.assertEqual(
            "--- a/src/probe.py\n+++ b/src/probe.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
            write["output"]["unified_diff"],
        )
        self.assertEqual("def value():\n    return 2\n",target.read_text(encoding="utf-8"))

    def test_package_install_uses_typed_executor_and_structured_evidence(self):
        model = PackageInstallModel(self.workspace)
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=FakePackageExecutor())
        task, response = runtime.execute(
            "Install the authorized frontend dependency.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "allow_explicit_package_manager_default": True},
        )
        self.assertEqual("completed", task.state.value)
        self.assertIn("structured evidence", response)
        evidence = next(item for item in task.tool_executions if item["tool"] == "package_install")
        self.assertEqual("success", evidence["status"])
        self.assertEqual("PACKAGE_INSTALL", evidence["output"]["operation_type"])
        self.assertEqual(0, evidence["output"]["exit_code"])

    def test_dependency_required_phase_dispatches_package_install_before_file_operations(self):
        model = OperationBatchModel([{"op": "write", "path": "runtime_probe.txt", "content": "after install"}])
        executor = FakePackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        task, _ = runtime.execute(
            "Initialize the authorized frontend project and implement the remaining files.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "package_install_required": True,
                             "package_install_satisfied": False,
                             "allow_explicit_package_manager_default": True},
        )
        tools = [item["tool"] for item in task.tool_executions]
        self.assertEqual("completed", task.state.value)
        self.assertEqual(1, len(executor.calls))
        self.assertEqual(["react"], executor.calls[0]["packages"])
        self.assertLess(tools.index("package_install"), tools.index("workspace_write"))
        self.assertIn("PACKAGE_INSTALL_DISPATCHED=YES", model.calls[0]["messages"][-1]["content"])

    def test_dependency_kinds_are_dispatched_as_separate_typed_package_operations(self):
        model = OperationBatchModel([{"op": "write", "path": "runtime_probe.txt", "content": "after install"}])
        executor = FakePackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        task, _ = runtime.execute(
            "Initialize the authorized frontend project.", workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react", "vite"],
                             "package_install_operations": [
                                 {"operation_type": "PACKAGE_INSTALL", "dependency_kind": "dependencies", "packages": ["react"]},
                                 {"operation_type": "PACKAGE_INSTALL", "dependency_kind": "devDependencies", "packages": ["vite"]},
                             ], "package_install_required": True,
                             "allow_explicit_package_manager_default": True},
        )
        self.assertEqual("completed", task.state.value)
        self.assertEqual(["dependencies", "devDependencies"], [item["dependency_kind"] for item in executor.calls])
        self.assertEqual(2, len([item for item in task.tool_executions if item["tool"] == "package_install"]))

    def test_successful_package_install_is_not_repeated_on_file_retry(self):
        model = OperationBatchModel([{"op": "write", "path": "runtime_probe.txt", "content": "retry"}])
        executor = FakePackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        task, _ = runtime.execute(
            "Continue the authorized frontend implementation after dependency installation.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "package_install_required": True,
                             "package_install_satisfied": True,
                             "allow_explicit_package_manager_default": True},
        )
        self.assertEqual("completed", task.state.value)
        self.assertEqual([], executor.calls)
        self.assertNotIn("package_install", [item["tool"] for item in task.tool_executions])

    def test_dependency_install_failure_stops_before_file_implementer(self):
        model = DeterministicFileModel()
        executor = FailingPackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        task, response = runtime.execute(
            "Initialize the authorized frontend project.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "package_install_required": True,
                             "allow_explicit_package_manager_default": True},
        )
        self.assertEqual("failed", task.state.value)
        self.assertIn("PACKAGE_MANAGER_NONZERO_EXIT", response)
        self.assertFalse(model.calls)
        self.assertEqual("failed", next(item for item in task.tool_executions if item["tool"] == "package_install")["status"])

    def test_package_json_dependency_edit_is_owned_by_package_executor(self):
        model = PackageJsonPatchModel()
        executor = FakePackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        package_json = self.workspace / "package.json"
        package_json.write_text("{}\n", encoding="utf-8")
        task, response = runtime.execute(
            "Initialize the authorized frontend project.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "package_install_required": True,
                             "allow_explicit_package_manager_default": True},
        )
        self.assertEqual("failed", task.state.value)
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertIn("PACKAGE_INSTALL", rejection["output"]["failure"])
        self.assertEqual("PACKAGE_JSON_OWNERSHIP_VIOLATION", rejection["input"]["failure_class"])
        self.assertEqual("PACKAGE_INSTALL", rejection["input"]["owner"])
        self.assertTrue(rejection["input"]["failure_fingerprint"])
        self.assertIn("PACKAGE_JSON_DEPENDENCY_OWNER=PACKAGE_INSTALL", model.calls[0]["messages"][-1]["content"])
        self.assertIn("PACKAGE_JSON_DEPENDENCY_MUTATION_ALLOWED=NO", model.calls[0]["messages"][-1]["content"])
        path_schema = model.calls[0]["format"]["properties"]["operations"]["items"]["oneOf"][1]["properties"]["path"]
        self.assertEqual({"const": "package.json"}, path_schema.get("not"))
        self.assertEqual("{}\n", package_json.read_text(encoding="utf-8"))
        self.assertIn("PACKAGE.JSON", response.upper())

    def test_package_json_non_dependency_edit_requires_explicit_config_contract(self):
        model = PackageJsonScriptPatchModel()
        executor = FakePackageExecutor()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model,
                          package_manager_executor=executor)
        package_json = self.workspace / "package.json"
        package_json.write_text("{}", encoding="utf-8")
        task, _ = runtime.execute(
            "Configure the authorized frontend project build script.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "authorized_package_requirements": ["react"],
                             "package_install_required": True,
                             "package_install_satisfied": True,
                             "allow_package_json_file_mutation": True,
                             "package_json_dependency_owner": "PACKAGE_INSTALL"},
        )
        self.assertEqual("completed", task.state.value)
        self.assertEqual('{"scripts":{"build":"vite build"}}', package_json.read_text(encoding="utf-8"))

    def test_approved_scope_is_enforced_before_any_operation_is_written(self):
        model = OutOfScopeFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        tests = self.workspace / "tests" / "test_math.py"
        tests.parent.mkdir()
        tests.write_text("original\n", encoding="utf-8")

        task, response = runtime.execute(
            "Fix src/math.py and verify the focused test.",
            workspace_root=str(self.workspace),
            managed_context={
                **self.managed_context,
                "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
            },
        )

        self.assertEqual("failed", task.state.value)
        self.assertIn("authorized mutation scope", response)
        self.assertEqual("def add(a, b):\n    return a - b\n", source.read_text(encoding="utf-8"))
        self.assertEqual("original\n", tests.read_text(encoding="utf-8"))
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertEqual("rejected", rejection["status"])
        self.assertEqual("tests/test_math.py", rejection["input"]["path"])
        self.assertEqual("OPERATION_SCOPE_UNAUTHORIZED", rejection["input"]["failure_class"])
        self.assertEqual("PLAN_MUTABLE_MANIFEST_SCOPE", rejection["output"]["authorization_scope"])
        self.assertIn("outside authorized mutation scope", rejection["output"]["rejection_reason"])
        self.assertFalse(rejection["output"]["authorized"])
        self.assertTrue(rejection["output"]["pathguard_allowed"])
        self.assertEqual("UNCHANGED", rejection["output"]["worktree_state"])

    def test_outside_root_operation_is_rejected_by_pathguard_before_write(self):
        model = OutsideRootFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute(
            "Fix the selected project.",
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )
        self.assertEqual("failed", task.state.value)
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertFalse(rejection["output"]["pathguard_allowed"])
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text(encoding="utf-8"))

    def test_python_source_is_supplied_to_typed_implementer(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        task, _ = self.runtime.execute(
            "Fix src/math.py.",
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )
        self.assertEqual("completed", task.state.value)
        prompt = self.model.calls[-1]["messages"][-1]["content"]
        self.assertIn("def add(a, b):", prompt)
        self.assertIn("Never emit a patch with an empty expected_old_fragment", prompt)
        self.assertIn("full updated file reconstructed from SOURCE", prompt)

    def test_managed_implementer_context_excludes_unapproved_neighbor_files(self):
        allowed = self.workspace / "ledger_core.py"
        distractor = self.workspace / "E2E_SHORT_FIX_PROBE.py"
        allowed.write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
        distractor.write_text("DISTRACTOR_CONTENT_MUST_NOT_REACH_IMPLEMENTER\n", encoding="utf-8")
        model = DeterministicFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)

        task, _ = runtime.execute(
            "Fix ledger_core.py.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "approved_scopes": [{"source": "plan_manifest",
                                                  "requested_scope": ["ledger_core.py"]}]},
        )

        self.assertEqual("failed", task.state.value)
        prompt = model.calls[-1]["messages"][-1]["content"]
        self.assertIn("Workspace files: ['ledger_core.py']", prompt)
        self.assertIn("def total(items):", prompt)
        self.assertNotIn("E2E_SHORT_FIX_PROBE.py", prompt)
        self.assertNotIn("DISTRACTOR_CONTENT_MUST_NOT_REACH_IMPLEMENTER", prompt)
        self.assertIn("E2E_SHORT_FIX_PROBE.py", task.tool_executions[0]["output"]["files"])

    def test_exact_patch_applies_to_current_source(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        model = PatchFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Fix src/math.py.", workspace_root=str(self.workspace), managed_context={
            **self.managed_context,
            "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
        })
        self.assertEqual("completed", task.state.value)
        self.assertIn("return a + b", source.read_text(encoding="utf-8"))
        self.assertTrue(any(item["tool"] == "operation_preflight" for item in task.tool_executions))

    def test_invalid_preimage_is_deterministic_and_rolls_back(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        original = "def add(a, b):\n    return a - b\n"
        source.write_text(original, encoding="utf-8")
        model = PatchFileModel(old="return never_existed")
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Fix src/math.py.", workspace_root=str(self.workspace), managed_context={
            **self.managed_context,
            "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
        })
        self.assertEqual("failed", task.state.value)
        self.assertEqual(original, source.read_text(encoding="utf-8"))
        failure = next(item for item in task.tool_executions if item["tool"] == "operation_application_failure")
        self.assertEqual("PREIMAGE_MISMATCH", failure["input"]["failure_class"])
        self.assertEqual("ROLLED_BACK", failure["output"]["worktree_state"])
        diagnostic = next(item for item in task.tool_executions if item["tool"] == "preimage_diagnostic")
        self.assertEqual("STALE_OLD_TEXT", diagnostic["output"]["preimage_failure_reason"])
        self.assertEqual(diagnostic["output"]["actual_file_content_hash"], diagnostic["output"]["executor_pre_apply_hash"])
        self.assertEqual(0, diagnostic["output"]["patch_old_match_count"])

    def test_patch_to_absent_target_with_old_fragment_is_rejected_before_writes(self):
        target = self.workspace / "src" / "new_math.py"
        model = OperationBatchModel([{"op": "patch", "path": "src/new_math.py",
                                      "expected_old_fragment": "old code that is not present",
                                      "replacement_fragment": "def add(a, b): return a + b\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Implement src/new_math.py.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                   "approved_scopes": [{"source": "original_request",
                                                                        "requested_scope": ["src/new_math.py"]}]})
        self.assertEqual("failed", task.state.value)
        self.assertFalse(target.exists())
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertIn("patch target does not exist; use op=write", rejection["output"]["failure"])
        self.assertEqual("UNCHANGED", rejection["output"]["worktree_state"])

    def test_same_attempt_operations_rebase_patch_against_prior_write(self):
        model = OperationBatchModel([
            {"op": "write", "path": "src/math.py", "content": "def add(a, b):\n    return a - b\n"},
            {"op": "patch", "path": "src/math.py", "expected_old_fragment": "return a - b", "replacement_fragment": "return a + b"},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Implement src/math.py.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                   "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}]})
        self.assertEqual("completed", task.state.value)
        self.assertIn("return a + b", (self.workspace / "src/math.py").read_text(encoding="utf-8"))

    def test_duplicate_write_is_rejected_before_any_batch_mutation(self):
        target = self.workspace / "src" / "ledger.py"
        target.parent.mkdir()
        content = "TOTAL = 1\n"
        model = OperationBatchModel([
            {"op": "write", "path": "src/ledger.py", "content": content},
            {"op": "write", "path": "src/ledger.py", "content": "TOTAL = 2\n"},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Create src/ledger.py.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                   "approved_scopes": [{"source": "original_request",
                                                                        "requested_scope": ["src/ledger.py"]}]})
        self.assertEqual("failed", task.state.value)
        self.assertFalse(target.exists())
        self.assertFalse(any(item["tool"] == "workspace_write" and item["status"] == "success"
                             for item in task.tool_executions))
        failure = next(item for item in task.tool_executions
                       if item["tool"] == "operation_rejection")
        self.assertIn("duplicate write operation", failure["output"]["failure"])
        self.assertFalse(failure["output"]["authorized"])
        self.assertEqual("UNCHANGED", failure["output"]["worktree_state"])

    def test_identical_duplicate_write_is_normalized_to_one_authorized_write(self):
        target=self.workspace/'src'/'same.py';target.parent.mkdir()
        operation={'op':'write','path':'src/same.py','content':'TOTAL = 1\n'}
        model=OperationBatchModel([operation,dict(operation)])
        runtime=Runtime(self.runtime.settings,self.db,self.runtime.retrieval,model)
        task,_=runtime.execute('Create src/same.py.',workspace_root=str(self.workspace),
            managed_context={**self.managed_context,'approved_scopes':[{'source':'original_request','requested_scope':['src/same.py']}]})
        self.assertEqual('completed',task.state.value)
        self.assertEqual('TOTAL = 1\n',target.read_text())
        self.assertEqual(1,len([op for op in task.tool_executions if op['tool']=='workspace_write']))
        array=model.calls[0]['format']['properties']['operations']
        self.assertEqual(1,array['maxItems'])
        self.assertEqual('src/same.py',array['prefixItems'][0]['properties']['path']['const'])

    def test_manifest_authorizes_exact_file_not_sibling_or_descendant(self):
        model = OperationBatchModel([{"op": "write", "path": "src/math.py/child.py", "content": "unsafe\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute(
            "Update src/math.py.", workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "implementation_plan_artifact": {
                                 "target_root": str(self.workspace.resolve()),
                                 "file_manifest": [{"path": "src/math.py", "action": "modify"}],
                             }},
        )
        self.assertEqual("failed", task.state.value)
        self.assertFalse((self.workspace / "src/math.py/child.py").exists())
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertFalse(rejection["output"]["authorized"])

    def test_manifest_authorized_symlink_cannot_redirect_write_outside_workspace(self):
        link = self.workspace / "src" / "math.py"
        link.parent.mkdir()
        link.symlink_to(self.sentinel)
        model = OperationBatchModel([{"op": "write", "path": "src/math.py", "content": "unsafe\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)

        task, _ = runtime.execute(
            "Update src/math.py.", workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "implementation_plan_artifact": {
                                 "target_root": str(self.workspace.resolve()),
                                 "file_manifest": [{"path": "src/math.py", "action": "modify"}],
                             }},
        )

        self.assertEqual("failed", task.state.value)
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text(encoding="utf-8"))
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertFalse(rejection["output"]["authorized"])
        self.assertFalse(rejection["output"]["pathguard_allowed"])
        self.assertEqual("UNCHANGED", rejection["output"]["worktree_state"])

    def test_scaffold_created_file_accepts_complete_patch_as_normalized_write(self):
        from olcr_api.implementation_plan import prepare_implementation_plan, workspace_state

        state = workspace_state(self.workspace)
        state["new_project_intent"] = True
        prepared, artifact = prepare_implementation_plan(
            "scaffold-patch", {"phases": [{"id": "p1"}],
                              "file_manifest": [{"path": "src/index.css", "action": "create"}]},
            self.workspace, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
        model = OperationBatchModel([{"op": "patch", "path": "src/index.css",
                                      "expected_old_fragment": "", "replacement_fragment": "body { color: red; }\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Implement the stylesheet.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                    "implementation_plan_artifact": artifact})
        self.assertEqual("completed", task.state.value)
        self.assertEqual("body { color: red; }\n",
                         (self.workspace / "src/index.css").read_text(encoding="utf-8"))
        self.assertTrue(any(item["tool"] == "workspace_write_normalized" for item in task.tool_executions))

    def test_scaffold_created_file_accepts_short_complete_write(self):
        from olcr_api.implementation_plan import prepare_implementation_plan, workspace_state

        state = workspace_state(self.workspace)
        state["new_project_intent"] = True
        prepared, artifact = prepare_implementation_plan(
            "scaffold-short-write", {"phases": [{"id": "p1"}],
                                     "file_manifest": [{"path": "result.txt", "action": "create"}]},
            self.workspace, {"task_profile": "GENERAL_CODING"}, state)
        model = OperationBatchModel([{"op": "write", "path": "result.txt",
                                      "content": "OLCR LONG FIX SINGLE"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Implement result.txt.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                   "implementation_plan_artifact": artifact})
        self.assertEqual("completed", task.state.value)
        self.assertEqual("OLCR LONG FIX SINGLE", (self.workspace / "result.txt").read_text(encoding="utf-8"))

    def test_manifest_operation_must_use_the_exact_canonical_file_path(self):
        from olcr_api.implementation_plan import prepare_implementation_plan

        plan = {
            "file_manifest": [{"path": "Create E2E_SENTINEL.md in the repository root", "action": "create"}],
            "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                         "goal": "Create E2E_SENTINEL.md in the repository root",
                         "done": ["file exists"], "verify": ["content matches"],
                         "dependencies": [], "risks": []}],
        }
        prepared, artifact = prepare_implementation_plan(
            "operation-path-canonicalization", plan, self.workspace,
            {"task_profile": "GENERAL_CODING"})
        self.assertEqual("E2E_SENTINEL.md", prepared["file_manifest"][0]["path"])
        model = OperationBatchModel([{"op": "patch",
                                      "path": "E2E_SENTINEL.md",
                                      "expected_old_fragment": "",
                                      "replacement_fragment": "OLCR E2E\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute(
            "Implement the exact-file request.", workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "implementation_plan_artifact": artifact})
        self.assertEqual("completed", task.state.value)
        self.assertEqual("OLCR E2E\n",
                         (self.workspace / "E2E_SENTINEL.md").read_text(encoding="utf-8"))
        self.assertTrue(any(item["tool"] == "workspace_write" for item in task.tool_executions))

    def test_absolute_descriptive_scope_path_is_not_reinterpreted_as_a_file(self):
        target = self.workspace / "absolute-scope.txt"

        class AbsoluteScopeModel(DeterministicFileModel):
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
                return {"text": json.dumps({"operations": [{
                    "op": "write",
                    "path": f"Create {target} in the repository root",
                    "content": "ABSOLUTE_SCOPE_OK\n",
                }]}), "latency_ms": 0}

        model = AbsoluteScopeModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute(
            f"Create {target} in the repository root and verify its content.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "approved_scopes": [{"source": "original_request",
                                                  "requested_scope": [f"Create {target} in the repository root"]}]},
        )
        self.assertEqual("failed", task.state.value)
        self.assertFalse(target.exists())
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertFalse(rejection["output"]["authorized"])

    def test_direct_greenfield_contract_authorizes_creation_without_plan_manifest(self):
        calls = []
        class ScopeAwareModel:
            def generate(inner, messages, model, **kwargs):
                prompt = '\n'.join(m['content'] for m in messages)
                calls.append((prompt, kwargs.get('format')))
                operations = [] if 'Authorized mutation paths: []' in prompt else [
                    {'op': 'write', 'path': 'index.html', 'content': '<!doctype html><title>Planner</title>'}]
                return {'text': json.dumps({'change_required': True, 'source_inspected': True,
                    'condition_evaluated': True, 'reason_code': 'new_app', 'operations': operations})}
        self.runtime.model = ScopeAwareModel()
        task, response = self.runtime.execute('献立アプリを一から作ってください。',
            workspace_root=str(self.workspace), managed_context={
                'direct_coding_request': True, 'operation_intent': 'IMPLEMENTATION',
                'coding_model': 'role:long_implementation'})
        self.assertEqual('completed', task.state.value, response)
        self.assertTrue((self.workspace / 'index.html').is_file())
        self.assertIn('DIRECT_WORKSPACE_IMPLEMENTATION', calls[0][0])
        self.assertNotIn('The orchestrator runs', calls[0][0])
        self.assertEqual(1, calls[0][1]['properties']['operations']['minItems'])

    def test_direct_empty_operations_get_one_correction_before_any_write(self):
        calls = []
        class CorrectableModel:
            def generate(inner, messages, model, **kwargs):
                calls.append(model)
                operations = [] if len(calls) == 1 else [
                    {'op': 'write', 'path': 'index.html', 'content': '<!doctype html><title>Planner</title>'}]
                self.assertFalse((self.workspace / 'index.html').exists())
                return {'text': json.dumps({'change_required': True, 'source_inspected': True,
                    'condition_evaluated': True, 'reason_code': 'new_app', 'operations': operations})}
        self.runtime.model = CorrectableModel()
        task, response = self.runtime.execute('献立アプリを一から作ってください。',
            workspace_root=str(self.workspace), managed_context={
                'direct_coding_request': True, 'operation_intent': 'IMPLEMENTATION',
                'coding_model': 'role:long_implementation'})
        self.assertEqual('completed', task.state.value, response)
        self.assertEqual(['role:long_implementation'] * 2, calls)
        self.assertTrue((self.workspace / 'index.html').is_file())

    def test_direct_empty_correction_is_bounded_and_cannot_escape_workspace(self):
        for corrected in ([], [{'op': 'write', 'path': '../sentinel-b.txt', 'content': 'ESCAPED'}]):
            with self.subTest(corrected=corrected):
                calls = []
                class InvalidModel:
                    def generate(inner, messages, model, **kwargs):
                        calls.append(model)
                        return {'text': json.dumps({'change_required': True, 'source_inspected': True,
                            'condition_evaluated': True, 'reason_code': 'new_app',
                            'operations': [] if len(calls) == 1 else corrected})}
                self.runtime.model = InvalidModel()
                task, _ = self.runtime.execute('献立アプリを一から作ってください。',
                    workspace_root=str(self.workspace), managed_context={
                        'direct_coding_request': True, 'operation_intent': 'IMPLEMENTATION',
                        'coding_model': 'role:long_implementation'})
                self.assertEqual('failed', task.state.value)
                self.assertEqual(2, len(calls))
                self.assertEqual([], list(self.workspace.iterdir()))
                self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL', self.sentinel.read_text())

    def test_mutation_required_empty_operations_are_typed_failure(self):
        class EmptyModel(DeterministicFileModel):
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append({"messages": messages, "model": model})
                return {"text": json.dumps({"change_required": True, "source_inspected": True,
                                             "condition_evaluated": True, "reason_code": "empty",
                                             "operations": []}), "latency_ms": 0}
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, EmptyModel())
        task, _ = runtime.execute("Implement the requested change.", workspace_root=str(self.workspace),
                                  managed_context=self.managed_context)
        self.assertEqual("failed", task.state.value)
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertEqual("EMPTY_IMPLEMENTATION_OPERATIONS", rejection["input"]["failure_class"])
        self.assertEqual("UNCHANGED", rejection["output"]["worktree_state"])

    def test_explicit_already_satisfied_result_records_hash_bound_noop_candidate(self):
        target = self.workspace / "src" / "math.py"
        target.parent.mkdir()
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        original = target.read_bytes()

        class AlreadySatisfiedModel:
            def __init__(self):
                self.calls = []

            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append({"messages": messages, "format": format})
                return {"text": json.dumps({
                    "change_required": False, "source_inspected": True,
                    "condition_evaluated": True, "reason_code": "already_satisfied",
                    "operations": [],
                }), "latency_ms": 0}

        model = AlreadySatisfiedModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, response = runtime.execute(
            "Keep src/math.py unchanged because it already implements addition.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "approved_scopes": [{"requested_scope": ["src/math.py"]}]},
        )
        self.assertEqual("completed", task.state.value)
        self.assertIn("must run the exact approved verifier", response)
        candidate = next(item for item in task.tool_executions
                         if item["tool"] == "implementation_noop_candidate")
        self.assertEqual("UNCHANGED", candidate["output"]["worktree_state"])
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         candidate["output"]["target_hashes"]["src/math.py"])
        self.assertEqual(original, target.read_bytes())
        implementer_prompt = model.calls[0]["messages"][-1]["content"]
        self.assertIn("if and only if", implementer_prompt.lower())
        self.assertIn("exact approved verifier", implementer_prompt)
        self.assertNotIn("you must provide one or more valid file mutation operations", implementer_prompt.lower())
        schema = model.calls[0]["format"]
        self.assertEqual({"change_required", "source_inspected", "condition_evaluated",
                          "reason_code", "operations"}, set(schema["required"]))

    def test_mixed_write_and_run_operations_are_rejected_atomically(self):
        model = OperationBatchModel([
            {"op": "write", "path": "unsafe_mix.py", "content": "value = 1\\n"},
            {"op": "run", "path": "unsafe_mix.py", "command": "python3 -m unittest unsafe_mix.py"},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute(
            "Create unsafe_mix.py and test it.",
            workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                             "approved_scopes": [{"requested_scope": ["unsafe_mix.py"]}]},
        )

        self.assertEqual("failed", task.state.value)
        self.assertFalse((self.workspace / "unsafe_mix.py").exists())
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertEqual("OPERATION_SCHEMA_SEMANTIC_ERROR", rejection["input"]["failure_class"])
        self.assertEqual("run", rejection["input"]["op"])
        self.assertEqual("UNCHANGED", rejection["output"]["worktree_state"])
        self.assertFalse(any(item["tool"] == "workspace_write" for item in task.tool_executions))

    def test_cross_attempt_uses_refreshed_worktree_preimage(self):
        class CrossAttemptModel(DeterministicFileModel):
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append({"messages": messages, "model": model})
                if len(self.calls) == 1:
                    operations = [{"op": "write", "path": "package.json", "content": '{"name":"olcr-probe"}\n'}]
                else:
                    operations = [{"op": "patch", "path": "package.json", "expected_old_fragment": '{"name":"olcr-probe"}',
                                   "replacement_fragment": '{"name":"olcr-probe","version":"1.0.0"}'}]
                    self.refreshed_source_seen = True
                return {"text": json.dumps({"change_required": True, "source_inspected": True,
                                             "condition_evaluated": True, "reason_code": "cross_attempt",
                                             "operations": operations}), "latency_ms": 0}
        model = CrossAttemptModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        first, _ = runtime.execute("Implement package metadata.", workspace_root=str(self.workspace),
                                   managed_context={**self.managed_context, "approved_scopes": [{"requested_scope": ["package.json"]}]})
        second, _ = runtime.execute("Update package metadata.", workspace_root=str(self.workspace),
                                    managed_context={**self.managed_context, "approved_scopes": [{"requested_scope": ["package.json"]}]})
        self.assertEqual("completed", first.state.value)
        self.assertEqual("completed", second.state.value)
        self.assertTrue(model.refreshed_source_seen)
        self.assertIn('"version":"1.0.0"', (self.workspace / "package.json").read_text(encoding="utf-8"))

    def test_patch_without_canonical_replacement_is_rejected_with_typed_semantic_error(self):
        model = OperationBatchModel([{"op": "patch", "path": "src/index.css",
                                      "expected_old_fragment": "/* Planned implementation stub. */\n"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Implement the stylesheet.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                    "approved_scopes": [{"requested_scope": ["src/index.css"]}]})
        self.assertEqual("failed", task.state.value)
        rejection = next(item for item in task.tool_executions if item["tool"] == "operation_rejection")
        self.assertEqual("OPERATION_SCHEMA_SEMANTIC_ERROR", rejection["input"]["failure_class"])
        self.assertIn("expected_old_fragment", rejection["input"]["operation_fields"])
        self.assertFalse((self.workspace / "src/index.css").exists())

    def test_legacy_patch_aliases_are_normalized_to_operations_v1(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("return TOKEN_A\n", encoding="utf-8")
        model = OperationBatchModel([{"op": "patch", "path": "src/math.py",
                                      "old_text": "TOKEN_A", "new_text": "TOKEN_B"}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Update src/math.py.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                    "approved_scopes": [{"requested_scope": ["src/math.py"]}]})
        self.assertEqual("completed", task.state.value)
        self.assertEqual("return TOKEN_B\n", source.read_text(encoding="utf-8"))

    def test_same_file_operations_use_state_after_previous_operation(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("value = TOKEN_A\n", encoding="utf-8")
        model = OperationBatchModel([
            {"op": "patch", "path": "src/math.py", "expected_old_fragment": "TOKEN_A", "replacement_fragment": "TOKEN_B"},
            {"op": "patch", "path": "src/math.py", "expected_old_fragment": "TOKEN_B", "replacement_fragment": "TOKEN_C"},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Update src/math.py.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                    "approved_scopes": [{"requested_scope": ["src/math.py"]}]})
        self.assertEqual("completed", task.state.value)
        self.assertEqual("value = TOKEN_C\n", source.read_text(encoding="utf-8"))
        self.assertEqual(2, len([item for item in task.tool_executions if item["tool"] == "workspace_write"]))

    def test_failed_later_operation_restores_earlier_write(self):
        source = self.workspace / "src" / "app.js"
        source.parent.mkdir()
        original = "const value = TOKEN_A;\n" + "// preserve this source line for the idempotent write check\n"
        source.write_text(original, encoding="utf-8")
        model = OperationBatchModel([
            {"op": "patch", "path": "src/app.js", "expected_old_fragment": "TOKEN_A", "replacement_fragment": "TOKEN_B"},
            {"op": "patch", "path": "src/app.js", "expected_old_fragment": "TOKEN_B", "replacement_fragment": "{"},
        ])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Update src/app.js.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                    "approved_scopes": [{"requested_scope": ["src/app.js"]}]})
        self.assertEqual("failed", task.state.value)
        self.assertEqual(original, source.read_text(encoding="utf-8"))
        failure = next(item for item in task.tool_executions if item["tool"] == "operation_application_failure")
        self.assertEqual("ROLLED_BACK", failure["output"]["worktree_state"])
        diagnostic = failure["output"]["operation_diagnostic"]
        self.assertEqual(1, diagnostic["operation_index"])
        self.assertEqual("patch", diagnostic["operation_kind"])
        self.assertEqual("src/app.js", diagnostic["relative_target_path"])
        self.assertEqual("CONTENT_VALIDATION", diagnostic["validation_stage"])
        self.assertEqual("STRUCTURAL_VALIDATION_FAILED", diagnostic["reason_subtype"])
        self.assertEqual("PASS", failure["output"]["rollback_result"])
        self.assertEqual(1, len([item for item in task.tool_executions if item["tool"] == "workspace_write"]))

    def test_empty_package_json_write_is_rejected_as_an_empty_structural_artifact(self):
        model = OperationBatchModel([{"op":"write","path":"package.json","content":""}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Create package.json.", workspace_root=str(self.workspace),
            managed_context={**self.managed_context,
                "approved_scopes":[{"requested_scope":["package.json"]}],
                "allow_package_json_file_mutation":True})
        self.assertEqual("failed", task.state.value)
        self.assertFalse((self.workspace/"package.json").exists())
        failure = next(item for item in task.tool_executions if item["tool"]=="operation_application_failure")
        self.assertIn("full-file content must not be empty",failure["output"]["failure"])
        self.assertEqual("STRUCTURAL_VALIDATION_FAILED",
            failure["output"]["operation_diagnostic"]["reason_subtype"])

    def test_successful_idempotent_write_exposes_zero_net_delta(self):
        source = self.workspace / "src" / "same.js"
        source.parent.mkdir()
        original = "const value = TOKEN_A;\n" + "// preserve this source line for the idempotent write check\n"
        source.write_text(original, encoding="utf-8")
        model = OperationBatchModel([{"op": "write", "path": "src/same.js", "content": original}])
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Keep src/same.js unchanged.", workspace_root=str(self.workspace),
                                  managed_context={**self.managed_context,
                                                   "approved_scopes": [{"requested_scope": ["src/same.js"]}]})
        self.assertEqual("completed", task.state.value)
        write = next(item for item in task.tool_executions if item["tool"] == "workspace_write")
        self.assertFalse(write["output"]["net_changed"])
        self.assertEqual(write["output"]["pre_apply_hash"], write["output"]["post_apply_hash"])

    def test_managed_web_words_do_not_select_normal_chat_web_search(self):
        self.runtime.settings.web_mode = "auto"
        self.runtime.settings.web_provider = "brave"
        request = (
            "Implement the approved Webサイト. Resolve the GitHub Releases URL from the repository, "
            "then create index.html, links.html, and styles.css in the workspace."
        )
        with patch("olcr_api.runtime.brave_search") as search:
            task, _ = self.runtime.execute(
                request,
                workspace_root=str(self.workspace),
                managed_context=self.managed_context,
            )
        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertTrue(any(item["tool"] == "workspace_write" for item in task.tool_executions))
        search.assert_not_called()

    def test_explicit_web_search_is_not_routed_to_local_file_retrieval(self):
        self.assertIsNone(Runtime._retrieval_query('Search the web for Tokyo'))
        self.assertIsNone(Runtime._retrieval_query(
            'Search the web for the current documented API, then implement the required HTML file.'))

    def test_authorized_coding_web_support_returns_to_implementation(self):
        self.runtime.settings.web_mode = "auto"
        self.runtime.settings.web_provider = "brave"
        context = {**self.managed_context, "coding_web_support": True}
        request = "Search the web for the current documented API, then implement the required HTML file."
        source = {"url": "https://example.com/api", "title": "API documentation", "rank": 1}
        fetched = {"final_url": "https://example.com/api", "text": "bounded API reference"}
        with patch("olcr_api.runtime.brave_search", return_value=[source]) as search, patch("olcr_api.runtime.web_fetch", return_value=fetched):
            task, _ = self.runtime.execute(
                request,
                workspace_root=str(self.workspace),
                managed_context=context,
            )
        search.assert_called_once()
        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertTrue(any(item["tool"] == "workspace_write" for item in task.tool_executions))

    def test_automatic_information_lookup_respects_configured_provider(self):
        class AnswerModel:
            def __init__(self):
                self.calls = []
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append((messages, model))
                return {"text": "取得した資料では、対象地域の指標は上昇傾向です。", "latency_ms": 0}

        model = AnswerModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        runtime.settings.web_mode = "auto"
        # Search recovery retains the configured provider instead of changing it.
        runtime.settings.web_provider = "tavily"
        search_result = {"url": "https://example.org/data/inflation", "title": "Official inflation data", "provider": "brave", "rank": 1}
        fetched = {"final_url": search_result["url"], "text": "Official data reports a rising annual inflation rate."}
        with patch("olcr_api.runtime.tavily_search", return_value=[search_result]) as search, \
             patch("olcr_api.runtime.web_fetch", return_value=fetched):
            task, response = runtime.execute("日本のインフレ率上昇の原因を分析して")

        search.assert_called_once()
        self.assertIn("インフレ率", search.call_args.args[0])
        self.assertNotIn("分析して", search.call_args.args[0])
        self.assertGreaterEqual(len(model.calls), 1)
        self.assertTrue(all(call[1] == runtime.settings.main_model for call in model.calls))
        self.assertIn("Official data reports", str(model.calls[0][0]))
        self.assertIn("Official inflation data", response)
        self.assertIn("Official data reports a rising annual inflation rate", response)
        self.assertIn("evidence",
                      "\n".join(item.get("content", "") for item in model.calls[0][0]))
        self.assertEqual("NEURAL", task.route.value)

    def test_api_miss_respects_configured_provider_for_vague_prompt(self):
        class AnswerModel:
            def __init__(self):
                self.calls = []
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append((messages, model))
                return {"text": "取得した資料に基づく要約です。", "latency_ms": 0}

        model = AnswerModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        runtime.settings.web_mode = "auto"
        runtime.settings.web_provider = "tavily"
        source = {"url": "https://example.org/ukraine", "title": "Conflict event data", "provider": "brave", "rank": 1}
        fetched = {"final_url": source["url"], "text": "The dataset records conflict events during 2025."}
        with patch("olcr_api.runtime.tavily_search", return_value=[source]) as search, \
             patch("olcr_api.runtime.web_fetch", return_value=fetched):
            task, response = runtime.execute("Ukraine conflict casualties in 2025?", api_miss_fallback=True)

        search.assert_called_once()
        self.assertIn("Ukraine conflict casualties in 2025", search.call_args.args[0])
        self.assertGreaterEqual(len(model.calls), 1)
        self.assertTrue(all(call[1] == runtime.settings.main_model for call in model.calls))
        self.assertIn("Conflict event data", response)
        self.assertEqual("NEURAL", task.route.value)

    def test_disabled_web_keeps_network_off_and_discloses_last_inference_fallback(self):
        calls = []
        class AnswerModel:
            def generate(self, messages, model, **kwargs):
                calls.append((messages, model))
                return {"text": "現在の死者数は確認できません。紛争の一般的背景を説明します。"}
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, AnswerModel())
        runtime.settings.web_mode = "off"
        with patch("olcr_api.runtime.brave_search") as search:
            _, response = runtime.execute("Ukraine conflict casualties in 2025?", api_miss_fallback=True)
        search.assert_not_called()
        self.assertEqual(runtime.settings.planner_model, calls[0][1])
        self.assertIn("一般知識", response)
        self.assertIn("未確認", response)
        self.assertIn("do not present them as current facts", str(calls[0][0]))

    def test_failed_brave_search_falls_back_to_wikipedia_evidence(self):
        class AnswerModel:
            def __init__(self): self.calls = []
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append(messages)
                return {"text": '{"items":[{"result_id":"wikipedia-1","summary":"Tokyo is the capital of Japan."}]}',
                        "latency_ms": 0}

        model = AnswerModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        runtime.settings.web_mode = "auto"
        runtime.settings.web_provider = "brave"
        wiki_result = {"tool_id": "knowledge.wikimedia", "provider": "Wikimedia",
                       "data": {"title": "Tokyo", "extract": "Tokyo is the capital of Japan.",
                                "page_url": "https://ja.wikipedia.org/wiki/Tokyo"},
                       "sources": [{"title": "Tokyo", "provider": "Wikipedia",
                                    "canonical_url": "https://ja.wikipedia.org/wiki/Tokyo"}]}
        with patch("olcr_api.runtime.brave_search", return_value=[]) as search, \
             patch("olcr_api.external_tools.execute", return_value=wiki_result) as wikipedia:
            task, response = runtime.execute("Search the web for Tokyo", wikipedia_fallback_allowed=True)

        self.assertEqual(2, search.call_count)
        wikipedia.assert_called_once()
        self.assertEqual("knowledge.wikimedia", wikipedia.call_args.args[0])
        self.assertEqual("Tokyo", wikipedia.call_args.args[1]["query"])
        self.assertIn("Tokyo is the capital", response)
        self.assertIn("Wikipedia", response)
        self.assertIn("Wikipedia fallback", "\n".join(
            item.get("content", "") for item in model.calls[0] if item.get("role") == "system"))

    def test_disabled_web_does_not_bypass_to_wikipedia(self):
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, object())
        runtime.settings.web_mode = "off"
        with patch("olcr_api.runtime.brave_search", side_effect=AssertionError("disabled search must not run")), \
             patch("olcr_api.external_tools.execute", side_effect=AssertionError("disabled search must not use Wikipedia")) as wikipedia:
            _, response = runtime.execute("Search the web for Tokyo")
        wikipedia.assert_not_called()
        self.assertIn("オフ", response)

    def test_automatic_lookup_without_evidence_stops_but_reasoning_may_use_configured_model(self):
        class AnswerModel:
            def __init__(self):
                self.calls = []
            def generate(self, messages, model, stream=False, think=None, format=None):
                self.calls.append((messages, model))
                return {"text": "根拠がないため断定できません。", "latency_ms": 0}

        def make_runtime(model):
            runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
            runtime.settings.web_mode = "auto"
            return runtime

        plain_model = AnswerModel()
        plain_runtime = make_runtime(plain_model)
        with patch("olcr_api.runtime.brave_search", return_value=[]):
            _, plain_response = plain_runtime.execute("日本のインフレ率のデータを教えて")
        self.assertEqual(1, len(plain_model.calls))
        self.assertEqual(plain_runtime.settings.planner_model, plain_model.calls[0][1])
        self.assertIn("一般知識", plain_response)
        self.assertIn("未確認", plain_response)

        reasoning_model = AnswerModel()
        reasoning_runtime = make_runtime(reasoning_model)
        with patch("olcr_api.runtime.brave_search", return_value=[]):
            _, reasoning_response = reasoning_runtime.execute("日本のインフレ率が上昇している理由を分析して")
        self.assertEqual(1, len(reasoning_model.calls))
        self.assertEqual(reasoning_runtime.settings.planner_model, reasoning_model.calls[0][1])
        all_prompt_text = "\n".join(item.get("content", "") for item in reasoning_model.calls[0][0])
        self.assertIn("External factual sources could not be verified", all_prompt_text)
        self.assertIn("根拠がない", reasoning_response)


if __name__ == "__main__":
    unittest.main()
