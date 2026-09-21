import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FTSRetriever, FileRetriever, RetrievalRouter
from olcr_api.runtime import Runtime


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
        self.assertIn("Return ONLY JSON", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn('"operations"', self.model.calls[0]["messages"][-1]["content"])
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
            managed_context=self.managed_context,
        )

        self.assertEqual("denied", denied.state.value)
        self.assertEqual("explicit_no_write_request", denied.reason_category)
        self.assertFalse((self.workspace / "forbidden_probe.txt").exists())
        self.assertEqual(1, len(self.model.calls))

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
        self.assertIn("def add(a, b):", self.model.calls[-1]["messages"][-1]["content"])

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
        original = "const value = TOKEN_A;\n"
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
        self.assertEqual(1, len([item for item in task.tool_executions if item["tool"] == "workspace_write"]))

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


if __name__ == "__main__":
    unittest.main()
