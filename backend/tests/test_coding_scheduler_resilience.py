"""Regression coverage for scheduler survival after unexpected tick errors."""

import ast
from pathlib import Path
import os
import sqlite3
import threading
import unittest


APP_SOURCE = Path(__file__).resolve().parents[1] / "olcr_api" / "app.py"


def load_scheduler_function():
    """Compile only the scheduler loop, avoiding app import/startup side effects."""
    source = APP_SOURCE.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(APP_SOURCE))
    scheduler = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_coding_scheduler"
    )

    namespace = {
        "os": os,
        "sqlite3": sqlite3,
        "threading": threading,
        "WorkspaceError": type("WorkspaceError", (Exception,), {}),
    }
    extracted = ast.Module(body=[scheduler], type_ignores=[])
    exec(compile(extracted, str(APP_SOURCE), "exec"), namespace)
    return namespace["_coding_scheduler"], namespace


class CodingSchedulerResilienceTests(unittest.TestCase):
    def assert_scheduler_retries_once_and_stops(self, error):
        stop = threading.Event()
        wake = threading.Event()
        calls = []
        second_tick_entered = threading.Event()
        release_second_tick = threading.Event()

        def tick(_concurrency):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise error("transient scheduler state mismatch")
            second_tick_entered.set()
            release_second_tick.wait(timeout=4)
            stop.set()

        scheduler, namespace = load_scheduler_function()
        namespace.update({
            "_coding_scheduler_stop": stop,
            "_coding_scheduler_wake": wake,
            "_coding_scheduler_tick": tick,
        })
        thread = threading.Thread(target=scheduler, name="test-coding-scheduler")
        try:
            thread.start()
            wake.set()
            self.assertTrue(
                second_tick_entered.wait(timeout=4),
                "scheduler exited after the unexpected tick error instead of retrying",
            )
            self.assertTrue(
                thread.is_alive(),
                "scheduler must stay alive while processing the next tick",
            )
        finally:
            release_second_tick.set()
            stop.set()
            wake.set()
            if thread.ident is not None:
                thread.join(timeout=4)

        self.assertFalse(thread.is_alive(), "scheduler thread did not stop cleanly")
        self.assertEqual([1, 2], calls, "the next queued tick must be processed exactly once")

    def test_scheduler_retries_after_one_unexpected_keyerror(self):
        self.assert_scheduler_retries_once_and_stops(KeyError)

    def test_scheduler_retries_after_one_unexpected_typeerror(self):
        self.assert_scheduler_retries_once_and_stops(TypeError)
