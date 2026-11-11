"""Regression cases for defects observed in the r06 same-chat GUI run."""
import unittest
from unittest.mock import patch
from olcr_api import external_tools as tools
from olcr_api.analysis_scope import country_scope
from olcr_api.analysis_coverage import evidence_needs
from olcr_api.analysis_summary import record_briefing


class AnalysisR06CompilerTests(unittest.TestCase):
    def test_space_weather_does_not_require_terrestrial_weather(self):
        needs = {x['key'] for x in evidence_needs('太陽活動と宇宙天気の変化を説明して')}
        self.assertIn('space_weather', needs)
        self.assertNotIn('weather', needs)
        self.assertIn('weather', {x['key'] for x in evidence_needs('宇宙天気と東京の気温を比較して')})

    def test_compound_jurisdiction_retains_japan_us_and_eu(self):
        scope = country_scope('日米欧の物価と金利を比較して')
        self.assertTrue({'JP', 'US', 'FR', 'DE'} <= scope)
        self.assertNotIn('CN', scope)

    def test_regional_trade_is_not_turned_into_bilateral_japan_trade(self):
        args = tools.compile_provider_arguments('trade.un_comtrade', '東南アジア各国の所得成長と貿易構成を比較して')
        self.assertEqual(11, len(args['requests']))
        self.assertEqual('0', args['partner_code'])
        self.assertNotIn('392', {x['reporter_code'] for x in args['requests']})
        self.assertIn('360', {x['reporter_code'] for x in args['requests']})

    def test_unknown_reporter_cannot_silently_become_japan(self):
        args = tools.compile_provider_arguments('trade.un_comtrade', '2024年のスリランカの輸出額を確認して')
        self.assertTrue(args['unsupported_capability'])
        bilateral = tools.compile_provider_arguments('trade.un_comtrade', '2024年の日本のドイツ向け輸出')
        self.assertEqual('392', bilateral['reporter_code'])
        self.assertEqual('276', bilateral['partner_code'])

    def test_japanese_policy_apis_receive_a_topic_not_sibling_instructions(self):
        request = '日本のAI政策審議と関連法令、国会発言を比較して'
        for tool in ('japan.diet_transcript', 'japan.law'):
            with self.subTest(tool=tool):
                args = tools.compile_provider_arguments(tool, request)
                self.assertEqual('人工知能', args['query'])
                self.assertEqual(request, args['request_text'])

    def test_repository_results_must_match_the_requested_subject(self):
        from olcr_api.evidence_quality import filter_provider_evidence
        data = {'items': [{'full_name': 'example/agent', 'description': 'AI agent tool execution'},
                          {'full_name': 'example/music', 'description': 'Music player'}]}
        result, rejected = filter_provider_evidence(data, 'AIエージェントの実装活動を比較して', 'software.github')
        self.assertEqual(1, rejected)
        self.assertEqual('example/agent', result['items'][0]['full_name'])

    def test_repository_search_does_not_require_a_known_repo(self):
        args = tools.compile_provider_arguments('software.github', 'AIエージェント開発記事の傾向とGitHub上の実装活動を照合して')
        self.assertEqual('search', args['endpoint'])
        self.assertEqual('AI agent', args['query'])
        payload = {'total_count': 42, 'incomplete_results': True, 'items': [
            {'full_name': 'example/agent', 'description': 'AI agent implementation',
             'pushed_at': '2026-10-20T00:00:00Z', 'html_url': 'https://github.com/example/agent'}]}
        with patch.object(tools, '_request', return_value=payload) as request:
            result = tools.execute('software.github', args)
        self.assertEqual('/search/repositories', request.call_args.args[1])
        self.assertEqual('AI agent', request.call_args.args[2]['q'])
        self.assertFalse(result['data']['request_complete'])
        self.assertEqual('example/agent', result['data']['items'][0]['full_name'])
        named = tools.compile_provider_arguments('software.github', 'FastAPIの最新版を調べて')
        self.assertEqual('fastapi/fastapi', named['repo'])
        self.assertEqual('latest', named['endpoint'])

    def test_large_region_uses_period_bounded_imf_request_and_checks_returned_scope(self):
        args = tools.compile_provider_arguments('econ.imf', '2024年のアフリカ諸国の債務を比較して')
        self.assertGreater(len(args['countries']), 8)
        payload = {'values': {'GGXWDG_NGDP': {'AGO': {'2024': 60}, 'USA': {'2024': 100}}}}
        with patch.object(tools, '_request', return_value=payload) as request:
            result = tools.execute('econ.imf', args)
        self.assertEqual('/external/datamapper/api/v1/GGXWDG_NGDP', request.call_args.args[1])
        self.assertEqual('2024', request.call_args.args[2]['periods'])
        self.assertEqual(['AGO'], [x['country'] for x in result['data']['items']])
        self.assertFalse(result['data']['request_complete'])
        self.assertIn('BEN', result['data']['missing_countries'])

    def test_jsonstat_preserves_dates_and_recent_observations_across_countries(self):
        payload = {'label': 'HICP annual rate', 'id': ['geo', 'time'], 'size': [2, 2],
                   'dimension': {'geo': {'category': {'index': {'AT': 0, 'DE': 1}}},
                                 'time': {'category': {'index': {'2021-01': 0, '2025-01': 1}}}},
                   'value': {'0': 1, '1': 2, '2': 3, '3': 4}}
        result = tools._external_result('econ.eurostat', 'EU inflation', payload, 'https://ec.europa.eu/eurostat/')
        rows = result['data']['items']
        self.assertEqual(['2025-01', '2025-01'], [row['date'] for row in rows[:2]])
        self.assertEqual({'AT', 'DE'}, {row['country'] for row in rows[:2]})
        self.assertEqual([2, 4], [row['value'] for row in rows[:2]])

    def test_speech_records_keep_speaker_and_the_actual_excerpt(self):
        evidence = [{'provider': '国会会議録', 'sources': [{'source_id': 'S1'}], 'data': {'items': [
            {'meeting': '科学技術委員会', 'speaker': '発言者', 'date': '2025-04-11',
             'speech_excerpt': '人工知能の利用に関する質疑。'}]}}]
        text = record_briefing(evidence)
        self.assertIn('人工知能の利用に関する質疑。', text)
        self.assertIn('発言者', text)

    def test_record_brief_preserves_content_and_does_not_turn_dates_into_metrics(self):
        evidence = [{'provider': 'Parliament', 'sources': [{'source_id': 'S1'}], 'data': {'items': [
            {'title': 'Environmental policy bill', 'date': '2025-04-11', 'description': 'Committee debate on emission limits.'}]}}]
        text = record_briefing(evidence)
        self.assertIn('Environmental policy bill', text)
        self.assertIn('Committee debate on emission limits.', text)
        self.assertIn('[S1]', text)
        self.assertNotIn('API応答に数値がありません', text)


import test_autonomous_api_analysis as support
import olcr_api.app as app
from olcr_api.models import Task, TaskState, Route


class AnalysisR06ExecutionTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def test_empty_api_then_web_preserves_attempts_route_and_sources(self):
        task = Task('fallback'); task.transition(TaskState.ROUTING)
        task.route = Route.RETRIEVAL; task.transition(TaskState.SEARCHING); task.transition(TaskState.COMPLETED)
        task.selected_context.append({'fallback_sources': [{'url': 'https://example.invalid/data'}]})
        ids = ['econ.worldbank']
        with patch.object(app, '_laya_select_analysis_apis', return_value=ids), \
             patch.object(app, '_brain_plan_analysis_apis', return_value=(ids, support._recipe(ids))), \
             patch.object(app, 'external_tool_credential_setup', return_value=None), \
             patch.object(app, 'compile_provider_arguments', return_value={'query': 'current'}), \
             patch.object(app, 'execute_external_tool', side_effect=tools.ExternalToolError('PROVIDER_EMPTY')), \
             patch.object(app.runtime, 'execute', return_value=(task, 'Verified web evidence')):
            outcome = app._run_autonomous_analysis('日本の経済成長を分析して', {})
        self.assertEqual(Route.EXTERNAL_API, outcome['task'].route)
        self.assertEqual('PROVIDER_EMPTY', task.tool_executions[0]['error'])
        self.assertEqual('https://example.invalid/data', outcome['sources'][0]['url'])
        self.assertIn({'recovery_route': 'RETRIEVAL'}, task.selected_context)

    def test_failed_enrichment_cannot_destroy_successful_provider_evidence(self):
        ids = ['econ.worldbank', 'trade.un_comtrade']
        result = {'tool_id': ids[0], 'provider': 'World Bank',
                  'data': {'items': [{'country': 'Japan', 'date': '2025', 'value': 1}]},
                  'sources': [{'canonical_url': 'https://example.invalid/growth'}]}
        with patch.object(app, '_laya_select_analysis_apis', return_value=ids), \
             patch.object(app, '_brain_plan_analysis_apis', return_value=(ids, support._recipe(ids))), \
             patch.object(app, 'external_tool_credential_setup', return_value=None), \
             patch.object(app, 'compile_provider_arguments', return_value={'query': 'current'}), \
             patch.object(app, 'execute_external_tool', side_effect=[result, TimeoutError('provider timeout')]), \
             patch.object(app.runtime, 'execute', side_effect=RuntimeError('search unavailable')), \
             patch.object(app, '_compose_autonomous_analysis', return_value=('Verified GDP observations', [], False)), \
             patch.object(app, 'external_tool_display_blocks', return_value=[]):
            outcome = app._run_autonomous_analysis('日本の経済成長と貿易を比較して', {})
        self.assertEqual(TaskState.COMPLETED, outcome['task'].state)
        self.assertIn('Verified GDP observations', outcome['response'])
        self.assertEqual(['success', 'failed'], [x['status'] for x in outcome['task'].tool_executions])

    def test_narrative_date_is_not_a_missing_numeric_series(self):
        evidence = [{'provider': 'Parliament', 'sources': [{'source_id': 'S1'}],
                     'data': {'items': [{'title': 'Environmental policy', 'date': '2025-04-11'}]}}]
        answer = app._deterministic_tabular_analysis(evidence, '環境政策を比較して')
        self.assertIn('Environmental policy', answer)
        self.assertNotIn('API応答に数値がありません', answer)


class RecoveryContextBudgetTests(unittest.TestCase):
    def test_long_web_recovery_retains_relevant_excerpt_within_model_budget(self):
        import tempfile
        from pathlib import Path
        from olcr_api.config import Settings
        from olcr_api.db import Database
        from olcr_api.retrieval import RetrievalRouter, FileRetriever, FTSRetriever, DisabledVectorStore
        from olcr_api.runtime import Runtime
        with tempfile.TemporaryDirectory() as root:
            db = Database(str(Path(root) / 'test.db')); db.initialize()
            settings = Settings(db_path=db.path, allowed_roots=(root,), main_model='mock',
                                web_mode='auto', web_provider='brave', context_budget=1024).validated()
            runtime = Runtime(settings, db, RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False), None)
            raw = ('Unrelated navigation and furniture catalogue.\n\n' * 2000
                   + 'Climate change legislation limits carbon emissions.\n\n' * 1000)
            candidates = [{'title': 'US climate legislation', 'url': 'https://example.invalid/data'}]
            with patch('olcr_api.runtime.brave_search', return_value=candidates), \
                 patch('olcr_api.runtime.provider_key', return_value='configured'), \
                 patch('olcr_api.runtime.web_fetch', return_value={'text': raw, 'final_url': candidates[0]['url']}):
                task, answer = runtime.execute('米国の気候法案を比較して', api_miss_fallback=True, evidence_only=True)
            sources = [x for item in task.selected_context for x in item.get('fallback_sources', [])]
            self.assertTrue(sources)
            self.assertLessEqual(sum(len(x['text']) for x in sources), 1024)
            self.assertNotIn('furniture', sources[0]['text'])
            self.assertIn('Climate', answer)
            self.assertTrue(sources[0]['truncated'])


if __name__ == '__main__':
    unittest.main()
