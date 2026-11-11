import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from olcr_api.db import Database
from olcr_api.trip_planning import TripPlanner, Brief, Itinerary, intent, collect_evidence, events, TravelExtraction

FORM='旅行計画を立てて\n出発地：東京\n目的地：京都\n開始日：2026-12-01\n終了日：2026-12-02\n人数：2\n予算：全員で10万円'
PLAN={'days':[{'date':d,'items':[{'time':'午前','activity':'散策','transport':'徒歩15分（目安）','cost':'未確認','source_ids':[1]}]} for d in ['2026-12-01','2026-12-02']], 'budget_notes':'交通・宿泊は未確認です。','checks':['営業時間を確認']}

class TripTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'db'));self.db.initialize();self.db.create_conversation('trip',1,'c',None)
        self.calls=[];self.research_calls=[]
        def generate(prompt,payload,schema):
            self.calls.append((payload,schema))
            if schema is TravelExtraction:
                return {'relevant':True,'brief':{**payload['previous'],'preferences':payload['message']}}
            return PLAN
        def research(brief):
            self.research_calls.append(brief)
            return [{'id':1,'title':'公式','url':'https://example.org/','retrieved_at':'2026-11-10T00:00:00Z'}],[{'source_id':1,'excerpt':'散策'}],[]
        self.planner=TripPlanner(self.db,generate,research);self.n=0
    def turn(self,text,source=None,cid='c'):
        self.n+=1
        return self.planner.respond(SimpleNamespace(message=text,conversation_id=cid,message_id=source or str(self.n),client_time_zone='Asia/Tokyo'),lambda v:(cid,'',self.db.conversation(cid)))
    def test_interview_restart_and_scoped_persistence(self):
        result=self.turn('旅行計画を立てて\n目的地：京都')
        self.assertIn('開始日',result['response']);self.assertFalse(self.research_calls)
        self.planner=TripPlanner(self.db,self.planner.generate,self.planner.research)
        self.assertEqual('京都',self.planner.state('c')['brief']['destination'])
        self.assertIsNone(self.planner.state('other'))
        self.assertIsNone(self.turn('旅程機能を実装して'))
    def test_generation_sources_replay_and_revision(self):
        result=self.turn(FORM,'same');self.assertIn('2026-12-02',result['response']);self.assertIn('出典1',result['response'])
        self.assertIn('2026-12-01（火）',result['response'])
        self.assertEqual(self.turn(FORM,'same'),result);self.assertEqual(len(self.calls),1)
        self.turn('旅程を修正して。2日目はゆっくり')
        self.assertEqual(self.planner.state('c')['brief']['origin'],'東京')
        self.assertIn('ゆっくり',self.planner.state('c')['brief']['preferences'])
        self.assertEqual(self.turn('旅程を見せて')['blocks'][0]['status'],'ready')
    def test_invalid_and_unsupported_dates_or_sources_preserve_conditions(self):
        self.turn(FORM.replace('2026-12-02','2026-11-01'))
        self.assertFalse(self.calls)
        self.planner.generate=lambda *args:{**PLAN,'days':PLAN['days'][:1]}
        result=self.turn(FORM)
        self.assertIn('失敗',result['response']);self.assertEqual(self.planner.state('c')['brief']['destination'],'京都')
        self.assertIsNone(self.planner.state('c')['itinerary'])
    def test_failure_cancel_new_and_no_network_when_off(self):
        self.planner.research=lambda brief: (_ for _ in ()).throw(RuntimeError('secret'))
        self.assertIn('保存',self.turn(FORM)['response'])
        self.turn('キャンセル');self.assertEqual(self.planner.state('c')['status'],'paused')
        self.assertIsNone(self.turn('こんにちは'))
        self.turn('/trip new');self.assertEqual(self.planner.state('c')['brief']['destination'],'')
        settings=SimpleNamespace(permission_level='automatic',external_access_enabled=False)
        self.assertEqual(collect_evidence(Brief.model_validate({'start_date':'2026-12-01'}),settings)[0],[])
    def test_stream_and_id_conflict(self):
        result=self.turn(FORM,'id')
        frames=[json.loads(e.removeprefix('data: ')) for e in events(result)]
        self.assertEqual(frames[-1]['response_owner'],'TRIP_PLANNING')
        self.assertIn('処理済み',self.turn('旅程を修正して','id')['response'])
        self.assertEqual(len(self.db.conversation('c')['messages']),2)
    def test_rewind_restores_last_surviving_trip_state(self):
        self.turn(FORM,'original')
        self.turn('旅程を修正して。2日目はゆっくり','edit')
        last=self.db.conversation('c')['messages'][-1]['id']
        self.db.rewind_conversation('c','edit',last,None,__import__('time').time())
        self.assertEqual(self.planner.state('c')['brief']['preferences'],'')
        self.turn('旅程を修正して。美術館を追加','edit')
        self.assertIn('美術館',self.planner.state('c')['brief']['preferences'])
    def test_unrelated_followup_and_invalid_travelers_do_not_escape(self):
        self.turn('旅行の計画を立てて\n目的地：京都')
        before=self.planner.state('c')
        self.planner.generate=lambda *args:{'relevant':False,'brief':before['brief']}
        self.assertIsNone(self.turn('今日のニュースを教えて'))
        self.assertEqual(self.planner.state('c'),before)
        self.assertIn('読み取れません',self.turn('人数：abc')['response'])
    def test_rejects_invented_sources_and_bad_date_keeps_previous(self):
        self.turn(FORM)
        previous=self.planner.state('c')
        self.turn('旅程を修正して\n開始日：2026-02-30')
        self.assertEqual(previous,self.planner.state('c'))
        self.planner.generate=lambda *args:PLAN
        self.planner.research=lambda brief:([],[],[])
        self.assertIn('失敗',self.turn('/trip plan')['response'])
    def test_research_reads_bounded_sources_and_obeys_search_off(self):
        settings=SimpleNamespace(permission_level='automatic',external_access_enabled=True,web_mode='auto',web_provider='brave')
        brief=Brief(destination='京都',origin='東京',start_date='2099-01-01')
        rows=[{'url':f'https://example.org/{i}','title':'公式','snippet':'検索結果'} for i in range(20)]
        with patch('olcr_api.web.brave_search',return_value=rows) as search,patch('olcr_api.web.fetch',return_value={'clean_markdown':'営業時間の本文'}) as fetch:
            sources,context,warnings=collect_evidence(brief,settings)
            self.assertEqual(len(sources),3)
            self.assertEqual(fetch.call_count,3)
            self.assertEqual(context[0]['excerpt'],'営業時間の本文')
            self.assertTrue(all(s['retrieved_at'] for s in sources))
            settings.web_mode='off'
            collect_evidence(brief,settings)
            self.assertGreaterEqual(search.call_count,5)

    def test_unsupported_named_place_is_not_rendered_as_verified(self):
        self.planner.generate=lambda *args:{'days':[{'date':d,'items':[{
            'time':'午前','activity':'伏見稲荷大社を訪問','transport':'未確認',
            'cost':'未確認','source_ids':[1],'place_ids':[999]
        }]} for d in ['2026-12-01','2026-12-02']], 'budget_notes':'未確認','checks':[]}
        result=self.turn(FORM)
        self.assertNotIn('伏見稲荷大社',result['response'])
        self.assertIn('未確認',result['response'])

    def test_evidence_search_covers_relevant_trip_categories(self):
        settings=SimpleNamespace(permission_level='automatic',external_access_enabled=True,web_mode='auto',web_provider='brave')
        brief=Brief(destination='京都',origin='東京',start_date='2099-01-01',end_date='2099-01-02')
        queries=[]
        def search(query,limit):
            queries.append(query)
            return [{'url':f'https://example.org/{len(queries)}','title':f'結果{len(queries)}','snippet':'参考'}]
        with patch('olcr_api.web.brave_search',side_effect=search),patch('olcr_api.web.fetch',side_effect=RuntimeError('offline')):
            sources,context,warnings=collect_evidence(brief,settings)
        self.assertGreaterEqual(len(queries),5)
        self.assertTrue(any('宿泊' in query for query in queries))
        self.assertTrue(any('食事' in query or '飲食' in query for query in queries))
        self.assertTrue(any('交通' in query for query in queries))
        self.assertTrue(any('営業時間' in query or '休業' in query for query in queries))
        coverage=next(item['coverage'] for item in context if 'coverage' in item)
        self.assertEqual({'transport','lodging','food','activities','hours','weather'},set(coverage))

    def test_render_shows_fresh_sources_and_unverified_categories(self):
        result=self.turn(FORM)
        self.assertIn('調査状況',result['response'])
        self.assertIn('未確認',result['response'])
        self.assertIn('取得時刻',result['response'])

    def test_weather_is_attached_only_to_dates_with_forecasts(self):
        from olcr_api.travel_evidence import weather_evidence_for_date
        wrong={'data':{'daily':{'time':['2026-12-02'],'temperature_2m_max':[8]}}}
        right={'data':{'daily':{'time':['2026-12-01'],'temperature_2m_max':[9]}}}
        self.assertIsNone(weather_evidence_for_date(wrong,'2026-12-01'))
        self.assertEqual('2026-12-01',weather_evidence_for_date(right,'2026-12-01')['date'])

    def test_partial_dates_without_valid_client_timezone_are_not_guessed(self):
        value=SimpleNamespace(message='京都で旅行計画を立てたい。12/12から',conversation_id='c',
            message_id='partial-date',client_time_zone=None)
        result=self.planner.respond(value,lambda v:('c','',self.db.conversation('c')))
        self.assertIn('年',result['response'])
        self.assertFalse(self.research_calls)

    def test_partial_date_and_trip_length_resolve_deterministically(self):
        value=SimpleNamespace(message='京都で、3泊4日の旅程を組みたい。12/12から',conversation_id='c',
            message_id='partial-duration',client_time_zone='Asia/Tokyo')
        result=self.planner.respond(value,lambda v:('c','',self.db.conversation('c')))
        brief=self.planner.state('c')['brief']
        self.assertEqual('2026-12-12',brief['start_date'])
        self.assertEqual('2026-12-15',brief['end_date'])
        self.assertIn('出発地',result['response'])
        self.assertFalse(self.research_calls)
    def test_cancelled_request_cannot_commit_partial_reply(self):
        from olcr_api.generation_control import Generation,generation_scope,GenerationCancelled
        generation=Generation()
        def cancel(brief):
            generation.cancel()
            raise RuntimeError('cancelled')
        self.planner.research=cancel
        with generation_scope(generation):
            with self.assertRaises(GenerationCancelled):self.turn(FORM)
        self.assertIsNone(self.planner.state('c'))
        self.assertEqual(self.db.conversation('c')['messages'],[])
    def test_input_bounds_and_intent(self):
        for text in ['旅行計画機能を追加実装して','Translate this itinerary','旅行計画APIの修正']:
            self.assertFalse(intent(text))
        with self.assertRaises(ValueError): Brief(start_date='2026-02-30')
        with self.assertRaises(ValueError): Brief(start_date='2026-12-01',end_date='2027-02-01')
        with self.assertRaises(ValueError): Brief(travelers=0)

if __name__=='__main__':unittest.main()
