import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.responses import StreamingResponse

_TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["OLCR_DB_PATH"] = str(Path(_TEST_ROOT.name) / "api.db")
os.environ["OLCR_ALLOWED_ROOTS"] = _TEST_ROOT.name

import olcr_api.app as api


def test_desktop_chat_journal_forwards_and_records_the_original_stream():
    async def source_events():
        yield b'data: {"type":"chunk","text":"hello"}\n\n'
        yield b'data: {"type":"done","conversation_id":"conversation-1"}\n\n'

    response_from_chat = StreamingResponse(source_events(), media_type="text/event-stream")
    request = SimpleNamespace(headers={"X-OLCR-Run-Source": "desktop"})

    with (
        patch.object(api, "_pc_run_observer_prompt", return_value="safe prompt metadata"),
        patch.object(api, "_stream_chat_impl", return_value=response_from_chat),
        patch.object(api.db, "conversation", return_value={"messages": []}),
        patch.object(api.mobile_runs, "register_pc_stream", return_value="run-1"),
        patch.object(api.mobile_runs, "append") as append,
        patch.object(api.mobile_runs, "finish_pc_stream") as finish,
    ):
        response = api.stream_chat(api.ChatInput(message="hello", conversation_id="conversation-1"), request)

        async def consume():
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(consume())

    assert b"".join(chunks) == (
        b'data: {"type":"chunk","text":"hello"}\n\n'
        b'data: {"type":"done","conversation_id":"conversation-1"}\n\n'
    )
    append.assert_any_call("run-1", {"type": "chunk", "text": "hello"}, terminal_state=None)
    append.assert_any_call(
        "run-1",
        {"type": "done", "conversation_id": "conversation-1"},
        terminal_state="completed",
    )
    finish.assert_called_once_with("run-1", "completed")
