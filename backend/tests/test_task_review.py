import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from olcr_api.db import Database
from olcr_api.git_api import GitIntegration
from olcr_api.git_workspace import GitError, GitWorkspace
from olcr_api.github_review import GitHubReview
from olcr_api.task_workspace import TaskWorkspaces, TaskWorkspaceIntegration, WorkspaceError
from olcr_api.task_review_api import TaskReviewAPI


class TaskReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'source'; self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        self.git('config', 'commit.gpgsign', 'false')
        (self.root / 'a.py').write_text('original\n')
        (self.root / 'b.py').write_text('unrelated\n')
        self.git('add', '.'); self.git('commit', '-m', 'Initial')
        self.db = Database(str(Path(self.temporary.name) / 'db.sqlite')); self.db.initialize()
        self.db.create_project('Project', str(self.root), time.time(), 'p')
        self.db.create_conversation('Task', time.time(), 'c', 'p')
        self.service = TaskWorkspaces(self.db)

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args, root=None):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root or self.root), *args],
                              check=True, capture_output=True).stdout.decode().strip()

    def task(self, task_id='task', mode='direct'):
        self.service.preference('p', mode)
        plan = {'file_manifest': [{'path': 'a.py'}, {'path': 'created.txt'}], 'phases': []}
        self.db.create_coding_task(task_id, 'c', 'fix a.py', 'RESUMABLE', 'NONE', plan, time.time())
        self.service.reserve(task_id)
        meta = self.service.prepare(task_id)
        return Path(meta['root'])

    def test_starting_dirty_content_is_baseline_and_unrelated_edits_are_not_task_changes(self):
        (self.root/'a.py').write_text('user edit\n')
        root = self.task()
        (root/'a.py').write_text('task edit\n')
        (root/'b.py').write_text('concurrent unrelated edit\n')
        (root/'created.txt').write_text('new\n')
        self.service.checkpoint('task')
        changes = self.service.changes('task')
        self.assertEqual(['a.py', 'created.txt'], [f['path'] for f in changes['files']])
        self.assertIn('-user edit', changes['files'][0]['diff'])
        self.assertNotIn('-original', changes['files'][0]['diff'])
        self.service.revert('task', 'a.py', changes['revision'])
        self.assertEqual('user edit\n', (root/'a.py').read_text())
        self.assertEqual('concurrent unrelated edit\n', (root/'b.py').read_text())

    def test_stale_review_running_task_and_symlinks_cannot_overwrite_user_files(self):
        root = self.task(); (root/'a.py').write_text('task\n')
        self.service.checkpoint('task')
        changes = self.service.changes('task')
        (root/'a.py').write_text('new user edit\n')
        with self.assertRaisesRegex(WorkspaceError, 'REFRESH_REQUIRED'):
            self.service.revert('task', 'a.py', changes['revision'])
        self.db.update_coding_task('task', status='RUNNING')
        with self.assertRaisesRegex(WorkspaceError, 'PAUSE'):
            self.service.revert('task', 'a.py', self.service.changes('task')['revision'])
        self.db.update_coding_task('task', status='RESUMABLE')
        (root/'a.py').unlink(); (root/'a.py').symlink_to(root/'b.py')
        self.assertFalse(self.service.changes('task')['files'][0]['revertible'])
        for path in ['../outside', '.git/config', '/tmp/file']:
            with self.assertRaises(WorkspaceError): self.service._read(root, path)

    def test_deletions_binary_and_executable_mode_can_be_restored(self):
        (self.root/'a.py').write_bytes(b'\x00binary')
        os.chmod(self.root/'a.py', 0o755)
        root=self.task(); (root/'a.py').unlink()
        self.service.checkpoint('task')
        changes=self.service.changes('task')
        self.assertTrue(changes['files'][0]['binary'])
        self.assertEqual('DELETED',changes['files'][0]['status'])
        self.service.revert('task','a.py',changes['revision'])
        self.assertEqual(b'\x00binary',(root/'a.py').read_bytes())
        self.assertEqual(0o755,(root/'a.py').stat().st_mode & 0o777)

    def test_worktrees_persist_and_apply_only_reviewed_changes_without_overwriting_source(self):
        (self.root/'b.py').write_text('user source edit\n')
        root=self.task(mode='worktree')
        self.assertNotEqual(root,self.root)
        self.assertEqual('unrelated\n',(root/'b.py').read_text())
        (root/'a.py').write_text('isolated change\n')
        (root/'created.txt').write_text('new\n')
        self.assertEqual(str(root),TaskWorkspaces(self.db).root_for('task'))
        self.service.checkpoint('task')
        changes=self.service.changes('task')
        self.service.apply('task',changes['revision'],['a.py','created.txt'])
        self.assertEqual('isolated change\n',(self.root/'a.py').read_text())
        self.assertEqual('user source edit\n',(self.root/'b.py').read_text())
        self.service.apply('task',changes['revision'],['a.py'])
        (self.root/'a.py').write_text('new source edit\n')
        with self.assertRaisesRegex(WorkspaceError,'SOURCE_CHANGED_CONFLICT'):
            self.service.apply('task',changes['revision'],['a.py'])

    def test_multi_file_apply_preflights_every_source_and_rollback_restores_prior_writes(self):
        root=self.task(mode='worktree'); (root/'a.py').write_text('changed\n'); (root/'b.py').write_text('changed too\n')
        self.service.checkpoint('task')
        changes=self.service.changes('task')
        original=self.service._write
        def write(root,path,state,contents):
            if root.resolve() == self.root.resolve() and path == 'b.py' and contents == b'changed too\n': raise OSError('fixture disk failure')
            return original(root,path,state,contents)
        with patch.object(self.service,'_write',side_effect=write), self.assertRaises(OSError):
            self.service.apply('task',changes['revision'],['a.py','b.py'])
        self.assertEqual('original\n',(self.root/'a.py').read_text())

    def test_clean_worktree_removal_preserves_branch_and_restore(self):
        root=self.task(mode='worktree')
        self.service.archive_worktree('task')
        self.assertFalse(root.exists())
        self.assertFalse(self.service.changes('task')['available'])
        self.service.restore_worktree('task')
        self.assertTrue(root.exists())
        self.assertEqual('original\n',(root/'a.py').read_text())

    def test_steering_is_durable_revision_checked_and_claimed_once(self):
        self.task()
        with self.assertRaisesRegex(WorkspaceError,'PLAN_CHANGED'):
            self.service.steer('task','Keep original colors',99)
        self.service.steer('task','Keep original colors',0)
        rows=self.service.take_steering('task')
        self.assertEqual(1,len(rows)); self.assertEqual([],self.service.take_steering('task'))
        self.service.finish_steering(rows,1,True)
        self.assertEqual('APPLIED',self.service.steering('task')[0]['status'])
        self.assertEqual(1,self.service.steering('task')[0]['applied_revision'])

    def test_user_edits_after_checkpoint_remain_non_revertible_after_refresh(self):
        root=self.task();(root/'a.py').write_text('task output\n')
        self.service.checkpoint('task')
        (root/'a.py').write_text('subsequent user output\n')
        changes=self.service.changes('task')
        self.assertTrue(changes['files'][0]['externally_modified'])
        with self.assertRaisesRegex(WorkspaceError,'CANNOT_BE_REVERTED'):
            self.service.revert('task','a.py',changes['revision'])

    def test_executor_hash_detects_edit_between_execution_and_checkpoint(self):
        import hashlib
        root=self.task();(root/'a.py').write_text('user output\n')
        self.service.checkpoint_execution('task',[{'tool':'workspace_write','status':'success',
            'output':{'path':str(root/'a.py'),'post_apply_hash':hashlib.sha256(b'task output\n').hexdigest()}}])
        self.assertTrue(self.service.changes('task')['files'][0]['externally_modified'])

    def test_worktree_preference_is_frozen_on_task_creation_and_rebases_explicit_paths(self):
        root=self.task(mode='worktree')
        self.service.preference('p','direct')
        plan={'original_goal':f'Fix {self.root}/a.py','file_manifest':[{'path':str(self.root.resolve()/'a.py')}],
              'implementation_plan_artifact':{'target_root':str(self.root.resolve())}}
        rebased=self.service.execution_plan('task',plan)
        self.assertEqual(str(root/'a.py'),rebased['file_manifest'][0]['path'])
        self.assertEqual(str(root),rebased['implementation_plan_artifact']['target_root'])
        self.assertEqual(plan['original_goal'],rebased['original_goal'])

    def test_git_execution_waits_for_inflight_task_even_after_pause_status_changes(self):
        self.task();(self.root/'a.py').write_text('task output\n')
        git=GitWorkspace(self.db,self.service.conversation_root,self.service.assert_idle)
        operation=git.prepare('c','commit',paths=['a.py'],message='Fix',task_id='task')
        with self.service.executing('task'):
            with self.assertRaisesRegex(GitError,'WAIT_FOR_CODING'):
                git.execute(operation['id'],'c')
        self.assertEqual('SUCCEEDED',git.execute(operation['id'],'c')['status'])

    def test_explicit_manifest_captures_existing_files_in_ignored_directories_before_write(self):
        directory=self.root/'dist';directory.mkdir();(directory/'a.txt').write_text('before\n')
        self.task()
        plan={'file_manifest':[{'path':'dist/a.txt'}]}
        self.service.capture_manifest('task',plan)
        self.db.update_coding_task('task',plan=plan)
        (directory/'a.txt').write_text('after\n')
        self.service.checkpoint('task')
        self.assertIn('-before',self.service.changes('task')['files'][0]['diff'])

    def test_review_api_and_git_target_the_selected_task(self):
        root=self.task(mode='worktree'); (root/'a.py').write_text('task\n')
        integration=TaskWorkspaceIntegration(lambda:self.db)
        app=FastAPI(); app.include_router(TaskReviewAPI(integration,lambda:None).router)
        from types import SimpleNamespace
        git=GitIntegration(lambda:self.db,lambda:SimpleNamespace(external_access_enabled=False),self.service.conversation_root)
        app.include_router(git.router); client=TestClient(app)
        changes=client.get('/api/coding-tasks/task/review').json()
        self.assertEqual(str(root),changes['workspace']['root'])
        self.assertEqual(str(root),client.get('/api/conversations/c/git/status?task_id=task').json()['workspace'])
        result=client.post('/api/conversations/c/git/prepare',json={'action':'commit','paths':['a.py'],'message':'Fix','task_id':'task'})
        self.assertEqual(200,result.status_code,result.text)
        operation=result.json(); result=client.post(f"/api/conversations/c/git/operations/{operation['id']}/approve")
        self.assertEqual('SUCCEEDED',result.json()['status'])
        self.assertEqual('original\n',(self.root/'a.py').read_text())

    def test_branch_creation_requires_review_and_detects_stale_state(self):
        self.task()
        git=GitWorkspace(self.db)
        op=git.prepare('c','branch',branch='olcr/feature')
        self.assertEqual('main',self.git('branch','--show-current'))
        self.assertEqual('SUCCEEDED',git.execute(op['id'],'c')['status'])
        self.assertEqual('olcr/feature',self.git('branch','--show-current'))
        with self.assertRaises(GitError): git.execute(op['id'],'c')

    def test_github_pr_creation_preserves_body_literal_and_checks_remote_state(self):
        self.task(); git=GitWorkspace(self.db); github=GitHubReview(git)
        head=self.git('rev-parse','HEAD')
        op={'remote':'origin','remote_url':'https://github.com/owner/repo.git','destination_branch':'olcr/task','base':'main',
            'head':head,'base_head':head,'repository':'owner/repo','title':'Fix','body':'Line one\n`$(literal)`\nLine three','draft':True}
        calls=[]
        def run(root,args,access,as_json=False):
            calls.append(args)
            if 'list' in args: return []
            self.assertEqual(op['body'],Path(args[args.index('--body-file')+1]).read_text())
            return 'https://github.com/owner/repo/pull/12'
        with patch.object(git,'_push_target',return_value=op['remote_url']), patch.object(git,'_remote_head',return_value=head), patch.object(github,'run',side_effect=run):
            self.assertEqual('https://github.com/owner/repo/pull/12',github.create(self.root,op,True)['pr_url'])
        self.assertIn('--draft',calls[-1])
        with self.assertRaisesRegex(GitError,'EXTERNAL_ACCESS'):
            github.create(self.root,op,False)

    def test_pr_operation_reviews_diff_and_does_not_replay_changed_remote(self):
        self.task();self.git('remote','add','origin','https://github.com/owner/repo.git')
        base=self.git('rev-parse','HEAD');self.git('switch','-c','olcr/feature')
        (self.root/'a.py').write_text('feature\n');self.git('add','a.py');self.git('commit','-m','Feature')
        head=self.git('rev-parse','HEAD');git=GitWorkspace(self.db)
        remote=lambda root,url,branch: base if branch=='main' else head
        with patch.object(git,'_remote_head',side_effect=remote):
            operation=git.prepare('c','pull_request',title='Feature',body='Details\nValidation',external_access=True)
        self.assertIn('+feature',operation['diff']);self.assertEqual('PENDING',operation['status'])
        with patch.object(git,'_remote_head',return_value='b'*40),patch.object(GitHubReview,'run',side_effect=AssertionError('do not create after base change')):
            self.assertEqual('FAILED',git.execute(operation['id'],'c',external_access=True)['status'])
        with self.assertRaisesRegex(GitError,'ALREADY_HANDLED'):
            git.execute(operation['id'],'c',external_access=True)

    def test_existing_open_pr_returns_url_without_duplicate_create(self):
        self.task();git=GitWorkspace(self.db);github=GitHubReview(git)
        head=self.git('rev-parse','HEAD')
        op={'remote':'origin','remote_url':'https://github.com/owner/repo.git','destination_branch':'olcr/feature',
            'base':'main','base_head':head,'head':head,'repository':'owner/repo'}
        with patch.object(git,'_push_target',return_value=op['remote_url']),patch.object(git,'_remote_head',return_value=head),patch.object(github,'run',return_value=[{'url':'https://github.com/owner/repo/pull/1'}]) as run:
            self.assertTrue(github.create(self.root,op,True)['pr_already_exists'])
        self.assertEqual(1,run.call_count)

    def test_interrupted_steering_requires_review_after_restart(self):
        self.task();self.service.steer('task','Keep current behavior',0);self.service.take_steering('task')
        restarted=TaskWorkspaces(self.db);restarted.recover_steering()
        self.assertEqual('NEEDS_REVIEW',restarted.steering('task')[0]['status'])
        self.assertEqual([],restarted.take_steering('task'))

    def test_generated_files_are_hidden_but_explicit_artifacts_can_be_reviewed_and_committed(self):
        root=self.task(mode='worktree')
        for directory in ['node_modules/pkg','__pycache__','dist']:
            (root/directory).mkdir(parents=True,exist_ok=True)
        (root/'node_modules/pkg/cache.js').write_text('generated\n')
        (root/'__pycache__/cache.pyc').write_bytes(b'cache')
        (root/'dist/result.txt').write_text('explicit artifact\n')
        plan={'file_manifest':[{'path':'dist/result.txt','action':'create'}]}
        self.db.update_coding_task('task',plan=plan)
        git=GitWorkspace(self.db,self.service.conversation_root)
        self.assertEqual(['dist/result.txt'],[row['path'] for row in git.status('c','task')['files']])
        self.assertEqual(['dist/result.txt'],[row['path'] for row in self.service.changes('task')['files']])
        operation=git.prepare('c','commit',paths=['dist/result.txt'],message='Artifact',task_id='task')
        self.assertEqual('SUCCEEDED',git.execute(operation['id'],'c')['status'])

    def test_credential_file_contents_are_not_persisted_in_snapshot_or_diff(self):
        (self.root/'.env').write_text('API_KEY=fixture-token-never-copy\n')
        self.task()
        state,contents=self.service._baseline('task')['.env']
        self.assertIsNone(contents);self.assertFalse(state['reviewable'])
        self.db.update_coding_task('task',plan={'file_manifest':[{'path':'.env','action':'modify'}]})
        (self.root/'.env').write_text('API_KEY=new-fixture-token\n')
        row=self.service.changes('task')['files'][0]
        self.assertTrue(row['sensitive']);self.assertEqual('',row['diff']);self.assertFalse(row['revertible'])

    def test_followup_preserves_previous_implementation_and_has_its_own_baseline(self):
        first=self.task('first',mode='worktree');(first/'a.py').write_text('first implementation\n')
        self.db.update_coding_task('first',status='COMPLETED',archived=True)
        second=self.task('second',mode='worktree')
        self.assertEqual(first,second)
        self.assertEqual('first implementation\n',(second/'a.py').read_text())
        (second/'a.py').write_text('refined implementation\n')
        changes=self.service.changes('second')
        self.assertIn('-first implementation',changes['files'][0]['diff'])
        self.assertNotIn('-original',changes['files'][0]['diff'])
        self.assertEqual('original\n',(self.root/'a.py').read_text())
        self.git('add','a.py',root=second);self.git('commit','-m','Refine',root=second)
        self.service.archive_worktree('second')
        self.assertTrue(self.service.metadata('first')['removed'])
        self.assertFalse(self.service.restore_worktree('first')['removed'])
        self.assertFalse(self.service.metadata('second')['removed'])
        self.assertEqual('refined implementation\n',(second/'a.py').read_text())

    def test_new_chat_gets_independent_worktree_after_another_chat_completes(self):
        first=self.task('first',mode='worktree');(first/'a.py').write_text('first implementation\n')
        self.db.update_coding_task('first',status='COMPLETED')
        self.db.create_conversation('Other chat',time.time(),'other','p')
        self.db.create_coding_task('other','other','Fix a.py','RESUMABLE','NONE',{'file_manifest':[{'path':'a.py'}]},time.time())
        self.service.reserve('other');second=Path(self.service.prepare('other')['root'])
        self.assertNotEqual(first,second)
        self.assertEqual('original\n',(second/'a.py').read_text())

    def test_followup_applies_cumulative_changes_including_inherited_files(self):
        root=self.task('first',mode='worktree')
        (root/'a.py').write_text('first implementation\n')
        (root/'created.txt').write_text('inherited addition\n')
        self.service.checkpoint('first')
        self.db.update_coding_task('first',status='COMPLETED')
        self.task('second',mode='worktree')
        (root/'a.py').write_text('refined implementation\n')
        self.service.checkpoint('second')
        changes=self.service.changes('second')
        rows={row['path']:row for row in changes['files']}
        self.assertIn('-first implementation',rows['a.py']['diff'])
        self.assertIn('-original',rows['a.py']['source_diff'])
        self.assertTrue(rows['created.txt']['inherited'])
        self.assertFalse(rows['created.txt']['revertible'])
        self.service.apply('second',changes['revision'],['a.py','created.txt'])
        self.assertEqual('refined implementation\n',(self.root/'a.py').read_text())
        self.assertEqual('inherited addition\n',(self.root/'created.txt').read_text())

    def test_followup_accepts_own_previous_source_apply_and_rejects_unrelated_source_edit(self):
        root=self.task('first',mode='worktree')
        (root/'a.py').write_text('first implementation\n');self.service.checkpoint('first')
        changes=self.service.changes('first');self.service.apply('first',changes['revision'],['a.py'])
        self.db.update_coding_task('first',status='COMPLETED')
        self.task('second',mode='worktree')
        (root/'a.py').write_text('refined implementation\n');self.service.checkpoint('second')
        changes=self.service.changes('second');self.service.apply('second',changes['revision'],['a.py'])
        self.assertEqual('refined implementation\n',(self.root/'a.py').read_text())
        (root/'a.py').write_text('later refinement\n');self.service.checkpoint('second')
        (self.root/'a.py').write_text('independent user edit\n')
        with self.assertRaisesRegex(WorkspaceError,'SOURCE_CHANGED_CONFLICT'):
            self.service.apply('second',self.service.changes('second')['revision'],['a.py'])
        self.assertEqual('independent user edit\n',(self.root/'a.py').read_text())

    def test_existing_branch_switch_is_reviewed_and_bound_to_its_commit(self):
        self.task();self.git('branch','existing')
        git=GitWorkspace(self.db)
        operation=git.prepare('c','switch_branch',branch='existing')
        self.assertEqual('main',self.git('branch','--show-current'))
        self.assertEqual('SUCCEEDED',git.execute(operation['id'],'c')['status'])
        self.assertEqual('existing',self.git('branch','--show-current'))
        self.git('switch','main');operation=git.prepare('c','switch_branch',branch='existing')
        alternate=self.git('commit-tree',self.git('rev-parse','HEAD^{tree}'),'-p',self.git('rev-parse','HEAD'),'-m','Alternate')
        self.git('update-ref','refs/heads/existing',alternate)
        result=git.execute(operation['id'],'c')
        self.assertEqual('STALE',result['status'])
        self.assertIn('TARGET_BRANCH_CHANGED',result['error'])


if __name__ == '__main__': unittest.main()

class PendingInstructionTests(unittest.TestCase):
    setUp = TaskReviewTests.setUp
    tearDown = TaskReviewTests.tearDown
    git = TaskReviewTests.git
    task = TaskReviewTests.task

    def pending(self):
        self.task()
        self.db.update_coding_task('task', status='RUNNING')
        self.service.steer('task', 'Original instruction', 0, queued=True, message_id='instruction')

    def test_edit_keeps_delivery_mode_and_updates_chat(self):
        self.pending()
        result = self.service.update_instruction('task', 'instruction', 'edit', 'Original instruction', 'WAITING', 'Changed instruction')
        self.assertEqual('WAITING', result['status'])
        self.assertEqual('Changed instruction', result['message'])
        self.assertEqual('Changed instruction', self.db.conversation('c')['messages'][-1]['content'])
        self.assertEqual([], self.service.take_steering('task'))

    def test_cancel_removes_prompt_from_future_context_and_never_claims_it(self):
        self.pending()
        self.service.update_instruction('task', 'instruction', 'cancel', 'Original instruction', 'WAITING')
        self.assertEqual('CANCELLED', self.service.steering('task')[0]['status'])
        self.assertEqual([], self.db.conversation('c')['messages'])
        self.assertEqual([], self.service.take_steering('task'))

    def test_send_now_promotes_exactly_one_instruction_without_duplicating_it(self):
        self.pending()
        self.service.steer('task', 'Still waiting', 0, queued=True, message_id='other')
        self.db.update_coding_task('task', plan_revision=2)
        result = self.service.update_instruction('task', 'instruction', 'send_now', 'Original instruction', 'WAITING')
        self.assertEqual('QUEUED', result['status'])
        self.assertEqual(2, result['received_revision'])
        self.assertEqual(['instruction'], [row['id'] for row in self.service.take_steering('task')])
        self.assertEqual('WAITING', next(row for row in self.service.steering('task') if row['id']=='other')['status'])
        self.assertEqual(2, len(self.db.conversation('c')['messages']))

    def test_failed_instruction_requires_explicit_retry(self):
        self.pending()
        self.service.take_steering('task')
        self.service.finish_steering([self.service.steering('task')[0]], 0, False)
        failed = self.service.steering('task')[0]
        self.assertEqual('NEEDS_REVIEW', failed['status'])
        self.assertEqual([], self.service.take_steering('task'))
        retried = self.service.update_instruction('task', 'instruction', 'retry',
            'Original instruction', 'NEEDS_REVIEW')
        self.assertEqual('QUEUED', retried['status'])
        self.assertEqual(['instruction'], [row['id'] for row in self.service.take_steering('task')])

    def test_runner_claim_and_concurrent_edits_are_conflicts(self):
        self.pending()
        self.service.update_instruction('task', 'instruction', 'edit', 'Original instruction', 'WAITING', 'Changed')
        with self.assertRaisesRegex(WorkspaceError, 'INSTRUCTION_CHANGED'):
            self.service.update_instruction('task', 'instruction', 'cancel', 'Original instruction', 'WAITING')
        self.service.update_instruction('task', 'instruction', 'send_now', 'Changed', 'WAITING')
        self.service.take_steering('task')
        for action in ['edit','cancel','send_now']:
            with self.subTest(action=action), self.assertRaisesRegex(WorkspaceError, 'INSTRUCTION_ALREADY_CLAIMED'):
                self.service.update_instruction('task', 'instruction', action, 'Changed', 'QUEUED', 'Too late')
        self.assertEqual('Changed', self.db.conversation('c')['messages'][-1]['content'])

    def test_http_controls_wake_only_for_one_off_promotion(self):
        self.pending()
        from types import SimpleNamespace
        wake = unittest.mock.Mock()
        app = FastAPI();app.include_router(TaskReviewAPI(SimpleNamespace(service=self.service), wake).router)
        client = TestClient(app)
        self.assertEqual(1, len(client.get('/api/coding-tasks/task/instructions').json()['steering']))
        result = client.patch('/api/coding-tasks/task/instructions/instruction', json={'action':'send_now','expected_message':'Original instruction','expected_status':'WAITING'})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual('QUEUED', result.json()['status']);wake.assert_called_once()
        stale = client.patch('/api/coding-tasks/task/instructions/instruction', json={'action':'cancel','expected_message':'Original instruction','expected_status':'WAITING'})
        self.assertEqual(409, stale.status_code)
