"""Permission choices preserve default routes and lift approval gates in full mode."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.auth import AuthorizationPolicy
from olcr_api.config import Settings
from olcr_api.models import Risk
from olcr_api.permissions import RequestApprovals, capability_enabled, requires_request_approval
from tests import test_core as core_tests


class PermissionPolicyTests(unittest.TestCase):
    def test_full_allows_every_risk_even_if_not_explicitly_approved(self):
        policy = AuthorizationPolicy(lambda: 'full')
        for risk in Risk:
            self.assertTrue(policy.decide(risk, False).allowed)

    def test_automatic_policy_is_unchanged(self):
        policy = AuthorizationPolicy()
        self.assertTrue(policy.decide(Risk.SAFE).allowed)
        self.assertFalse(policy.decide(Risk.CONFIRM).allowed)
        self.assertTrue(policy.decide(Risk.CONFIRM, True).allowed)
        self.assertFalse(policy.decide(Risk.DENY_DEFAULT, True).allowed)

    def test_full_overrides_access_switches_without_overwriting_them(self):
        settings = Settings(external_access_enabled=False, computer_use_enabled=False)
        for mode in ('confirm', 'automatic', 'full', 'automatic'):
            settings.permission_level = mode
            self.assertEqual(mode == 'full', capability_enabled(settings, 'external_access_enabled'))
            self.assertEqual(mode == 'full', capability_enabled(settings, 'computer_use_enabled'))
        self.assertFalse(settings.external_access_enabled)

    def test_invalid_mode_is_rejected_and_old_settings_default_to_automatic(self):
        self.assertEqual('automatic', Settings().with_overrides({}).permission_level)
        for value in ('custom', '', None, True):
            with self.assertRaises(ValueError):
                Settings().with_overrides({'permission_level': value})

    def test_approval_is_exact_single_use_and_expires(self):
        grants = RequestApprovals()
        fingerprint = grants.fingerprint('POST', '/api/chat', b'{"message":"one"}')
        token = grants.issue(fingerprint)
        self.assertFalse(grants.consume(token, fingerprint))
        self.assertTrue(grants.approve(token, True))
        self.assertFalse(grants.consume(token, grants.fingerprint('POST', '/api/chat', b'{"message":"two"}')))
        self.assertTrue(grants.consume(token, fingerprint))
        self.assertFalse(grants.consume(token, fingerprint))
        token = grants.issue(fingerprint)
        with patch('olcr_api.permissions.time.monotonic', return_value=time.monotonic() + 301):
            self.assertFalse(grants.approve(token, True))

    def test_decline_and_mode_switch_revoke_grants(self):
        grants = RequestApprovals()
        token = grants.issue('first')
        grants.approve(token, False)
        self.assertFalse(grants.consume(token, 'first'))
        token = grants.issue('second')
        grants.approve(token, True)
        grants.clear()
        self.assertFalse(grants.consume(token, 'second'))

    def test_stop_polling_configuration_and_verification_remain_available(self):
        for method, path in [('POST', '/api/tasks/x/cancel'), ('POST','/api/computer/actions/type/verify'),
                             ('PUT','/api/permissions'), ('GET','/api/chat'), ('POST','/api/web/scrape/runs/x/stop')]:
            self.assertFalse(requires_request_approval(method, path))
        for path in ('/api/chat', '/api/chat/stream', '/api/web/fetch', '/api/conversations/x/git/prepare'):
            self.assertTrue(requires_request_approval('POST', path))


class FullAccessRuntimeTests(unittest.TestCase):
    setUp = core_tests.CoreTests.setUp
    tearDown = core_tests.CoreTests.tearDown

    def test_full_writes_outside_roots_without_pending_confirmation(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'outside.txt'
            self.runtime.settings.permission_level = 'full'
            task, response = self.runtime.execute(f'write file {target}: approved by full access')
            self.assertEqual('completed', task.state.value)
            self.assertEqual('approved by full access', target.read_text())
            self.assertNotIn('confirmation_required', response)
            self.assertEqual([], self.model.calls)
            self.runtime.settings.permission_level = 'automatic'
            with self.assertRaises(PermissionError):
                self.runtime._permission_path(str(target))

    def test_automatic_still_requires_exact_write_confirmation(self):
        target = self.root / 'normal.txt'
        task, response = self.runtime.execute(f'write file {target}: original path')
        self.assertEqual('waiting_for_confirmation', task.state.value)
        self.assertFalse(target.exists())
        confirmation = json.loads(response)
        self.runtime.resolve_confirmation(task.id, confirmation['action_id'], True)
        self.assertEqual('original path', target.read_text())

    def test_full_removes_keyword_deny_without_inventing_a_shell_executor(self):
        self.runtime.settings.permission_level = 'full'
        task, _ = self.runtime.execute('Explain sudo credentials and system configuration')
        self.assertNotEqual('deny_default_operation', task.reason_category)
        self.assertNotEqual('denied', task.state.value)


class PermissionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        cls.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'OLCR_DB_PATH': str(Path(cls.tmp.name) / 'api.db'),
                                     'OLCR_ALLOWED_ROOTS': cls.tmp.name, 'OLCR_VECTOR_ENABLED':'false'}):
            import olcr_api.app as api
        cls.api = api

    def setUp(self):
        from fastapi.testclient import TestClient
        from olcr_api.db import Database
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = self.api.db
        self.old_mode = self.api.settings.permission_level
        self.api.db = Database(str(Path(self.temp.name) / 'api.db'))
        self.api.db.initialize()
        self.client = TestClient(self.api.app)
        self.client.put('/api/permissions', json={'level':'automatic'})

    def tearDown(self):
        self.client.put('/api/permissions', json={'level':self.old_mode})
        self.api.db = self.old_db
        self.temp.cleanup()

    def test_persists_without_replacing_runtime_and_rejects_invalid_choice(self):
        original = self.api.runtime
        result = self.client.put('/api/permissions', json={'level':'full'})
        self.assertEqual({'level':'full'}, result.json())
        self.assertIs(original, self.api.runtime)
        self.assertEqual('full', self.api.db.load_application_settings()['permission_level'])
        self.assertEqual('full', self.api.runtime.settings.permission_level)
        self.assertEqual(422, self.client.put('/api/permissions',json={'level':'custom'}).status_code)
        self.assertEqual({'level':'full'},self.client.get('/api/permissions').json())

    def test_confirm_chat_requires_single_use_grant_before_dispatch(self):
        self.client.put('/api/permissions',json={'level':'confirm'})
        payload = {'message':'calculate 2+3'}
        with patch.object(self.api, 'chat', wraps=self.api.chat):
            denied = self.client.post('/api/chat',json=payload)
        self.assertEqual(409,denied.status_code)
        token = denied.json()['detail']['approval_id']
        self.assertEqual(200,self.client.post('/api/permissions/approvals/'+token,json={'approve':True}).status_code)
        # Invalid body is deliberately used to prove grants cannot authorize a different request.
        changed = self.client.post('/api/chat',json={'message':'calculate 9+9'},headers={'X-OLCR-Approval':token})
        self.assertEqual(409,changed.status_code)
        # Use a non-executing fake route dependency after permission approval: real chat routing remains intact.
        with patch.object(self.api.runtime, 'execute', wraps=self.api.runtime.execute):
            accepted = self.client.post('/api/chat',json=payload,headers={'X-OLCR-Approval':token})
        self.assertEqual(200,accepted.status_code,accepted.text)
        self.assertEqual(409,self.client.post('/api/chat',json=payload,headers={'X-OLCR-Approval':token}).status_code)

    def test_confirm_stream_challenge_happens_before_creating_a_conversation(self):
        self.client.put('/api/permissions',json={'level':'confirm'})
        payload={'message':'calculate 6*7'}
        denied=self.client.post('/api/chat/stream',json=payload)
        self.assertEqual(409,denied.status_code)
        token=denied.json()['detail']['approval_id']
        self.client.post('/api/permissions/approvals/'+token,json={'approve':True})
        accepted=self.client.post('/api/chat/stream',json=payload,headers={'X-OLCR-Approval':token})
        self.assertEqual(200,accepted.status_code,accepted.text)
        self.assertIn('data: ',accepted.text)
        self.assertIn('42',accepted.text)

    def test_full_releases_only_permission_scope_waits_and_preserves_pause(self):
        from tests.test_coding_tasks import plan
        api=self.api
        api.db.create_conversation('Wait',time.time(),'wait-conversation',None)
        for task_id,goal in [('scope-task','repoを修正してください'),('human-plan','計画だけ作成して、承認まで実装しないで')]:
            pending=plan(goal)
            api.db.create_coding_task(task_id,'wait-conversation',goal,'WAITING_FOR_USER','SCOPE_AUTHORIZATION',None,time.time())
            api.db.update_coding_task(task_id,pending_plan=pending,pending_authorization={'source':'plan','requested_scope':['git commit']},pending_user_confirmation=1)
        with patch.object(api._coding_scheduler_wake,'set'):
            self.client.put('/api/permissions',json={'level':'full'})
        self.assertEqual('QUEUED',api.db.coding_task('scope-task')['status'])
        self.assertFalse(api.db.coding_task('scope-task')['pending_authorization'])
        self.assertEqual('WAITING_FOR_USER',api.db.coding_task('human-plan')['status'])

    def test_switching_to_confirm_pauses_next_phase_through_existing_scope_route(self):
        from tests.test_coding_tasks import plan
        api=self.api
        api.db.create_conversation('Mode switch',time.time(),'switch-conversation',None)
        task=api.db.create_coding_task('switch-task','switch-conversation','fix source','QUEUED','NONE',plan('fix source'),time.time())
        self.client.put('/api/permissions',json={'level':'confirm'})
        self.assertIsNotNone(api._coding_permission_wait(task))
        waiting=api.db.coding_task(task['id'])
        self.assertEqual('SCOPE_AUTHORIZATION',waiting['activity'])
        with patch.object(api._coding_scheduler_wake,'set'):
            api.chat(api.ChatInput(message='承認します',conversation_id='switch-conversation'))
        approved=api.db.coding_task(task['id'])
        self.assertIsNone(api._coding_permission_wait(approved))

    def test_confirm_challenge_keeps_cors_for_the_development_frontend(self):
        self.client.put('/api/permissions',json={'level':'confirm'})
        response=self.client.post('/api/chat',json={'message':'hello'},headers={'Origin':'http://127.0.0.1:5173'})
        self.assertEqual(409,response.status_code)
        self.assertEqual('http://127.0.0.1:5173',response.headers.get('access-control-allow-origin'))
        preflight=self.client.options('/api/chat',headers={'Origin':'http://127.0.0.1:5173',
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type,x-olcr-session,x-olcr-approval'})
        self.assertEqual(200,preflight.status_code)

    def test_full_has_no_http_permission_gate(self):
        self.client.put('/api/permissions',json={'level':'full'})
        result = self.client.post('/api/chat',json={'message':'calculate 6*7'})
        self.assertEqual(200,result.status_code,result.text)
        self.assertNotIn('PERMISSION_CONFIRMATION_REQUIRED',result.text)

class PermissionCodingIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_coding_tasks import CodingTaskApiTests
        CodingTaskApiTests.setUp(self)

    def tearDown(self):
        from tests.test_coding_tasks import CodingTaskApiTests
        CodingTaskApiTests.tearDown(self)

    def _run_plan(self, mode, goal='repoを修正してください', protected=True):
        import olcr_api.app as api
        from tests.test_coding_tasks import plan
        api.settings.permission_level = mode
        api.db.create_conversation('Permissions',time.time(),'permissions-conversation',self.project)
        task_plan = plan(goal)
        if protected:
            task_plan['scope']['allowed']=['frontend','git commit']
        task = api.db.create_coding_task('permissions-task','permissions-conversation',goal,'QUEUED','QWEN_PLANNING',None,time.time())
        with patch.object(api,'_generate_plan',return_value=(task_plan,'{}')), patch.object(api._coding_scheduler_wake,'set'):
            api._run_coding_planning(task)
        return api.db.coding_task(task['id'])

    def test_full_protected_plan_queues_without_scope_approval(self):
        task = self._run_plan('full')
        self.assertEqual('QUEUED',task['status'])
        self.assertFalse(task['pending_authorization'])

    def test_automatic_protected_plan_uses_existing_approval_path(self):
        task = self._run_plan('automatic')
        self.assertEqual('WAITING_FOR_USER',task['status'])
        self.assertEqual(['git commit'],task['pending_authorization']['requested_scope'])

    def test_confirm_plans_wait_before_workspace_mutation(self):
        task = self._run_plan('confirm',protected=False)
        self.assertEqual('WAITING_FOR_PLAN_APPROVAL',task['status'])
        self.assertFalse((Path(self.tmp.name)/'frontend').exists())

    def test_full_still_honors_explicit_user_plan_only_instruction(self):
        task = self._run_plan('full',goal='計画だけ作成して、承認するまで実装しないで',protected=False)
        self.assertEqual('WAITING_FOR_PLAN_APPROVAL',task['status'])

class FullImplementationScopeTests(unittest.TestCase):
    def test_full_implementation_can_write_outside_workspace_via_same_executor(self):
        from tests.test_runtime_managed_context import RealRuntimeManagedContextTests, OutsideRootFileModel
        fixture = RealRuntimeManagedContextTests()
        fixture.setUp()
        try:
            fixture.runtime.settings.permission_level='full'
            fixture.runtime.model=OutsideRootFileModel()
            task, _ = fixture.runtime.execute('Implement a file update', workspace_root=str(fixture.workspace),
                                              managed_context=fixture.managed_context)
            self.assertEqual('completed',task.state.value)
            self.assertEqual('must not write',fixture.sentinel.read_text())
        finally:
            fixture.tearDown()

class FullGitPermissionTests(unittest.TestCase):
    def test_full_uses_existing_git_executor_without_manual_approval(self):
        from olcr_api.git_api import GitIntegration
        settings=Settings(permission_level='full',external_access_enabled=False)
        integration=GitIntegration(lambda:None,lambda:settings)
        operation={'id':'git-op','status':'PENDING'}
        with patch.object(integration.github,'automatic',return_value=False), patch.object(integration,'approve',return_value={'id':'git-op','status':'COMPLETED'}) as approve:
            result=integration.finish('conversation',operation)
            approve.assert_called_once_with('conversation','git-op')
            self.assertEqual('COMPLETED',result['status'])
        self.assertTrue(integration.network_access())
        settings.permission_level='automatic'
        with patch.object(integration.github,'automatic',return_value=False),patch.object(integration,'approve') as approve:
            self.assertEqual(operation,integration.finish('conversation',operation))
            approve.assert_not_called()

    def test_confirm_overrides_existing_autonomous_git_preference(self):
        from olcr_api.git_api import GitIntegration
        integration=GitIntegration(lambda:None,lambda:Settings(permission_level='confirm'))
        operation={'id':'git-op','status':'PENDING'}
        with patch.object(integration.github,'automatic',return_value=True),patch.object(integration,'approve') as approve:
            self.assertEqual(operation,integration.finish('conversation',operation))
            approve.assert_not_called()

class NativePermissionTests(unittest.TestCase):
    def test_full_lifts_sensitive_action_confirmation_but_preserves_grounding(self):
        from olcr_api.native_goal import validate_goal_action
        observation={'ax':{'permissionGranted':True,'applicationPid':42,'applicationBundleId':'example.app',
            'observedAt':time.time(),'observationId':'ax1','sessionId':'s1','confidence':1,'text':['Ready'],
            'elements':[{'id':'button1','role':'AXButton','label':'Delete','visible':True,'enabled':True,
                         'supportedActions':['AXPress'],'frame':{'x':1,'y':1,'width':30,'height':20}}]}}
        plan={'decision':'execute','kind':'press_button','target_label':'Delete','expected_text':'Deleted'}
        with self.assertRaisesRegex(ValueError,'CONFIRMATION_REQUIRED'):
            validate_goal_action(plan,'Delete the selected item',observation)
        self.assertEqual('execute',validate_goal_action(plan,'Delete the selected item',observation,allow_sensitive=True)['decision'])
        observation['ax']['observedAt']-=31
        with self.assertRaisesRegex(ValueError,'STALE'):
            validate_goal_action(plan,'Delete the selected item',observation,allow_sensitive=True)
