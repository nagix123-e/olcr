import json
import time
from unittest.mock import patch
from pydantic import ValidationError
from olcr_api.chat_maps import ChatMap, map_context, latest_map
import test_concept_diagrams as diagram_tests
import unittest


def map_fixture():
    return {'type':'location_map','schema_version':1,'points':[
        {'lat':35.68,'lon':139.76,'name':'日本, 東京都, 千代田区'},
        {'lat':34.69,'lon':135.50,'name':'日本, 大阪府, 大阪市'}]}

class ChatMapApiTests(diagram_tests.ConceptDiagramApiTests):
    def test_only_b_keeps_its_identity_in_saved_context(self):
        value=ChatMap.model_validate({'points':[{'lat':35,'lon':139,'name':'Tokyo','pin':'B'}]})
        self.assertIn('"pin": "B"',map_context(value))
        self.assertEqual('B',value.model_dump()['points'][0]['pin'])
        with self.assertRaises(ValidationError):
            ChatMap.model_validate({'points':[{'lat':35,'lon':139,'name':'Tokyo','pin':'A'},{'lat':36,'lon':140,'name':'Other','pin':'A'}]})

    # Reuse the isolated database and offline host fixture.
    def test_map_chat_and_stream_persist_original_text_and_ground_analysis_with_names(self):
        for endpoint in ['/api/chat','/api/chat/stream']:
            settings=self.api.settings.with_overrides({'external_access_enabled':True})
            outcome={'blocked':'NO_PROVIDER_EVIDENCE'}
            with patch.object(self.api,'settings',settings), patch.object(self.api,'router_decision',return_value=None) as router, patch.object(self.api,'_run_autonomous_analysis',return_value=outcome) as analysis:
                response=self.client.post(endpoint,json={'message':'ピンの人口密度を比較して','project_id':self.project,'location_map':map_fixture()})
            self.assertEqual(response.status_code,200,response.text)
            data=response.json() if endpoint.endswith('/chat') else json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
            record=self.db.conversation(data['conversation_id'])
            self.assertEqual(record['messages'][0]['content'],'ピンの人口密度を比較して')
            self.assertEqual(record['messages'][0]['blocks'][0],map_fixture())
            request=analysis.call_args.args[0]
            self.assertIn('千代田区',request);self.assertIn('大阪市',request)
            self.assertNotIn('139.76',request);router.assert_called_once()
            self.assertIn('location_map',analysis.call_args.kwargs)
            self.assertIn('数値の比較は行っていません',record['messages'][-1]['content'])
            with patch.object(self.api,'settings',settings), patch.object(self.api,'router_decision',return_value=None), patch.object(self.api,'_run_autonomous_analysis',return_value=outcome) as followup:
                result=self.client.post(endpoint,json={'message':'この二地点の歴史を説明して','project_id':self.project,'conversation_id':record['id']})
            self.assertEqual(result.status_code,200,result.text)
            self.assertIn('千代田区',followup.call_args.args[0])

    def test_invalid_map_is_rejected_and_external_off_makes_no_calls(self):
        bad=map_fixture();bad['points']*=2
        self.assertEqual(422,self.client.post('/api/chat',json={'message':'compare','location_map':bad}).status_code)
        with patch.object(self.api,'router_decision') as router, patch.object(self.api,'_run_autonomous_analysis') as analysis, patch.object(self.api,'reverse_place') as reverse:
            response=self.client.post('/api/chat',json={'message':'説明して','project_id':self.project,'location_map':map_fixture()})
            self.assertEqual(200,response.status_code)
            self.assertEqual(403,self.client.get('/api/maps/reverse?lat=35&lon=139').status_code)
            router.assert_not_called();analysis.assert_not_called();reverse.assert_not_called()

    def test_analysis_retrieves_each_pin_then_synthesizes_combined_evidence(self):
        from contextlib import ExitStack
        value=ChatMap.model_validate(map_fixture())
        request='歴史を比較して'+map_context(value)
        def provider(tool_id, arguments):
            return {'tool_id':tool_id,'provider':'Wikipedia','data':{'title':arguments['query'],'summary':'Local evidence'},
                    'sources':[{'title':arguments['query'],'url':'https://example.org/place'}]}
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.api,'settings',self.api.settings.with_overrides({'external_access_enabled':True})))
            stack.enter_context(patch.object(self.api,'_laya_select_analysis_apis',return_value=['knowledge.wikimedia']))
            stack.enter_context(patch.object(self.api,'_brain_plan_analysis_apis',return_value=(['knowledge.wikimedia'],{'sources':[]})))
            compiler=stack.enter_context(patch.object(self.api,'compile_provider_arguments',side_effect=lambda tool,prompt,seed,**kw:{'query':prompt}))
            execute=stack.enter_context(patch.object(self.api,'execute_external_tool',side_effect=provider))
            stack.enter_context(patch.object(self.api,'external_tool_credential_setup',return_value=None))
            stack.enter_context(patch.object(self.api,'filter_provider_evidence',side_effect=lambda data,*a:(data,0)))
            stack.enter_context(patch.object(self.api,'_analysis_result_matches_scope',return_value=True))
            stack.enter_context(patch.object(self.api,'missing_coverage',return_value=[]))
            compose=stack.enter_context(patch.object(self.api,'_compose_autonomous_analysis',return_value=('Comparison',[{'url':'https://example.org/place'}],True)))
            result=self.api._run_autonomous_analysis(request,{},location_map=value)
        self.assertEqual(execute.call_count,2)
        self.assertIn('千代田区',compiler.call_args_list[0].args[1])
        self.assertNotIn('大阪市',compiler.call_args_list[0].args[1])
        self.assertIn('大阪市',compiler.call_args_list[1].args[1])
        self.assertEqual(compiler.call_args_list[0].kwargs['map_point'].lat, value.points[0].lat)
        evidence=compose.call_args.args[1]
        self.assertEqual([row['data']['map_pin'] for row in evidence],['A','B'])
        self.assertEqual(result['response'],'Comparison')

    def test_local_candidates_do_not_collapse_to_country_statistics(self):
        request='人口密度を比較して'+map_context(ChatMap.model_validate(map_fixture()))
        candidates=self.api._analysis_candidates_for_domain(request,['econ.worldbank','knowledge.wikimedia'])
        self.assertNotIn('econ.worldbank',candidates)
        self.assertEqual(candidates,['econ.jp_estat'])

    def test_narrative_rejects_unknown_fact_and_renders_actual_source_words(self):
        request='この地点の歴史を説明して'+map_context(ChatMap(points=[map_fixture()['points'][0]]))
        evidence=[{'tool_id':'knowledge.wikimedia','provider':'Wikipedia',
            'data':{'map_task_kind':'knowledge.wikimedia','map_pin':'A','map_place':'千代田区',
                    'title':'史跡','extract':'1945年に建設された史跡である。'},
            'sources':[{'title':'史跡','canonical_url':'https://ja.wikipedia.org/wiki/example'}]}]
        recipe={}
        def selection(fid):
            return {'text':json.dumps({'selections':{'A:history':[fid]}},ensure_ascii=False)}
        with patch.object(self.api.runtime, '_generate_brain', side_effect=[selection('F999'), selection('F1')]) as brain:
            response, _, accepted=self.api._compose_autonomous_analysis(request,evidence,recipe)
        self.assertTrue(accepted)
        self.assertEqual(2,brain.call_count)
        self.assertIn('1945年に建設された史跡である。',response)
        self.assertIn('https://ja.wikipedia.org/wiki/example',response)
        self.assertEqual('brain_selected_source_passages',recipe['_composition_mode'])



class MapContractTests(unittest.TestCase):
 def test_map_contract_and_context(self):
     value=ChatMap.model_validate(map_fixture())
     assert '千代田区' in map_context(value)
     assert '35.68' not in map_context(value)
     assert latest_map({'messages':[{'blocks':[value.model_dump()]}]}) == value
     for points in [[],value.model_dump()['points']*2,[{'lat':91,'lon':0,'name':'bad'}],[{'lat':float('nan'),'lon':0,'name':'bad'}]]:
         try: ChatMap(points=points)
         except ValidationError: pass
         else: raise AssertionError('invalid points accepted')
