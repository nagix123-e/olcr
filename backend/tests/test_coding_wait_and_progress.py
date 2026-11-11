import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from olcr_api.ollama import OllamaProvider, inference_timeout, model_timeout_seconds, unbounded_inference
from olcr_api.coding_progress import stop_progress


class CodingWaitAndProgressTests(unittest.TestCase):
    def test_test_repair_exhaustion_label_does_not_claim_every_failure_was_startup(self):
        snapshot=stop_progress({'plan':{'phases':[]}},[],'PROJECT_TEST_REPAIR_EXHAUSTED',False)
        self.assertIn('テスト修正が上限に達したため',snapshot['text'])
        self.assertNotIn('テスト起動用コード',snapshot['text'])

    def test_stop_log_shows_actual_failing_assertion_before_long_successful_tap_output(self):
        task={'plan_revision':0,'plan':{'phases':[{'id':'p','goal':'Verify','status':'pending'}]}}
        report={'plan_revision':0,'test_executed':['node --test'],'test_fail':[json.dumps({'ids':['REQUIRED_TESTS'],
            'error':'node:test failed: TAP version 13 '+('successful case '*500)})],
            'typed_execution_summary':{'operations':[{'tool':'verification_command','output':{
                'test_runs':[{'registered_tests':10,'passed':9,'failed':1,'failures':[{
                    'name':'latest five history','detail':"duration_ms: 1\nerror: Expected latest five\nactual: ['B','C','D','E','F']\nexpected: ['A','B','C','D','E']",
                    'source_locations':[{'path':'logic.test.js','line':50}]}]}],
                'failures':[{'ids':['REQUIRED_TESTS'],'error':'node:test failed: TAP version 13...'}]}}]}}
        text=stop_progress(task,[{'phase_id':'p','structured_report':report}],'REPLAN_LIMIT',False)['text']
        self.assertIn('登録10件、成功9件、失敗1件',text)
        self.assertIn('latest five history（logic.test.js:50）',text)
        self.assertIn("actual: ['B','C','D','E','F']",text)
        self.assertIn("expected: ['A','B','C','D','E']",text)
        self.assertNotIn('successful case',text)
        self.assertNotIn('TAP version',text)

    def test_successful_node_run_does_not_hide_a_failed_browser_check(self):
        task={'plan_revision':0,'plan':{'phases':[{'id':'p','goal':'Verify','status':'pending'}]}}
        report={'test_executed':['owned runner'],'typed_execution_summary':{'operations':[{'tool':'verification_command',
            'output':{'test_runs':[{'registered_tests':5,'passed':5,'failed':0}],
                'failures':[{'ids':['VERIFY:1'],'error':'AssertionError: multiline input is missing',
                    'assertion':{'actual':'INPUT','expected':'TEXTAREA'}}]}}]}}
        text=stop_progress(task,[{'phase_id':'p','structured_report':report}],'REPLAN_LIMIT',False)['text']
        self.assertIn('登録5件、成功5件、失敗0件',text)
        self.assertIn('multiline input is missing',text)
        self.assertIn('TEXTAREA',text)
        self.assertNotIn('保存済みのテスト失敗：なし',text)

    def test_latest_command_result_supersedes_prior_probe_in_the_same_attempt(self):
        task={'plan_revision':0,'plan':{'phases':[{'id':'p','goal':'Verify','status':'pending'}]}}
        report={'test_executed':['owned runner'],'typed_execution_summary':{'operations':[
            {'tool':'verification_command','output':{'test_runs':[{'registered_tests':5,'passed':4,'failed':1,
                'failures':[{'name':'resolved failure','detail':'old error'}]}]}},
            {'tool':'verification_command','output':{'test_runs':[{'registered_tests':5,'passed':5,'failed':0}]}}]}}
        text=stop_progress(task,[{'phase_id':'p','structured_report':report}],'PAUSED',True)['text']
        self.assertIn('登録5件、成功5件、失敗0件',text)
        self.assertNotIn('resolved failure',text)
        self.assertNotIn('成功4件',text)

    def test_stop_log_keeps_changes_from_earlier_attempts_and_current_failure_only(self):
        task={'plan_revision':1,'plan':{'phases':[{'id':'p','status':'pending','goal':'実装と検証'}]}}
        rows=[{'phase_id':'p','structured_report':{'plan_revision':0,'changed_files':['first.js'],
                    'test_fail':['old resolved failure']}},
              {'phase_id':'p','structured_report':{'plan_revision':1,'changed_files':['second.js']}},
              {'phase_id':'p','structured_report':{'plan_revision':1,'changed_files':['rolled-back.js'],
                    'typed_execution_summary':{'worktree_state':'ROLLED_BACK'}}},
              {'phase_id':'p','structured_report':{'plan_revision':1,'changed_files':[],
                    'test_fail':['current failure']}},
              {'phase_id':'future','structured_report':{'plan_revision':2,'changed_files':['future.js']}}]
        text=stop_progress(task,rows,'PAUSED',True)['text']
        self.assertIn('first.js',text);self.assertIn('second.js',text)
        self.assertNotIn('rolled-back.js',text);self.assertNotIn('future.js',text)
        self.assertIn('current failure',text);self.assertNotIn('old resolved failure',text)

    def test_explicit_deadline_bounds_managed_inference_and_restores_other_requests(self):
        self.assertEqual(750,model_timeout_seconds(750))
        with inference_timeout(10):
            with unbounded_inference():
                self.assertLessEqual(model_timeout_seconds(750),10)
                with ThreadPoolExecutor(1) as pool:
                    self.assertEqual(120,pool.submit(model_timeout_seconds,120).result())
            self.assertLessEqual(model_timeout_seconds(750),10)
        self.assertEqual(750,model_timeout_seconds(750))

    def test_repetitive_thinking_closes_the_provider_stream(self):
        from olcr_api.ollama import ModelFailure
        text='Identical long reasoning passage about validating the same browser action. '*40
        response=io.BytesIO(('\n'.join(json.dumps({'message':{'thinking':text},'done':False}) for _ in range(4))).encode())
        with self.assertRaisesRegex(ModelFailure,'THINKING_REPETITION'):
            list(OllamaProvider('http://fixture')._stream(response,0,{}))
        self.assertTrue(response.closed)

    def test_ollama_bounds_socket_inactivity_without_a_total_coding_deadline(self):
        with unbounded_inference(),patch('olcr_api.ollama.request.urlopen',return_value=io.BytesIO(b'{"message":{"content":"ok"}}')) as opened:
            result=OllamaProvider('http://localhost').generate([], 'test-model')
        self.assertEqual('ok',result['text'])
        self.assertGreater(opened.call_args.kwargs['timeout'],0)

    def test_progress_uses_verified_phases_and_includes_partial_errors(self):
        task={'plan_revision':0,'plan':{'phases':[
            {'id':'p1','goal':'ファイル構成','status':'pass'},
            {'id':'p2','goal':'抽選ロジック','status':'pending'},
            {'id':'p3','goal':'UI','status':'pending'},
            {'id':'p4','goal':'テストと修正','status':'pending'}]}}
        rows=[{'phase_id':'p1','structured_report':{'status':'PASS','manager_decision':{'decision':'PASS'},'changed_files':['index.html'],'test_executed':['file existence check']}},
              {'phase_id':'p2','structured_report':{'status':'FAIL','changed_files':['logic.js'],'test_executed':['node tests.js'],'test_fail':['Assignment to constant variable.']}}]
        snapshot=stop_progress(task,rows,'PAUSED',True)
        self.assertIn('1 / 4 完了',snapshot['text'])
        self.assertIn('抽選ロジック',snapshot['text'])
        self.assertIn('logic.js',snapshot['text'])
        self.assertIn('Assignment to constant variable.',snapshot['text'])
        self.assertIn('ユーザーの一時停止',snapshot['text'])
        self.assertEqual(snapshot,stop_progress(task,rows,'PAUSED',True))

    def test_phase_status_without_successful_evidence_is_not_completion(self):
        snapshot=stop_progress({'plan':{'phases':[{'id':'p1','status':'pass','goal':'実装'}]}}, [], 'REPLAN_VALIDATION',False)
        self.assertIn('0 / 1 完了',snapshot['text'])
        self.assertIn('再計画の検証失敗',snapshot['text'])
        self.assertIn('テストの実行記録はありません',snapshot['text'])

    def test_failed_planning_does_not_report_zero_remaining_work(self):
        result=stop_progress({'plan':None,'status':'RESUMABLE'},[],'PLANNING',False)
        self.assertIn('計画未確定',result['text'])
        self.assertIn('実行可能な計画を確定できなかった',result['text'])
        self.assertNotIn('0 / 0 完了',result['text'])
        self.assertNotIn('残っている工程\n- ありません',result['text'])
        self.assertIn('同じTaskで元の依頼の計画作成から再開',result['text'])

    def test_replan_preserves_completed_evidence_but_never_hides_current_failure(self):
        task={'plan_revision':1,'plan':{'phases':[{'id':'p1','status':'pass','goal':'実装'}]}}
        previous={'phase_id':'p1','structured_report':{'plan_revision':0,'status':'PASS','manager_decision':{'decision':'PASS'}}}
        self.assertIn('1 / 1 完了',stop_progress(task,[previous],'PAUSED',True)['text'])
        current={'phase_id':'p1','structured_report':{'plan_revision':1,'status':'FAIL','errors':['changed contract failed']}}
        self.assertIn('0 / 1 完了',stop_progress(task,[previous,current],'PAUSED',True)['text'])


if __name__ == '__main__':
    unittest.main()
