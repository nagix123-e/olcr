import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from olcr_api.db import Database
from olcr_api.git_api import GitIntegration
from olcr_api.git_workspace import GitError, GitWorkspace, coding_git_intent, git_intent, github_url


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'; self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.test')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'credential.helper', '')
        (self.root/'a.py').write_text('original a\n'); (self.root/'b.py').write_text('original b\n')
        self.git('add', '.'); self.git('commit', '-m', 'Initial')
        self.db = Database(Path(self.temp.name)/'db.sqlite'); self.db.initialize()
        self.db.create_project('fixture', str(self.root), time.time(), 'p')
        self.db.create_conversation('fixture', time.time(), 'c', 'p')
        self.service = GitWorkspace(self.db)

    def tearDown(self): self.temp.cleanup()

    def git(self, *args, root=None):
        proc = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root or self.root), *args],
                              env={**os.environ, 'GIT_TERMINAL_PROMPT':'0'}, capture_output=True, check=True)
        return proc.stdout.decode().strip()

    def change(self, path='a.py', content='new a\n'):
        (self.root/path).write_text(content)

    def commit_op(self, **kwargs):
        return self.service.prepare('c', 'commit', paths=kwargs.pop('paths', ['a.py']), message='Fix fixture', **kwargs)

    def test_autonomous_stage_and_named_commit_preserve_other_staged_changes(self):
        integration = GitIntegration(lambda:self.db, lambda:SimpleNamespace(external_access_enabled=False))
        integration.github.policy('autonomous')
        self.change();self.change('b.py','unrelated user edit\n');self.git('add','b.py')
        staged = integration.command('c','stage',json.dumps({'paths':['a.py']}))
        self.assertEqual('SUCCEEDED', staged['status'])
        self.assertEqual({'a.py','b.py'}, set(self.git('diff','--cached','--name-only').splitlines()))
        committed = integration.command('c','commit',json.dumps({'paths':['a.py'],'message':'Fix a'}))
        self.assertEqual('SUCCEEDED',committed['status'])
        self.assertEqual('b.py',self.git('diff','--cached','--name-only'))
        self.assertEqual('original b',self.git('show','HEAD:b.py'))

    def test_connected_identity_is_used_only_when_repository_identity_is_missing(self):
        integration=GitIntegration(lambda:self.db,lambda:SimpleNamespace(external_access_enabled=False))
        integration.github.preference('account','fixture-user');integration.github.policy('autonomous')
        self.change();service=integration.service;original=service._run
        def missing_identity(root,args,**kwargs):
            if args in (['config','--get','user.name'],['config','--get','user.email']):return ''
            return original(root,args,**kwargs)
        with patch.object(service,'_run',side_effect=missing_identity):
            operation=integration.command('c','commit',json.dumps({'paths':['a.py']}))
        self.assertEqual('SUCCEEDED',operation['status'])
        self.assertEqual('fixture-user <fixture-user@users.noreply.github.com>',self.git('log','-1','--format=%an <%ae>'))
        self.assertEqual('Fixture',self.git('config','user.name'))

    def test_autonomous_commit_push_reaches_a_local_bare_remote_without_second_review(self):
        bare=self.bare_remote();self.change()
        integration=GitIntegration(lambda:self.db, lambda:SimpleNamespace(external_access_enabled=True))
        integration.github.policy('autonomous')
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            completed=integration.command('c','commit_push','')
        self.assertEqual('SUCCEEDED',completed['status'])
        self.assertEqual('SUCCEEDED',completed['following_operation']['status'])
        self.assertEqual(self.git('rev-parse','HEAD'),self.git('rev-parse','refs/heads/main',root=bare))

    def test_explicit_git_commands_and_autonomous_negative_instructions(self):
        from olcr_api.commands import workspace_command
        self.assertEqual('/git stage', workspace_command('変更をステージして'))
        self.assertEqual('/git commit', workspace_command('今の変更をコミットしておいて'))
        self.assertEqual('/git push', workspace_command('Please push my changes'))
        self.assertEqual('/git commit', workspace_command('コミットして'))
        self.assertEqual('/git commit_push', workspace_command('変更をコミットしてプッシュして'))
        self.assertEqual({'paths':['a.py']},json.loads(workspace_command('a.pyをコミットして').split(' ',2)[2]))
        self.assertEqual({'message':'Fix a bug'},json.loads(workspace_command('「Fix a bug」でコミットして').split(' ',2)[2]))
        self.assertIsNone(coding_git_intent('修正して。Git操作はしない', True))
        self.assertEqual({'commit':True,'push':False},coding_git_intent('修正して。プッシュしない', True))
        self.assertIsNone(coding_git_intent('修正して。コミットしない', True))
        self.assertTrue(coding_git_intent('Fix the bug', True)['commit'])

    def test_review_has_no_mutation_and_commit_preserves_other_staged_changes(self):
        self.change(); self.change('b.py', 'other staged\n'); self.git('add', 'b.py')
        staged = self.git('diff', '--cached')
        old_head = self.git('rev-parse', 'HEAD')
        op = self.commit_op()
        self.assertEqual(old_head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(staged, self.git('diff', '--cached'))
        self.assertIn('+new a', op['diff']); self.assertNotIn('other staged', op['diff'])
        result = self.service.execute(op['id'], 'c')
        self.assertEqual('SUCCEEDED', result['status'])
        self.assertEqual('a.py', self.git('show', '--format=', '--name-only', 'HEAD'))
        self.assertEqual(staged, self.git('diff', '--cached'))
        self.assertEqual('other staged\n', (self.root/'b.py').read_text())
        with self.assertRaises(GitError): self.service.execute(op['id'], 'c')

    def test_new_deleted_and_literal_pathspec_files(self):
        self.change(':literal*.py', 'literal\n'); (self.root/'a.py').unlink()
        op = self.commit_op(paths=[':literal*.py', 'a.py'])
        self.assertIn('deleted file', op['diff'])
        self.assertEqual('SUCCEEDED', self.service.execute(op['id'], 'c')['status'])
        self.assertEqual('literal', self.git('show', 'HEAD::literal*.py'))
        self.assertNotIn('a.py', self.git('ls-tree', '--name-only', 'HEAD'))

    def test_initial_commit_without_head(self):
        empty=Path(self.temp.name)/'empty';empty.mkdir()
        self.git('init', '-b', 'main', root=empty)
        for key,value in [('user.name','Fixture'),('user.email','fixture@example.test'),('commit.gpgsign','false')]:
            self.git('config',key,value,root=empty)
        self.db.create_project('empty',str(empty),time.time(),'p2');self.db.create_conversation('empty',time.time(),'c2','p2')
        (empty/'new.txt').write_text('first\n')
        op=self.service.prepare('c2','commit',paths=['new.txt'],message='Initial commit')
        self.assertEqual('SUCCEEDED',self.service.execute(op['id'],'c2')['status'])

    def test_changed_worktree_index_and_branch_invalidate_approval(self):
        self.change(); op=self.commit_op(); self.change(content='changed after review\n')
        self.assertEqual('STALE',self.service.execute(op['id'],'c')['status'])
        op=self.commit_op();self.change('b.py','staged after review\n');self.git('add','b.py')
        self.assertEqual('STALE',self.service.execute(op['id'],'c')['status'])
        op=self.commit_op();self.git('switch','-c','other')
        self.assertEqual('STALE',self.service.execute(op['id'],'c')['status'])

    def test_scope_conflicts_and_detached_head(self):
        self.change()
        for path in ['../outside', '/tmp/outside', '.git/config', '.', 'missing.py']:
            with self.assertRaises(GitError): self.commit_op(paths=[path])
        (self.root/'link').symlink_to(self.temp.name)
        with self.assertRaises(GitError): self.commit_op(paths=['link/db.sqlite'])
        self.git('checkout','--detach')
        with self.assertRaises(GitError): self.commit_op()

    def test_workspace_subdirectory_and_projectless_are_rejected(self):
        sub=self.root/'sub';sub.mkdir();self.db.create_project('sub',str(sub),time.time(),'sub')
        self.db.create_conversation('sub',time.time(),'sub','sub');self.db.create_conversation('none',time.time(),'none')
        for conversation in ('sub','none'):
            with self.assertRaises(GitError): self.service.status(conversation)

    def test_declined_expired_and_interrupted_are_not_replayed(self):
        self.change();op=self.commit_op();self.service.reject(op['id'],'c')
        with self.assertRaises(GitError):self.service.execute(op['id'],'c')
        op=self.commit_op();op['created_at']=time.time()-3700;self.service._save(op)
        with self.assertRaises(GitError):self.service.execute(op['id'],'c')
        op=self.commit_op();op['status']='RUNNING';self.service._save(op)
        restarted=GitWorkspace(self.db)
        self.assertEqual('INTERRUPTED',restarted.operation(op['id'],'c')['status'])

    def test_connection_status_does_not_probe_network_or_expose_tokens(self):
        self.git('remote','add','origin','https://secret:password@github.com/example/repo.git')
        value=self.service.status('c')
        self.assertFalse(value['remotes'][0]['github'])
        self.assertNotIn('password',json.dumps(value))
        for url in ['https://github.com/owner/repo.git','git@github.com:owner/repo.git','ssh://git@github.com/owner/repo.git']:
            self.assertEqual(url,github_url(url))
        for url in ['https://github.com.evil.test/owner/repo','/tmp/repo','https://key@github.com/owner/repo','ext::command']:
            with self.assertRaises(GitError):github_url(url)

    def test_backend_provider_secrets_and_git_injection_environment_not_inherited(self):
        with patch.dict(os.environ,{'OLCR_GITHUB_TOKEN':'private','OPENAI_API_KEY':'private','GIT_DIR':'/evil','GIT_CONFIG_COUNT':'1'}):
            env=self.service._env()
            self.assertNotIn('OLCR_GITHUB_TOKEN',env);self.assertNotIn('OPENAI_API_KEY',env)
            self.assertNotIn('GIT_DIR',env);self.assertNotIn('GIT_CONFIG_COUNT',env)

    def bare_remote(self):
        bare=Path(self.temp.name)/'remote.git';bare.mkdir();self.git('init','--bare',root=bare)
        self.git('remote','add','origin',str(bare));return bare

    def test_push_requires_network_gate_and_review_then_pins_commit(self):
        bare=self.bare_remote()
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            with patch.object(self.service,'_remote_head') as probe:
                with self.assertRaises(GitError):self.service.prepare('c','push')
                probe.assert_not_called()
            op=self.service.prepare('c','push',external_access=True)
            with self.assertRaises(GitError):self.service.execute(op['id'],'c',external_access=False)
            result=self.service.execute(op['id'],'c',external_access=True)
            self.assertEqual('SUCCEEDED',result['status'])
            self.assertEqual(op['head'],self.git('rev-parse','refs/heads/main',root=bare))
            with self.assertRaises(GitError):self.service.execute(op['id'],'c',external_access=True)

    def test_remote_destination_and_remote_branch_changes_invalidate_review(self):
        bare=self.bare_remote()
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            op=self.service.prepare('c','push',external_access=True)
            self.git('remote','set-url','origin',str(Path(self.temp.name)/'other.git'))
            self.assertEqual('STALE',self.service.execute(op['id'],'c',external_access=True)['status'])
            self.git('remote','set-url','origin',str(bare))
            op=self.service.prepare('c','push',external_access=True)
            self.git('push','origin','main')
            self.assertEqual('STALE',self.service.execute(op['id'],'c',external_access=True)['status'])

    def test_non_fast_forward_is_reported_without_forcing_or_retry(self):
        bare=self.bare_remote();self.git('push','origin','main')
        old=self.git('rev-parse','HEAD');self.change();self.git('add','a.py');self.git('commit','-m','Remote update')
        self.git('push','origin','main');remote_head=self.git('rev-parse','HEAD')
        self.git('checkout','-b','different',old);self.change(content='different\n');self.git('add','a.py');self.git('commit','-m','Divergent')
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            op=self.service.prepare('c','push',branch='main',external_access=True)
            result=self.service.execute(op['id'],'c',external_access=True)
            self.assertEqual('FAILED',result['status'])
            self.assertEqual(remote_head,self.git('rev-parse','refs/heads/main',root=bare))

    def test_api_approval_is_bound_to_conversation_and_failures_are_visible(self):
        settings=SimpleNamespace(external_access_enabled=False)
        integration=GitIntegration(lambda:self.db,lambda:settings)
        app=FastAPI();app.include_router(integration.router)
        self.change()
        with TestClient(app) as client:
            op=client.post('/api/conversations/c/git/prepare',json={'action':'commit','paths':['a.py'],'message':'Fix'}).json()
            self.db.create_conversation('other',time.time(),'other','p')
            self.assertEqual(409,client.post(f"/api/conversations/other/git/operations/{op['id']}/approve").status_code)
            self.assertEqual('SUCCEEDED',client.post(f"/api/conversations/c/git/operations/{op['id']}/approve").json()['status'])
            self.assertEqual(409,client.post(f"/api/conversations/c/git/operations/{op['id']}/approve").status_code)

    def test_coding_handoff_excludes_preexisting_dirty_files_and_stays_pending(self):
        integration=GitIntegration(lambda:self.db,lambda:SimpleNamespace(external_access_enabled=False))
        self.change('b.py','user draft\n')
        plan={'file_manifest':[{'path':'a.py'},{'path':'b.py'}]}
        task=self.db.create_coding_task('task','c','Fix the bug and commit changes','QUEUED','NONE',plan,time.time())
        integration.capture_task_baseline(task)
        self.change()
        self.db.add_coding_phase_report('task','p1',1,{'changed_files':['a.py','b.py']},'PASS',time.time())
        task=self.db.update_coding_task('task',status='COMPLETED')
        integration.completed_task(task)
        operations=integration.service.operations('c')
        self.assertEqual(1,len(operations));self.assertEqual(['a.py'],operations[0]['paths'])
        self.assertEqual('PENDING',operations[0]['status'])
        integration.completed_task(task);self.assertEqual(1,len(integration.service.operations('c')))

    def test_autonomous_task_handoff_commits_only_task_owned_changes(self):
        integration=GitIntegration(lambda:self.db,lambda:SimpleNamespace(external_access_enabled=False))
        integration.github.policy('autonomous')
        self.change('b.py','user draft\n')
        task=self.db.create_coding_task('auto-task','c','Fix the bug. Do not push.','QUEUED','NONE',
            {'file_manifest':[{'path':'a.py'},{'path':'b.py'}]},time.time())
        integration.capture_task_baseline(task)
        self.change()
        self.db.add_coding_phase_report('auto-task','p1',1,{'changed_files':['a.py','b.py']},'PASS',time.time())
        task=self.db.update_coding_task('auto-task',status='COMPLETED')
        integration.completed_task(task)
        operations=integration.service.operations('c')
        self.assertEqual(1,len(operations));self.assertEqual(['a.py'],operations[0]['paths'])
        self.assertEqual('SUCCEEDED',operations[0]['status'])
        self.assertEqual('original b',self.git('show','HEAD:b.py'))
        integration.completed_task(task);self.assertEqual(1,len(integration.service.operations('c')))

    def test_intents_do_not_hijack_implementation_or_analysis(self):
        self.assertEqual({'action':'push'},git_intent('GitHubにプッシュして'))
        self.assertIsNone(git_intent('GitHubへのプッシュ機能を実装して'))
        self.assertIsNone(coding_git_intent('コミットしない'))
        self.assertIsNone(coding_git_intent('Explain commit behavior'))
        self.assertIsNone(coding_git_intent('Add GitHub commit and push support'))
        self.assertEqual({'commit':True,'push':True},coding_git_intent('修正し、コミットしてプッシュして'))
        self.assertEqual({'commit':True,'push':False},coding_git_intent('commit changes, do not push'))

    def test_commit_uses_reviewed_tree_if_worktree_changes_during_execution(self):
        self.change();op=self.commit_op();original=self.service._run
        def change_before_commit(root,args,**kwargs):
            if args[0]=='commit':self.change(content='late edit\n')
            return original(root,args,**kwargs)
        with patch.object(self.service,'_run',side_effect=change_before_commit):
            result=self.service.execute(op['id'],'c')
        self.assertEqual('SUCCEEDED',result['status'])
        self.assertEqual('new a',self.git('show','HEAD:a.py'))
        self.assertEqual('late edit\n',(self.root/'a.py').read_text())

    def test_concurrent_approval_commits_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.change();op=self.commit_op()
        def execute():
            try:return self.service.execute(op['id'],'c')['status']
            except GitError:return 'REJECTED_DUPLICATE'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:execute(),range(2)))
        self.assertEqual(['REJECTED_DUPLICATE','SUCCEEDED'],sorted(results))
        self.assertEqual('2',self.git('rev-list','--count','HEAD'))

    def test_active_coding_blocks_git_mutation(self):
        self.change();op=self.commit_op()
        self.db.create_coding_task('active','c','Fix a bug','RUNNING','IMPLEMENTING',None,time.time())
        with self.assertRaises(GitError):self.commit_op()
        with self.assertRaises(GitError):self.service.execute(op['id'],'c')

    def test_commit_then_push_requires_a_second_review(self):
        bare=self.bare_remote();self.change()
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            op=self.commit_op(push_after=True)
            result=self.service.execute(op['id'],'c',external_access=True)
            self.assertEqual('SUCCEEDED',result['status'])
            following=self.service.operation(result['next_operation_id'],'c')
            self.assertEqual('PENDING',following['status']);self.assertEqual('push',following['action'])
            self.assertEqual('',self.service._remote_head(self.root,str(bare),'main'))
            pushed=self.service.execute(following['id'],'c',external_access=True)
            self.assertEqual(result['commit'],pushed['pushed_commit'])

    def test_failure_after_commit_retains_sha_and_prevents_duplicate_commit(self):
        self.change();op=self.commit_op();original=self.service._run
        def fail_reconcile(root,args,**kwargs):
            if args[0]=='reset':raise GitError('FIXTURE_INDEX_LOCKED')
            return original(root,args,**kwargs)
        with patch.object(self.service,'_run',side_effect=fail_reconcile):
            result=self.service.execute(op['id'],'c')
        self.assertEqual('SUCCEEDED_WITH_WARNING',result['status'])
        self.assertEqual(self.git('rev-parse','HEAD'),result['commit'])
        with self.assertRaises(GitError):self.service.execute(op['id'],'c')

    def test_disabling_external_access_during_remote_check_prevents_push(self):
        bare=self.bare_remote();enabled={'value':True}
        with patch('olcr_api.git_workspace.github_url',lambda url:url):
            op=self.service.prepare('c','push',external_access=True)
            original=self.service._remote_head
            def turn_off(*args):
                result=original(*args);enabled['value']=False;return result
            with patch.object(self.service,'_remote_head',side_effect=turn_off):
                result=self.service.execute(op['id'],'c',external_access=lambda:enabled['value'])
            self.assertEqual('FAILED',result['status']);self.assertEqual('EXTERNAL_ACCESS_REQUIRED',result['error'])
            self.assertEqual('',self.service._remote_head(self.root,str(bare),'main'))


if __name__=='__main__':unittest.main()
