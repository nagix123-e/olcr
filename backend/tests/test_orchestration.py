import unittest

from olcr_api.orchestration import InvocationBoundary


class InvocationBoundaryTests(unittest.TestCase):
    def test_model_switch_requires_stop_evidence_checkpoint_and_finalized_telemetry(self):
        boundary = InvocationBoundary(task_id="task-1", phase_id="implementation")

        boundary.stop()
        boundary.commit_evidence()
        boundary.checkpoint()
        boundary.finalize_telemetry()
        boundary.next_invocation(task_id="task-1", phase_id="replan")

        self.assertEqual("NEXT_INVOCATION", boundary.state)
        self.assertEqual("replan", boundary.phase_id)
        self.assertEqual(
            ["STOP", "EVIDENCE_COMMITTED", "CHECKPOINT_SAVED",
             "TELEMETRY_FINALIZED", "NEXT_INVOCATION"],
            boundary.events,
        )
        self.assertTrue(boundary.public_dict()["invariants_pass"])
        self.assertEqual("task-1", boundary.public_dict()["task_id"])

    def test_evidence_cannot_be_committed_before_stop(self):
        boundary = InvocationBoundary(task_id="task-1")

        with self.assertRaisesRegex(RuntimeError, "EVIDENCE_BEFORE_STOP"):
            boundary.commit_evidence()

    def test_checkpoint_cannot_precede_evidence_commit(self):
        boundary = InvocationBoundary(task_id="task-1")
        boundary.stop()

        with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_ORDER"):
            boundary.checkpoint()

    def test_next_invocation_cannot_precede_telemetry_finalization(self):
        boundary = InvocationBoundary(task_id="task-1")
        boundary.stop()
        boundary.commit_evidence()
        boundary.checkpoint()

        with self.assertRaisesRegex(RuntimeError, "NEXT_BEFORE_FINALIZE"):
            boundary.next_invocation(task_id="task-1", phase_id="replan")

    def test_model_switch_cannot_change_task_identity(self):
        boundary = InvocationBoundary(task_id="task-1")
        boundary.stop()
        boundary.commit_evidence()
        boundary.checkpoint()
        boundary.finalize_telemetry()

        with self.assertRaisesRegex(RuntimeError, "TASK_ID_CHANGED"):
            boundary.next_invocation(task_id="task-2", phase_id="implementation")


if __name__ == "__main__":
    unittest.main()
