"""Regressions from the persisted OLCR static-site manual request."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.coding_tasks import (classify_execution_mode, execution_mode_diagnostics,
    compact_normal_plan, workspace_mutation_count, zero_mutation_retry_instruction)

GOAL = (Path(__file__).parent / 'fixtures/olcr_static_website.txt').read_text()


def make_plan():
    return {'phases': [dict(id=f'p{i}', goal=goal, done=[f'{goal} observable'], verify=['browser check'],
                           dependencies=[] if i == 1 else [f'p{i-1}'], status='pending', risks=[])
                       for i, goal in enumerate(['create index.html', 'create links.html', 'add styles.css', 'browser verification'], 1)]}


class ClassificationTests(unittest.TestCase):
    def test_individual_exclusions_do_not_leak(self):
        for exclusion in ['Backendは不要', 'Reactは使用しない', 'Databaseなし', 'FastAPIを使わない',
                          'npm依存不要', 'No frontend', 'No backend', 'No database', 'No full-stack',
                          'Do not use React, FastAPI, SQLite', 'Backend、Database、Reactは不要', 'No framework, backend, database, or dependencies']:
            with self.subTest(exclusion=exclusion):
                goal = 'Create a static HTML website with index.html.\n' + exclusion
                self.assertEqual('NORMAL', classify_execution_mode(goal))
                self.assertEqual([], execution_mode_diagnostics(goal)['POSITIVE_SIGNALS'])

    def test_japanese_suffixes_preserve_signal_identity(self):
        for term, signal in [('Backendは不要', 'backend'), ('Reactは使用しない', 'frontend'), ('Databaseなし', 'database'), ('npm依存不要', 'dependencies')]:
            self.assertIn(signal, execution_mode_diagnostics(term)['NEGATED_SIGNALS'])
        self.assertEqual('HEAVY_BATCHED', classify_execution_mode('Reactでフロントエンドを作成。FastAPIでバックエンドを実装。SQLiteに保存。'))

    def test_real_prompt_is_normal_and_compacts(self):
        self.assertEqual('NORMAL', classify_execution_mode(GOAL))
        self.assertEqual([], execution_mode_diagnostics(GOAL)['POSITIVE_SIGNALS'])
        self.assertEqual(2, len(compact_normal_plan(make_plan(), GOAL)['phases']))

    def test_fullstack_stays_heavy_and_positive_override_survives(self):
        for goal in ['Create a Task Tracker: React + TypeScript\nFastAPI\nSQLite',
                     '実装しないもの:\n- React\n- FastAPI\n- Database\n要件:\nReact frontend + FastAPI backend + SQLite persistence',
                     'No database, but use React frontend and FastAPI backend']:
            with self.subTest(goal=goal):
                self.assertEqual('HEAVY_BATCHED', classify_execution_mode(goal))
        goal = 'static HTML website\nNo backend\nUse React frontend and FastAPI backend'
        self.assertEqual(4, len(compact_normal_plan(make_plan(), goal)['phases']))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.phase = make_plan()['phases'][0]
        self.report = {'phase_id': 'p1', 'attempt': 0, 'status': 'FAIL', 'errors': ['missing file'],
                       'typed_execution_summary': {'state': 'failed', 'operations': []}}

    def test_zero_mutation_requires_explicit_instruction(self):
        self.report['implemented'] = ['I created index.html']
        self.assertIn('Do not only explain', zero_mutation_retry_instruction(self.phase, self.report))
        self.assertEqual(0, workspace_mutation_count(self.report))

    def test_zero_mutation_patch_failure_gets_specific_correction(self):
        self.report['typed_execution_summary']['error'] = 'patch precondition failed; source changed or fragment is ambiguous'
        self.assertIn('for missing files use a write operation', zero_mutation_retry_instruction(self.phase, self.report))
        self.report['typed_execution_summary']['error'] = 'model output invalid'
        self.assertNotIn('patch precondition', zero_mutation_retry_instruction(self.phase, self.report))

    def test_real_mutation_does_not_get_zero_write_instruction(self):
        for tool in ['workspace_write', 'workspace_write_normalized', 'workspace_patch', 'workspace_delete']:
            self.report['typed_execution_summary']['operations'] = [{'tool': tool, 'status': 'success', 'output': {'path': 'index.html'}}]
            self.assertEqual('', zero_mutation_retry_instruction(self.phase, self.report))
            self.assertEqual(1, workspace_mutation_count(self.report))
        self.report['manager_decision'] = {'decision': 'RETRY', 'diagnosis': {'retry_instruction': 'Fix the failed browser check'}}
        self.assertEqual('Fix the failed browser check', api._bounded_review_context(self.report)['diagnosis']['retry_instruction'])

    def test_prior_done_evidence_and_japanese_verification_are_not_zero_write_failures(self):
        phase = {'id': 'p1', 'goal': 'create index.html', 'done': ['index.html exists'], 'verify': ['browser check']}
        prior = {**self.report, 'typed_execution_summary': {'operations': [{'tool': 'workspace_write', 'status': 'success', 'output': {'path': 'index.html'}}]}}
        self.assertEqual('', zero_mutation_retry_instruction(phase, self.report, [prior]))
        phase = {'id': 'p1', 'goal': '実装内容をブラウザで確認', 'done': ['実装結果を確認'], 'verify': ['browser check']}
        self.assertEqual('', zero_mutation_retry_instruction(phase, self.report))
        for goal in ['実装したWebサイトをブラウザで確認', '最終的な報告を作成', 'write the final report', 'Do not edit files; inspect index.html']:
            phase = {'id': 'p1', 'goal': goal, 'done': ['report available'], 'verify': ['manual check']}
            self.assertEqual('', zero_mutation_retry_instruction(phase, self.report))

    def test_failed_write_and_verification_only_phase(self):
        self.report['typed_execution_summary']['operations'] = [{'tool': 'workspace_write', 'status': 'failed', 'output': {'path': 'index.html'}}]
        self.assertTrue(zero_mutation_retry_instruction(self.phase, self.report))
        self.assertEqual('', zero_mutation_retry_instruction(make_plan()['phases'][-1], self.report))
        self.report['criteria_evidence'] = {self.phase['done'][0]: True}
        self.assertEqual('', zero_mutation_retry_instruction(self.phase, self.report))


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.old_db = api.db
        api.db = Database(str(Path(self.tmp.name) / 'db.sqlite')); api.db.initialize()
        api.db.create_project('Project', self.tmp.name, time.time(), 'project')
        api.db.create_conversation('Conversation', time.time(), 'conversation', 'project')
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db = self.old_db
        self.tmp.cleanup()

    def task(self, reason):
        plan = make_plan(); plan['phases'][0]['status'] = 'pass'
        api.db.create_coding_task('task', 'conversation', GOAL, 'RESUMABLE', 'NONE', plan, time.time())
        api.db.update_coding_task('task', current_phase_id='p2', retry_count=1,
                                 recovery_reason=reason, recovery_action='RECOVERY_REVIEW')
        return api.db.coding_task('task')

    def test_continue_preserves_phase_and_retry_for_each_reason(self):
        for reason in ['RESOURCE_CHECKPOINT', 'MANAGER_DECISION', 'PAUSED', 'USER_PAUSE']:
            with self.subTest(reason=reason):
                if not api.db.coding_task('task'): self.task(reason)
                api.db.update_coding_task('task', status='RESUMABLE', recovery_reason=reason)
                before = api.db.coding_task('task')
                with patch.object(api.runtime, 'execute', side_effect=AssertionError('Generic Brain intercepted')), patch.object(api._coding_scheduler_wake, 'set') as wake:
                    result = self.client.post('/api/chat', json={'message': '続行', 'conversation_id': 'conversation', 'project_id': 'project'})
                self.assertEqual('task', result.json()['coding_task_id']); wake.assert_called_once()
                after = api.db.coding_task('task')
                self.assertEqual('QUEUED', after['status'])
                for field in ['current_phase_id', 'retry_count', 'plan', 'recovery_reason', 'recovery_action']:
                    self.assertEqual(before[field], after[field])

    def test_continue_never_authorizes(self):
        self.task('MANAGER_DECISION')
        api.db.update_coding_task('task', pending_authorization={'requested_scope': ['deploy'], 'state': 'PENDING'})
        with patch.object(api.runtime, 'execute', side_effect=AssertionError('Brain intercepted')), patch.object(api._coding_scheduler_wake, 'set') as wake:
            result = self.client.post('/api/chat', json={'message': '続行', 'conversation_id': 'conversation', 'project_id': 'project'})
            self.assertEqual('task', result.json()['coding_task_id']); wake.assert_not_called()
            self.assertEqual(409, self.client.patch('/api/coding-tasks/task', json={'resume': True}).status_code)
        self.assertEqual('RESUMABLE', api.db.coding_task('task')['status'])
        self.assertTrue(api.db.coding_task('task')['pending_authorization'])

    def test_manager_resume_runs_current_phase_with_saved_attempt_and_instruction(self):
        self.task('MANAGER_DECISION')
        report = {'phase_id': 'p2', 'attempt': 0, 'plan_revision': 0, 'status': 'FAIL',
                  'errors': ['no mutation'], 'manager_decision': {'decision': 'RETRY', 'reason': 'recoverable'},
                  'typed_execution_summary': {'state': 'failed', 'operations': []}}
        for attempt in (0, 1):
            api.db.add_coding_phase_report('task', 'p2', attempt, {**report, 'attempt': attempt}, 'PASS', time.time())
        with patch.object(api._coding_scheduler_wake, 'set'):
            self.client.post('/api/chat', json={'message': '続行', 'conversation_id': 'conversation', 'project_id': 'project'})
        with patch.object(api, 'coding_knowledge_context_for_subtask', return_value=''), patch.object(api.runtime, 'execute', side_effect=RuntimeError('captured implementation')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'captured implementation'):
                api.run_managed_task('task', self.tmp.name)
        request = execute.call_args.args[0]
        self.assertIn('Phase: create links.html', request)
        self.assertIn('Attempt: 2', request)
        self.assertIn('The previous attempt produced no authoritative workspace mutation', request)
        self.assertNotIn('Phase: create index.html', request)
        self.assertEqual('p2', api.db.coding_task('task')['current_phase_id'])
        self.assertEqual(1, api.db.coding_task('task')['retry_count'])

    def test_manager_retry_after_progress_refreshes_live_preimages(self):
        self.task('MANAGER_DECISION')
        report = {'phase_id': 'p2', 'attempt': 0, 'plan_revision': 0, 'status': 'FAIL',
                  'errors': ['patch precondition failed'],
                  'manager_decision': {'decision': 'RETRY', 'reason': 'recoverable'},
                  'accepted_mutations': 1, 'files_written': 1, 'phase_progress': True,
                  'typed_execution_summary': {
                      'state': 'failed', 'failure_class': 'PREIMAGE_MISMATCH',
                      'worktree_state': 'ROLLED_BACK', 'accepted_mutations': 1,
                      'files_written': 1, 'phase_progress': True,
                      'preimage_diagnostics': [{
                          'path': 'links.html', 'preimage_failure_reason': 'STALE_OLD_TEXT',
                          'executor_actual_hash': 'actual', 'expected_old_hash': 'expected'}],
                      'operations': []}}
        api.db.add_coding_phase_report('task', 'p2', 0, report, 'PASS', time.time())
        with patch.object(api._coding_scheduler_wake, 'set'):
            self.client.post('/api/chat', json={'message': '続行', 'conversation_id': 'conversation', 'project_id': 'project'})
        with patch.object(api, 'coding_knowledge_context_for_subtask', return_value=''), \
             patch.object(api.runtime, 'execute', side_effect=RuntimeError('captured implementation')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'captured implementation'):
                api.run_managed_task('task', self.tmp.name)
        request = execute.call_args.args[0]
        self.assertIn('Re-enumerate the current authorized files', request)
        self.assertIn('rebuild every source hash and patch preimage from the live worktree', request)
        self.assertIn('preimage_diagnostics', request)
        self.assertIn('preserve successful prior mutations', request)

    def test_resume_cannot_bypass_exhausted_retry_budget(self):
        self.task('MANAGER_DECISION')
        for attempt in range(3):
            report = {'phase_id': 'p2', 'attempt': attempt, 'status': 'FAIL', 'manager_decision': {'decision': 'RETRY'}}
            api.db.add_coding_phase_report('task', 'p2', attempt, report, 'PASS', time.time())
        with patch.object(api._coding_scheduler_wake, 'set'):
            self.client.patch('/api/coding-tasks/task', json={'resume': True})
        with patch.object(api, '_replan_task', return_value='bounded replan') as replan, patch.object(api.runtime, 'execute') as execute:
            self.assertEqual('bounded replan', api.run_managed_task('task', self.tmp.name))
            replan.assert_called_once(); execute.assert_not_called()
