import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import Route, Task
from olcr_api.retrieval import DisabledVectorStore, FTSRetriever, FileRetriever, RetrievalRouter
from olcr_api.runtime import Runtime
from olcr_api.models import TaskState


class ProseImplementer:
    def generate(self, messages, model, stream=False, think=None, format=None):
        return {"text": "Workspace Inspection & Implementation Start\n```ts\ninstallDependency('invented')\n```", "latency_ms": 0}


class SplitThinkingStream:
    def generate(self, messages, model, stream=False, think=None, format=None):
        if stream:
            return iter(({"text": "<think>private reasoning"}, {"text": "</think>visible answer", "done": True}))
        return {"text": "visible answer", "latency_ms": 0}


class ExecutionResponseOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.old_db = api.db
        self.db = Database(str(self.root / "olcr.sqlite"))
        self.db.initialize()

    def tearDown(self):
        api.db = self.old_db
        self.tmp.cleanup()

    def test_free_form_implementer_output_is_typed_failure_and_never_execution(self):
        settings = Settings(allowed_roots=(str(self.workspace),), db_path=self.db.path,
                            main_model="deterministic-prose").validated()
        retrieval = RetrievalRouter(FileRetriever([str(self.workspace)]), FTSRetriever(self.db),
                                    DisabledVectorStore(), False)
        runtime = Runtime(settings, self.db, retrieval, ProseImplementer())
        task, response = runtime.execute(
            "implement the approved file change",
            core_context="",
            workspace_root=str(self.workspace),
            managed_context={"managed_coding_task": True, "operation_intent": "IMPLEMENTATION"},
        )
        self.assertEqual(TaskState.FAILED, task.state)
        self.assertIn("IMPLEMENTER_STRUCTURED_OUTPUT_INVALID", task.error)
        self.assertNotIn("installDependency", response)
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_thinking_channel_is_removed_before_user_rendering(self):
        self.assertEqual("visible answer", api._sanitize_user_visible_assistant_text(
            "<think>private reasoning</think>visible answer"))
        self.assertEqual("visible answer", api._sanitize_user_visible_assistant_text(
            "private reasoning</think>visible answer"))
        self.assertNotIn("private reasoning", api._sanitize_user_visible_assistant_text(
            "<think>private reasoning</think>visible answer"))

    def test_failed_execution_report_does_not_copy_model_prose(self):
        task = Task("implementation")
        task.transition(TaskState.ROUTING)
        task.route = Route.IMPLEMENTATION
        task.transition(TaskState.EXECUTING)
        task.error = "IMPLEMENTER_STRUCTURED_OUTPUT_INVALID"
        task.transition(TaskState.FAILED)
        report = api._report_from_execution(
            {"id": "p1", "goal": "implement a file", "done": ["file exists"], "verify": []},
            0, task, "Workspace Inspection & Implementation Start")
        self.assertEqual([], report["implemented"])

    def test_stream_continuation_is_owned_by_coding_orchestrator(self):
        api.db = self.db
        self.db.create_project("Project", str(self.workspace), time.time(), "project")
        self.db.create_conversation("Conversation", time.time(), "conversation", "project")
        self.db.create_coding_task("task", "conversation", "continue implementation", "RESUMABLE", "NONE", {}, time.time())
        from olcr_api.app import ChatInput

        def consume(response):
            async def read():
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
                return "".join(chunks)
            return asyncio.run(read())

        with patch.object(api, "chat", return_value={
            "conversation_id": "conversation", "coding_task_id": "task", "response": "",
            "progress_event": {"type": "coding_task_progress", "task_id": "task", "status": "QUEUED"},
        }) as routed:
            result = api.stream_chat(ChatInput(message="続行", conversation_id="conversation", project_id="project"))
            body = consume(result)
        routed.assert_called_once()
        self.assertIn('"response_owner": "CODING_ORCHESTRATOR"', body)
        self.assertNotIn("Workspace Inspection", body)
        self.assertNotIn("installDependency", body)
        self.assertNotIn("<think>", body)

    def test_streaming_reasoning_channel_is_not_emitted(self):
        api.db = self.db
        self.db.create_project("Project", str(self.workspace), time.time(), "project")
        self.db.create_conversation("Conversation", time.time(), "conversation", "project")
        from olcr_api.app import ChatInput

        async def read(response):
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        with patch.object(api.runtime, "model", SplitThinkingStream()):
            body = asyncio.run(read(api.stream_chat(ChatInput(
                message="hello", conversation_id="conversation", project_id="project"))))
        self.assertIn("visible answer", body)
        self.assertNotIn("private reasoning", body)
        self.assertNotIn("<think>", body)


if __name__ == "__main__":
    unittest.main()
