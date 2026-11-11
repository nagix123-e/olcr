"""Chat controls stay attached to durable tasks, including empty projects."""
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.coding_tasks import explicitly_requests_plan_only, normalize_coding_requirements
from olcr_api.task_workspace import WorkspaceError
from olcr_api.git_workspace import GitError
if __package__:
    from .test_local_coding_workflow import LocalCodingWorkflowTests
else:
    from test_local_coding_workflow import LocalCodingWorkflowTests


class ChatFirstCodingTests(LocalCodingWorkflowTests):
    def empty_task(self, identifier='empty'):
        root=Path(self.temporary.name)/identifier;root.mkdir()
        api.db.update_project('p', None, str(root), None, time.time())
        task=self.task(identifier, 'worktree', planning=True)
        return root,task

    def control(self,task,message,mode='queue',message_id='instruction'):
        return api._managed_coding_chat(api.ChatInput(conversation_id='c', message=message,
            coding_task_id=task['id'],coding_followup=mode),'c',message_id)

    def test_lunch_prompt_requires_plan_approval(self):
        self.assertTrue(explicitly_requests_plan_only('HTML・CSS・JavaScriptで昼食ルーレットを作成。まず実装計画を作成し、承認後に進めてください。'))
        self.assertFalse(explicitly_requests_plan_only('計画を作成し、承認不要で最後まで実装してください。'))

    def test_empty_worktree_stops_before_planner_with_actionable_setup(self):
        root,task=self.empty_task()
        with patch.object(api,'_run_coding_planning',side_effect=AssertionError('must not plan')):
            api._coding_job(task)
        saved=api.db.coding_task(task['id'])
        self.assertEqual('WAITING_FOR_USER',saved['status'])
        self.assertEqual('WORKSPACE_SETUP',saved['pending_authorization']['type'])
        self.assertEqual([],list(root.iterdir()))
        result=self.control(task,'承認します')
        self.assertIn('作業場所',result['response'])
        self.assertEqual('WAITING_FOR_USER',api.db.coding_task(task['id'])['status'])

    def test_direct_setup_preserves_task_and_does_not_initialize_git(self):
        root,task=self.empty_task();api._coding_workspace_ready(task)
        with patch.object(api._coding_scheduler_wake,'set'):
            result=self.control(task,'現在のWorkspaceで進めて')
        self.assertEqual(task['id'],result['coding_task_id'])
        self.assertEqual('QUEUED',api.db.coding_task(task['id'])['status'])
        self.assertEqual('direct',api.task_workspaces.service.requested_mode(task['id']))
        self.assertEqual([],list(root.iterdir()))
        api.task_workspaces.service.prepare(task['id'])
        self.assertFalse((root/'.git').exists())

    def test_chat_workspace_setup_failure_preserves_task_and_returns_the_error(self):
        root,task=self.empty_task();api._coding_workspace_ready(task)
        for failure in (WorkspaceError('WORKTREE_REQUIRES_INITIAL_COMMIT'),
                        GitError('GIT_SETUP_FAILED'),OSError('setup unavailable')):
            with self.subTest(error=type(failure).__name__), patch.object(
                    api.task_workspaces.service,'setup',side_effect=failure):
                result=self.control(task,'Gitを初期化して進めて')
                self.assertTrue(result['response'])
                self.assertEqual(task['id'],result['coding_task_id'])
                self.assertEqual('WAITING_FOR_USER',api.db.coding_task(task['id'])['status'])
        self.assertFalse((root/'.git').exists())

    def test_explicit_empty_git_setup_creates_only_empty_baseline(self):
        root,task=self.empty_task();api._coding_workspace_ready(task)
        api.task_workspaces.service.setup(task['id'],'initialize')
        files=subprocess.run(['git','-C',str(root),'ls-tree','-r','--name-only','HEAD'],check=True,capture_output=True).stdout
        self.assertEqual(b'',files)
        meta=api.task_workspaces.service.prepare(task['id'])
        self.assertNotEqual(str(root),meta['root'])
        self.assertEqual('worktree',meta['mode'])

    def test_git_setup_rejects_existing_files(self):
        root,task=self.empty_task();api._coding_workspace_ready(task);(root/'mine.txt').write_text('keep')
        with self.assertRaisesRegex(WorkspaceError,'EMPTY_FOLDER'):
            api.task_workspaces.service.setup(task['id'],'initialize')
        self.assertFalse((root/'.git').exists())
        self.assertEqual('keep',(root/'mine.txt').read_text())

    def test_chat_approval_targets_selected_persisted_plan(self):
        task=self.task('chosen');api.db.update_coding_task(task['id'],status='WAITING_FOR_PLAN_APPROVAL',requirements={'planning_only':True})
        self.task('other');api.db.update_coding_task('other',status='WAITING_FOR_PLAN_APPROVAL')
        with patch.object(api._coding_scheduler_wake,'set'):
            result=self.control(task,'この計画で進めて')
        self.assertEqual('chosen',result['coding_task_id'])
        self.assertEqual('QUEUED',api.db.coding_task('chosen')['status'])
        self.assertEqual('WAITING_FOR_PLAN_APPROVAL',api.db.coding_task('other')['status'])

    def test_plan_request_never_replies_with_normal_model_code(self):
        task=self.task('planned')
        result=self.control(task,'計画を出して')
        self.assertEqual('',result['response'])
        self.assertEqual([],api.db.conversation('c')['messages'])
        self.assertEqual('planned',result['coding_task_id'])
        self.assertEqual(1,len(api.db.coding_tasks('c')))

    def test_defined_task_queues_directly_without_a_plan_chat_session(self):
        if __package__:
            from .test_coding_tasks import plan
        else:
            from test_coding_tasks import plan
        task=self.task('defined',planning=True)
        with patch.object(api,'_generate_plan',return_value=(plan(task['original_goal']),'{}')),patch.object(api._coding_scheduler_wake,'set'):
            api._run_coding_planning(task)
        saved=api.db.coding_task(task['id'])
        self.assertEqual('LEAN',saved['requirements']['planning_policy']['mode'])
        self.assertEqual('QUEUED',saved['status'])
        self.assertEqual([],api.db.conversation('c')['messages'])

    def test_ambiguous_product_asks_for_definition_then_uses_the_chat_answer(self):
        if __package__:
            from .test_coding_tasks import plan
        else:
            from test_coding_tasks import plan
        goal='新規Webアプリを作成してください。'
        api.task_workspaces.service.preference('p','direct')
        task=api.db.create_coding_task('definition','c',goal,'QUEUED','QWEN_PLANNING',None,time.time())
        api.task_workspaces.service.reserve(task['id'])
        with patch.object(api,'_generate_plan',side_effect=AssertionError('clarify before model planning')):
            api._run_coding_planning(task)
        saved=api.db.coding_task(task['id'])
        self.assertEqual('WAITING_FOR_USER',saved['status'])
        self.assertEqual('REQUIREMENTS_CLARIFICATION',saved['pending_authorization']['type'])
        answer='ローカルのHTML・CSS・JavaScriptで、メモを入力し、一覧に表示して保存する。'
        with patch.object(api._coding_scheduler_wake,'set'):
            result=self.control(saved,answer)
        self.assertEqual('',result['response'])
        self.assertEqual(goal,api.db.coding_task(task['id'])['original_goal'])
        with patch.object(api,'_generate_plan',return_value=(plan(goal),'{}')) as planner,patch.object(api._coding_scheduler_wake,'set'):
            api._run_coding_planning(api.db.coding_task(task['id']))
        self.assertIn(answer,planner.call_args.args[2])
        self.assertEqual('QUEUED',api.db.coding_task(task['id'])['status'])
        self.assertEqual(1,len(api.db.conversation('c')['messages']))

    def test_generated_plan_repairs_collapsed_ui_logic_steps(self):
        if __package__:
            from .test_adaptive_planning import LUNCH
            from .test_coding_tasks import plan
        else:
            from test_adaptive_planning import LUNCH
            from test_coding_tasks import plan
        first=plan(LUNCH);first['phases']=first['phases'][:1]
        corrected=plan(LUNCH);corrected['phases']=corrected['phases'][:2]
        corrected['phases'][0]['goal']='入力と抽選結果の画面を実装'
        corrected['phases'][1]['goal']='抽選ロジックを実装'
        with patch.object(api,'_model_text',side_effect=[json.dumps(first),json.dumps(corrected)]) as model:
            result,_=api._generate_plan('split',LUNCH,'translate definition','QWEN_PLANNING',{})
        self.assertIsNotNone(result)
        self.assertEqual(2,len(result['phases']))
        self.assertEqual(2,model.call_count)

    def test_running_pause_waits_for_checkpoint_and_logs_progress_once(self):
        task=self.task('pause-log')
        api.db.update_coding_task(task['id'],status='RUNNING')
        result=api.update_coding_task(task['id'],api.CodingTaskUpdate(pause_requested=True))
        self.assertEqual('RUNNING',result['status'])
        self.assertEqual([],api.db.conversation('c')['messages'])
        self.assertTrue(api._pause_at_checkpoint(task['id']))
        self.assertTrue(api._pause_at_checkpoint(task['id']))
        self.assertEqual('RESUMABLE',api.db.coding_task(task['id'])['status'])
        messages=api.db.conversation('c')['messages']
        self.assertEqual(1,len(messages))
        self.assertIn('ユーザーの一時停止要求',messages[0]['content'])
        api._transition_resumable(task['id'],'PLANNING','RECOVERABLE_INTERNAL')
        self.assertEqual('PAUSED',api.db.coding_task(task['id'])['recovery_reason'])
        self.assertEqual(1,len(api.db.conversation('c')['messages']))
        api.db.update_coding_task(task['id'],pause_requested=False)
        api.db.enqueue_coding_task(task['id'])
        def pause_again(*args):
            api.db.update_coding_task(task['id'],pause_requested=True)
            api._pause_at_checkpoint(task['id'])
            return 'paused'
        with patch.object(api,'_run_managed_task',side_effect=pause_again):
            api.run_managed_task(task['id'],str(self.root))
        self.assertEqual(2,len(api.db.conversation('c')['messages']))

    def test_queue_waits_until_completed_then_dispatches_once_in_order(self):
        task=self.task('task');api.task_workspaces.service.prepare('task');api.db.update_coding_task('task',status='RUNNING')
        self.control(api.db.coding_task('task'),'赤色にする',message_id='first')
        self.control(api.db.coding_task('task'),'文字を大きくする',message_id='second')
        api._dispatch_coding_followups('task')
        self.assertEqual(1,len(api.db.coding_tasks('c')))
        self.assertEqual(['WAITING','WAITING'],[row['status'] for row in api.task_workspaces.service.steering('task')])
        api.db.update_coding_task('task',status='COMPLETED')
        with patch.object(api._coding_scheduler_wake,'set'):
            api._dispatch_coding_followups('task');api._dispatch_coding_followups('task')
        tasks=api.db.coding_tasks('c');self.assertEqual(2,len(tasks))
        next_task=next(item for item in tasks if item['id']!='task')
        self.assertEqual('赤色にする\n\n文字を大きくする',next_task['original_goal'])
        self.assertEqual('Fix a.py',next_task['requirements']['prior_task_goal'])
        self.assertEqual(['APPLIED','APPLIED'],[row['status'] for row in api.task_workspaces.service.steering('task')])

    def test_steer_is_ready_at_boundary_and_not_duplicated_in_transcript(self):
        task=self.task('task');api.db.update_coding_task('task',status='RUNNING')
        self.control(task,'配色を保つ',mode='steer')
        self.assertEqual('QUEUED',api.task_workspaces.service.steering('task')[0]['status'])
        self.assertEqual(0,sum(message['role']=='user' for message in api.db.conversation('c')['messages']))

    def test_followup_inherits_direct_override_after_non_git_setup(self):
        root,task=self.empty_task();api._coding_workspace_ready(task);api.task_workspaces.service.setup(task['id'],'direct')
        api.db.update_coding_task(task['id'],status='COMPLETED')
        with patch.object(api._coding_scheduler_wake,'set'):
            next_task=api._new_coding_followup(api.db.coding_task(task['id']),'配色を青く','next')
        self.assertEqual('QUEUED',next_task['status'])
        self.assertEqual('direct',api.task_workspaces.service.requested_mode(next_task['id']))
        self.assertFalse((root/'.git').exists())

    def test_empty_project_plan_chat_approval_real_files_and_node_tests(self):
        root,task=self.empty_task('lunch');api._coding_workspace_ready(task)
        api.task_workspaces.service.setup(task['id'],'direct')
        goal='昼食ルーレットを index.html、styles.css、lottery.cjs、lottery.test.cjs に新規作成。まず実装計画を作成し、承認後に進めてください。外部ライブラリ・ネットワークなし。node:testで検証。commitとpushは禁止。'
        requirements=normalize_coding_requirements(goal)
        # Set the original goal exactly as a newly registered request would.
        with api.db.connect() as conn:conn.execute('UPDATE coding_tasks SET original_goal=? WHERE id=?',(goal,task['id']))
        api.db.update_coding_task(task['id'],requirements=requirements)
        files={
            'lottery.cjs': "function parseDishes(text){return [...new Set(text.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean))]}\nfunction pick(dishes,previous,random=Math.random){const pool=dishes.length>1?dishes.filter(s=>s!==previous):dishes;return pool.length?pool[Math.floor(random()*pool.length)]:null}\nmodule.exports={parseDishes,pick};\n",
            'lottery.test.cjs': "const {test}=require('node:test');const assert=require('node:assert/strict');const {parseDishes,pick}=require('./lottery.cjs');\ntest('empty',()=>assert.equal(pick(parseDishes('  \\n')),null));\ntest('avoid previous',()=>assert.equal(pick(['カレー','うどん'],'カレー',()=>0),'うどん'));\ntest('one',()=>assert.equal(pick(['カレー'],'カレー'),'カレー'));\n",
            'index.html': '<!doctype html><html lang="ja"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="styles.css"><title>昼食ルーレット</title><h1>昼食ルーレット</h1><textarea aria-label="候補"></textarea><button>候補から選ぶ</button><output></output><ol></ol><script>let previous="",history=[];const input=document.querySelector("textarea"),out=document.querySelector("output"),list=document.querySelector("ol");document.querySelector("button").onclick=()=>{const dishes=[...new Set(input.value.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean))];if(!dishes.length){out.textContent="候補を入力してください";return}const pool=dishes.length>1?dishes.filter(s=>s!==previous):dishes;previous=pool[Math.floor(Math.random()*pool.length)];out.textContent=previous;history=[previous,...history].slice(0,5);list.replaceChildren(...history.map(s=>{const li=document.createElement("li");li.textContent=s;return li}))}</script></html>',
            'styles.css': 'body{max-width:40rem;margin:2rem auto;padding:1rem;font-family:sans-serif}textarea,button{box-sizing:border-box;width:100%;min-height:3rem;margin:1rem 0}output{display:block;font-size:2rem}\n',
        }
        planned={'schema_version':1,'original_goal':goal,'scope':{'allowed':list(files),'forbidden':[]},'assumptions':[],
            'max_retries_per_phase':2,'requires_user_approval':True,
            'file_manifest':[{'path':path,'action':'create','role':'implementation','required':True,'owner_phase':'p1'} for path in files],
            'phases':[{'id':'p1','goal':'昼食ルーレットの画面と抽選ロジックとテストを作成','status':'pending','dependencies':[],
                'done':['昼食ルーレットの4ファイルが実装されている'],'verify':['node:test が合格する'],'risks':[],
                'execution_mode':'IMPLEMENTATION','verification_command':['node','--test','lottery.test.cjs'],'verification_targets':list(files)}]}
        with patch.object(api,'_generate_plan',return_value=(planned,'{}')),patch.object(api._coding_scheduler_wake,'set'):
            api._run_coding_planning(api.db.coding_task(task['id']))
        saved=api.db.coding_task(task['id']);self.assertEqual('WAITING_FOR_PLAN_APPROVAL',saved['status'])
        self.assertEqual([],list(root.iterdir()))
        with patch.object(api._coding_scheduler_wake,'set'):
            self.control(saved,'承認します')
        class Model:
            def generate(self,*args,**kwargs):return {'text':json.dumps({'operations':[{'op':'write','path':path,'content':content} for path,content in files.items()]})}
        with patch.object(api.runtime,'model',Model()),patch.object(api.runtime,'db',api.db):
            api.run_managed_task(task['id'],str(root))
        saved=api.db.coding_task(task['id']);self.assertEqual('COMPLETED',saved['status'])
        self.assertEqual(set(files),{path.name for path in root.iterdir() if path.is_file()})
        report=api.db.coding_phase_reports(task['id'])[-1]['structured_report']
        self.assertTrue(report['test_pass']);self.assertEqual([],report['test_fail'])
        self.assertFalse((root/'.git').exists())
        self.assertEqual(set(files),{file['path'] for file in api.task_workspaces.service.changes(task['id'])['files']})

    def test_plan_correction_replans_and_increments_revision_before_approval(self):
        if __package__:
            from .test_coding_tasks import plan
        else:
            from test_coding_tasks import plan
        task=self.task('revision');api.db.update_coding_task(task['id'],status='WAITING_FOR_PLAN_APPROVAL',plan=plan(task['original_goal']),plan_revision=3)
        with patch.object(api._coding_scheduler_wake,'set'):
            result=self.control(api.db.coding_task(task['id']),'配色を青くし、既存の関数を保ってください',mode='steer')
        self.assertEqual('',result['response'])
        self.assertEqual([],api.db.conversation('c')['messages'])
        saved=api.db.coding_task(task['id']);self.assertEqual('QWEN_PLANNING',saved['activity'])
        with patch.object(api,'_generate_plan',return_value=(plan(task['original_goal']),'{}')) as generate,patch.object(api._coding_scheduler_wake,'set'):
            api._run_coding_planning(saved)
        self.assertIn('配色を青く',generate.call_args.args[2])
        saved=api.db.coding_task(task['id']);self.assertEqual(4,saved['plan_revision'])
        self.assertEqual('WAITING_FOR_PLAN_APPROVAL',saved['status'])
        self.assertEqual('APPLIED',api.task_workspaces.service.steering(task['id'])[0]['status'])

    def test_http_stream_plan_request_and_approval_are_task_bound(self):
        from fastapi.testclient import TestClient
        task=self.task('http');api.db.update_coding_task(task['id'],status='WAITING_FOR_PLAN_APPROVAL',plan={'phases':[{'id':'p1','goal':'Implement lunch'}],'scope':{'allowed':['index.html']}})
        with patch.object(api.runtime.model,'generate',side_effect=AssertionError('control must not generate code')),patch.object(api._coding_scheduler_wake,'set'):
            client=TestClient(api.app)
            response=client.post('/api/chat/stream',json={'conversation_id':'c','coding_task_id':'http','message':'計画を出して'})
            self.assertEqual(200,response.status_code);self.assertIn('Implement lunch',response.text);self.assertIn('http',response.text)
            response=client.post('/api/chat/stream',json={'conversation_id':'c','coding_task_id':'http','message':'承認します'})
            self.assertEqual(200,response.status_code);self.assertIn('CODING_ORCHESTRATOR',response.text)
        self.assertEqual('QUEUED',api.db.coding_task('http')['status'])

    def test_http_followup_saves_one_user_message(self):
        from fastapi.testclient import TestClient
        task=self.task('http');api.db.update_coding_task(task['id'],status='RUNNING')
        with patch.object(api.runtime.model,'generate',side_effect=AssertionError('follow-up must not generate code')):
            response=TestClient(api.app).post('/api/chat',json={'conversation_id':'c','coding_task_id':'http','coding_followup':'queue','message':'文字を大きくして','message_id':'saved-user'})
        self.assertEqual(200,response.status_code)
        messages=api.db.conversation('c')['messages'];self.assertEqual(1,sum(row['role']=='user' for row in messages))
        self.assertEqual('WAITING',api.task_workspaces.service.steering('http')[0]['status'])
