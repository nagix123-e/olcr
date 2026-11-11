"""Offline regressions for the 47-case GUI record's PARTIAL/FAIL outputs."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from olcr_api import external_tools as tools
from olcr_api import app
from olcr_api.runtime import Runtime
from olcr_api.models import Task, TaskState


class SemanticFollowupRepairs(unittest.TestCase):
    def test_nested_provider_group_with_null_items_is_renderable(self):
        result = {'tool_id': 'internet.domain_registration', 'provider': 'RDAP', 'data': {
            'items': [{'data': {'items': None}}]}}
        self.assertIn('RDAP', app._new_provider_summary(result))

    def route_result(self):
        return tools._result('geo.routing', 'comparison', {'items': [
            {'request': {'profile': profile}, 'data': {'profile': profile, 'origin': '東京駅',
              'destination': '皇居', 'items': [{'distance_m': distance, 'duration_s': duration}],
              'semantic_status': 'DATA'}}
            for profile, distance, duration in [('walking', 1611.3, 1288.6), ('cycling', 1638.6, 816.6)]],
            'semantic_status': 'DATA', 'render_nested_evidence': True},
            [{'provider': 'OSRM', 'canonical_url': 'https://project-osrm.org/'}])

    def test_recorded_route_operands_are_compared_on_direct_and_analysis_paths(self):
        result = self.route_result()
        direct = app.external_tool_summary(result)
        analysis, _, _ = app._compose_autonomous_analysis('徒歩と自転車を比べて', [result])
        for answer in (direct, analysis):
            self.assertIn('+27.3 m', answer)
            self.assertIn('-7.9分', answer)
            self.assertNotIn('統合できる数値系列がない', answer)

    def test_successful_route_composition_does_not_replace_arithmetic_with_model_prose(self):
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('unneeded composition')):
            _, answer = app.compose_external_result('徒歩と自転車の違い', self.route_result())
        self.assertIn('-7.9分', answer)

    def test_one_route_is_not_reported_as_global_shortest(self):
        result = self.route_result()
        result['data'] = result['data']['items'][0]['data']
        result['query'] = '移動距離が短い道順'
        answer = app.external_tool_summary(result)
        self.assertIn('候補1件', answer)
        self.assertIn('全道路での最短距離を保証する結果ではありません', answer)

    def test_nested_uniprot_function_text_survives_composer_boundary(self):
        generation = Mock(return_value={'text': '説明'})
        runtime = SimpleNamespace(settings=SimpleNamespace(main_model='fixture'), _generate_brain=generation,
                                  context_budget=lambda *_args: 4096,
                                  _finish=lambda task, answer, started: (task, answer))
        row = {'primaryAccession': 'P04637', 'comments': [{'commentType': 'FUNCTION', 'texts': [
            {'value': 'Acts as a tumor suppressor.', 'evidences': [{'evidenceCode': 'ECO:0000269'}]}]}]}
        packet = {'tool_id': 'biology.protein', 'provider': 'UniProt', 'data': {'semantic_status': 'DATA',
                  'items': [{'request': {'query': 'TP53'}, 'data': {'semantic_status': 'DATA', 'items': [row]}}]}, 'sources': []}
        Runtime.compose_tool_result(runtime, '基本機能を比べて', packet)
        text = generation.call_args.args[0][-1]['content'].split('[TOOL_RESULT_UNTRUSTED]\n', 1)[1]
        self.assertIn('Acts as a tumor suppressor.', text)

    def test_rdap_missing_registrant_is_not_mapped_to_registrar_or_privacy(self):
        payload = {'ldhName': 'example.org', 'entities': [{'roles': ['registrar'], 'vcardArray': ['vcard',
                   [['fn', {}, 'text', 'ICANN']]]}], 'nameservers': [{'ldhName': 'ns.example.net'}]}
        result = tools._external_result('internet.domain_registration', 'example.org', payload, 'https://rdap.org/')
        answer = app.external_tool_summary(result)
        self.assertIn('registrarロール', answer)
        self.assertIn('公開登録者', answer)
        self.assertIn('空欄だけでは非公開', answer)
        self.assertNotIn('レジストラID 376', answer)

    def test_forecast_is_compared_and_utc_epoch_is_formatted(self):
        result = {'tool_id': 'aviation.operational_weather', 'provider': 'AWC', 'data': {'items': [
            {'icaoId': 'RJTT', 'reportTime': '2026-10-11T11:00:00Z', 'wdir': 40, 'wspd': 10,
             'rawOb': 'METAR RJTT 111100Z 04010KT CAVOK 21/17 Q1018 NOSIG'}], 'forecast': [
            {'rawTAF': 'TAF RJTT 111105Z 1112/1218 01014KT 9999 FEW030', 'fcsts': [
                {'timeFrom': 1791720000, 'timeTo': 1791828000, 'wdir': 10, 'wspd': 14, 'visib': '6+'}]}]}}
        answer = app._new_provider_summary(result)
        self.assertIn('+4.0 kt', answer)
        self.assertIn('変化-30°', answer)
        self.assertNotIn('予報区間 1791720000', answer)
        self.assertIn('視程悪化の兆候はありません', answer)

    def test_no_product_asks_for_product_identifier_on_error_path(self):
        answer = app._external_tool_user_message('ARGUMENT_COMPILATION_FAILED', {}, 'food.product')
        self.assertIn('バーコード', answer)
        self.assertNotIn('場所・対象・期間', answer)

    def test_enthusiast_movie_quiz_withholds_answer_and_requests_hard(self):
        args = tools.compile_provider_arguments('games.trivia', '映画好き向けに、少し難しめのクイズを一つお願い。')
        self.assertTrue(args['hide_answer'])
        self.assertEqual('hard', args['difficulty'])
        self.assertEqual('medium', tools.compile_provider_arguments('games.trivia', '少し難しい科学のクイズ')['difficulty'])
        with patch.object(tools, '_request', return_value={'response_code': 0, 'results': [{
            'question': 'Question', 'correct_answer': 'secret', 'incorrect_answers': ['one', 'two', 'three']}] }):
            result = tools._simple_http('games.trivia', args)
        self.assertNotIn('correct_answer', json.dumps(result['data']))
        self.assertEqual(4, len(result['data']['items'][0]['choices']))

    def test_disaster_region_is_filtered_before_global_result_limit(self):
        with patch.object(tools, '_request', return_value={'events': []}) as request:
            tools._simple_http('earth.natural_event', {'region_bbox': [92, -11, 141, 29],
                               'region': '東南アジア', 'limit': 100})
        self.assertEqual('92,29,141,-11', request.call_args.args[2]['bbox'])

    def test_oakland_compiles_to_prediction_station(self):
        args = tools.compile_provider_arguments('ocean.tides', '同じ日にサンフランシスコとオークランドで潮汐時刻がどれくらい違う？')
        self.assertEqual(['9414290', '9414777'], [request['station'] for request in args['requests']])

    def test_fda_nonserious_reports_are_not_called_serious(self):
        result = {'tool_id': 'health.openfda', 'provider': 'openFDA', 'data': {'items': [{
            'received_date': '20260630', 'serious': '2', 'reactions': ['Headache']} ]}}
        answer = app._new_provider_summary(result)
        self.assertIn('重篤報告0件、非重篤報告1件', answer)
        self.assertIn('実行日直近の事例とは限りません', answer)

    def test_followup_uses_only_recent_user_player_and_ignores_failed_assistant(self):
        with patch.object(app.db, 'conversation', return_value={'messages': [
            {'role': 'user', 'content': 'Nakamuraの最近の黒番対局'},
            {'role': 'assistant', 'content': 'Web検索プロバイダーが未設定です'}]}):
            self.assertEqual('hikaru', app._chess_followup_arguments('彼の最近の対局を比べたい', 'fixture')['username'])
        with patch.object(app.db, 'conversation', return_value={'messages': [
            {'role': 'assistant', 'content': 'Nakamuraの例'}]}):
            self.assertIsNone(app._chess_followup_arguments('彼の最近の対局を比べたい', 'fixture'))

    def test_primary_provider_timeout_is_not_replaced_by_search_key_setup(self):
        task = Task('fixture'); task.transition(TaskState.ROUTING)
        task.error = 'WEB_PROVIDER_NOT_CONFIGURED'; task.transition(TaskState.FAILED)
        with patch.object(app.runtime, 'execute', return_value=(task, 'APIキー発行・料金プラン')):
            _, answer = app._direct_provider_failure_fallback('流量を確認', 'earth.streamflow', 'PROVIDER_TIMEOUT')
        self.assertIn('応答時間内', answer)
        self.assertIn('元の公開APIに検索用APIキーが必要という意味ではありません', answer)
        self.assertNotIn('料金プラン', answer)

    def test_launch_update_renderer_does_not_claim_every_record_was_delayed(self):
        result = {'tool_id': 'space.launches', 'provider': 'LL2', 'data': {'explain_schedule_changes': True,
                  'items': [{'name': 'USSF-481', 'status': 'Go for Launch', 'schedule_updates': [
                    {'comment': 'Added based on navigation warnings', 'created_on': '2026-10-06'}]}]}}
        answer = app._new_provider_summary(result)
        self.assertIn('新規登録やGO状態への変更は延期の証明ではありません', answer)
        self.assertNotIn('すべて', answer)

    def test_quiz_composition_cannot_invent_or_expose_the_answer(self):
        args = tools.compile_provider_arguments('games.trivia', '映画好き向けに、少し難しめのクイズを一つお願い。')
        def provider(host, path, params, *unused):
            if host == 'opentdb.com':
                return {'response_code': 0, 'results': [{'question': 'Movie question',
                         'correct_answer': 'A', 'incorrect_answers': ['B', 'C', 'D']}]}
            return {'responseStatus': 200, 'responseData': {'translatedText':
                    '映画の問題' if params['q'] == 'Movie question' else params['q']}}
        with patch.object(tools, '_request', side_effect=provider):
            result = tools._simple_http('games.trivia', args)
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('quiz must stay typed')):
            task, answer = app.compose_external_result('映画クイズを一つお願い', result)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertNotIn('正解', answer)
        self.assertIn('回答を選んで', answer)
        self.assertTrue(tools.compile_provider_arguments('games.trivia', '科学の四択クイズ。答えを表示せずお願い')['hide_answer'])
        self.assertFalse(tools.compile_provider_arguments('games.trivia', '科学のクイズを答え付きでお願い')['hide_answer'])

    def test_shortest_route_requires_mode_without_breaking_explicit_modes(self):
        with self.assertRaises(tools.ExternalToolError):
            tools.compile_provider_arguments('geo.routing', '東京駅から皇居まで最短の道順')
        walking = tools.compile_provider_arguments('geo.routing', '東京駅から皇居まで徒歩で最短の道順')
        cycling = tools.compile_provider_arguments('geo.routing', '東京駅から皇居まで自転車で最短の道順')
        self.assertEqual('walking', walking['profile'])
        self.assertEqual('cycling', cycling['profile'])
        self.assertNotIn('requests', cycling)

    def test_failed_reverse_lookup_keeps_coordinate_region_outside_atlantic(self):
        def request(host, path, params):
            if host == 'nominatim.openstreetmap.org':
                raise tools.ExternalToolError('PROVIDER_UNAVAILABLE')
            return {'latitude': -30, 'longitude': -140, 'timestamp': 1791720000}
        with patch.object(tools, '_request', side_effect=request), patch.object(tools, '_land_surface', return_value='sea'):
            result = tools._provider_details('space.iss_position', {'request_text': '近い都市や地域を説明'}, '', 1)
        _, answer = app.compose_external_result('今ISSがいる場所を近い都市や地域と説明して', result)
        self.assertIn('南太平洋', answer)
        self.assertIn('近隣都市は未確認', answer)

    def test_mature_protein_renderer_uses_exact_feature_ranges_without_brain(self):
        features = [{'type': kind, 'description': label, 'location': {'start': {'value': a, 'modifier': 'EXACT'},
                     'end': {'value': b, 'modifier': 'EXACT'}}} for kind, label, a, b in
                    [('Signal', 'Signal', 1, 24), ('Chain', 'B chain', 25, 54),
                     ('Propeptide', 'C peptide', 57, 87), ('Chain', 'A chain', 90, 110)]]
        result = tools._external_result('biology.protein', 'INS', {'items': [{'primaryAccession': 'P01308',
                 'sequence_length': 110, 'organism': {'scientificName': 'Homo sapiens'},
                 'processing_features': features}]}, 'https://www.uniprot.org/')
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('must stay typed')):
            task, answer = app.compose_external_result('インスリンの前駆体から成熟タンパク質まで', result)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertIn('前駆体配列長 110', answer)
        self.assertIn('25〜54（30残基）', answer)
        self.assertIn('90〜110（21残基）', answer)

    def test_chess_games_are_rendered_with_pgn_and_length_comparison(self):
        result = tools._external_result('games.chess', 'hikaru', {'username': 'hikaru', 'compare_length': True,
                 'archive_months': 2, 'items': [{'white': {'username': 'opponent'}, 'black': {'username': 'hikaru'},
                 'pgn': '1. e4 e5 2. Nf3', 'ply_count': count, 'end_time': 1791720000,
                 'url': f'https://www.chess.com/game/live/{count}'} for count in (3, 100)]},
                 'https://www.chess.com/member/hikaru')
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('must stay typed')):
            task, answer = app.compose_external_result('最近の短い対局と長いものを比べたい', result)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertIn('黒 hikaru', answer)
        self.assertIn('```pgn', answer)
        self.assertIn('差は97半手', answer)

    def test_disaster_alias_uses_existing_eonet_renderer_without_brain(self):
        result = tools._restore_canonical_result('earth.disasters', tools._external_result('earth.natural_event',
                 '東南アジア', {'current_turn_evidence': True, 'items': [{'title': 'Volcano', 'location': '東南アジア',
                 'date': '2026-10-10', 'geometry': [{'date': '2026-10-11', 'coordinates': [120, 10]}]}]},
                 'https://eonet.gsfc.nasa.gov/'))
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('must stay typed')):
            task, answer = app.compose_external_result('東南アジアの自然災害を説明して', result)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertIn('Volcano', answer)
        self.assertIn('登録観測', answer)
        self.assertIn('現時点で災害が継続', answer)

    def test_court_metadata_does_not_claim_verified_judgment_contents(self):
        result = tools._result('law.us_courts', 'copyright', {'items': [{'title': 'Candidate v. Example',
                 'date_filed': '2026-10-10', 'opinion_texts': [], 'url': 'https://www.courtlistener.com/opinion/123/'}],
                 'request_complete': False, 'order_by': 'dateFiled desc'},
                 [{'provider': tools.REGISTRY['law.us_courts'].provider, 'canonical_url': 'https://www.courtlistener.com/'}])
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('metadata must stay typed')):
            _, answer = app.compose_external_result('著作権とオンラインサービスに関する最近の連邦判決を探して', result)
        self.assertIn('判断内容', answer)
        self.assertIn('未確認', answer)
        self.assertIn('一部の要求項目を取得できていない', answer)

    def test_composition_deadline_is_scoped_and_failure_remains_available_to_renderer(self):
        from olcr_api.ollama import ModelFailure, model_timeout_seconds
        def generate(*args):
            self.assertLessEqual(model_timeout_seconds(750), 60)
            raise ModelFailure('timeout', 'fixture composition timeout')
        runtime = SimpleNamespace(settings=SimpleNamespace(main_model='fixture'), _generate_brain=generate,
                                  context_budget=lambda *_args: 4096,
                                  _finish=lambda task, answer, started: (task, answer))
        task, answer = Runtime.compose_tool_result(runtime, '説明して', {'data': {'fallback_evidence': True, 'items': [{'name': 'record'}]}})
        self.assertEqual(TaskState.FAILED, task.state)
        self.assertEqual('', answer)
        self.assertEqual(750, model_timeout_seconds(750))

    def test_secondary_search_failure_does_not_discard_successful_fetched_evidence(self):
        import olcr_api.runtime as runtime_module
        task = Task('fallback'); task.transition(TaskState.ROUTING); task.transition(TaskState.GENERATING)
        task.transition(TaskState.COMPLETED)
        source = {'final_url': 'https://example.org/evidence', 'text': 'Fetched evidence remains available.',
                  'fetch_success': True}
        with patch.object(app.runtime, 'settings', app.runtime.settings.with_overrides({'web_mode': 'auto', 'web_provider': 'brave'})), \
             patch.object(runtime_module, 'provider_key', return_value='fixture'), \
             patch.object(runtime_module, 'brave_search', side_effect=[[{'url': source['final_url'], 'title': 'Evidence'}], RuntimeError('WEB_SEARCH_PROVIDER_NOT_READY')]), \
             patch.object(runtime_module, 'web_fetch', return_value=source), \
             patch.object(app.runtime, '_news_articles', return_value=[]), \
             patch.object(app.runtime, 'compose_tool_result', return_value=(task, 'Evidence-based answer')) as compose:
            result, answer = app.runtime.execute('河川の資料を探して', api_miss_fallback=True)
        compose.assert_called_once()
        self.assertEqual(TaskState.COMPLETED, result.state)
        self.assertIn('Evidence-based answer', answer)

    def test_stream_routes_provider_failure_through_shared_chat_without_duplicate_turns(self):
        import asyncio
        payload = app.ChatInput(message='映画好き向けにクイズを一つお願い。', project_id=None)
        result = {'response': 'failure disclosure', 'task': {'id': 'task', 'state': 'failed'}, 'conversation_id': 'fixture'}
        with patch.object(app, '_resolve_chat_context', return_value=('fixture', '', None)), \
             patch.object(app, 'chat', return_value=result) as chat:
            response = app.stream_chat(payload)
            async def collect():
                return [frame async for frame in response.body_iterator]
            frames = asyncio.run(collect())
        self.assertEqual('fixture', chat.call_args.args[0].conversation_id)
        self.assertTrue(any('failure disclosure' in frame for frame in frames))
        self.assertTrue(any('"type": "done"' in frame for frame in frames))

    def test_provider_access_denied_message_is_not_sec_specific_for_nasa(self):
        answer = app._external_tool_user_message('PROVIDER_ACCESS_DENIED', tool_id='space.exoplanets')
        self.assertIn('HTTP 403', answer)
        self.assertNotIn('SEC', answer)

    def test_optional_composition_deadline_reaches_mlx_subprocess_timeout(self):
        from pathlib import Path
        import subprocess
        from olcr_api.model_provider import RoleAwareModelProvider
        from olcr_api.config import Settings
        from olcr_api.ollama import inference_timeout
        provider = RoleAwareModelProvider(Settings())
        with inference_timeout(60), patch.object(provider, '_mlx_python', return_value=Path('/tmp/fixture-python')), \
             patch('olcr_api.model_provider.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"text":"answer"}', '')) as run:
            provider._run_mlx('ornith', {'messages': []})
        self.assertLessEqual(run.call_args.kwargs['timeout'], 60)
        self.assertGreater(run.call_args.kwargs['timeout'], 0)

    def test_large_gbif_metadata_cannot_erase_later_observation_identities(self):
        records = [{'key': index, 'eventDate': '2026-10-10', 'scientificName': f'Bird {index}',
                    'decimalLatitude': 35.71, 'decimalLongitude': 139.77, 'unneeded_metadata': 'x' * 24000}
                   for index in range(1, 6)]
        with patch.object(tools, '_request', return_value={'results': records}):
            result = tools._simple_http('biology.occurrence', {'geometry': 'fixture', 'limit': 5})
        self.assertEqual(5, len(result['data']['items']))
        self.assertTrue(all(row.get('scientificName') and row.get('eventDate') for row in result['data']['items']))

    def test_failed_api_composition_keeps_fetched_evidence_at_outer_boundary(self):
        task = Task('fixture'); task.transition(TaskState.ROUTING)
        task.error = 'MLX timeout'; task.transition(TaskState.FAILED)
        task.selected_context = [{'fallback_sources': [{'url': 'https://example.org/source'}]}]
        with patch.object(app.runtime, 'execute', return_value=(task, '取得済み根拠と https://example.org/source')):
            result, answer = app._direct_provider_failure_fallback('惑星を確認', 'space.exoplanets', 'PROVIDER_UNAVAILABLE')
        self.assertIn('取得済み根拠', answer)
        self.assertIn('https://example.org/source', answer)
        self.assertEqual(TaskState.FAILED, result.state)
        self.assertEqual('PROVIDER_UNAVAILABLE', result.error)

    def test_streamflow_units_and_quality_flags_bypass_model_reinterpretation(self):
        result = tools._result('earth.streamflow', 'fixture', {'items': [{
            'station': 'Potomac', 'value': '7660', 'unit': 'ft3/s', 'qualifiers': ['P'],
            'dateTime': '2026-10-12T03:50:00-04:00', 'variable': 'Discharge'}],
            'region': 'Washington DC', 'station_count': 48, 'render_nested_evidence': True},
            [{'provider': tools.REGISTRY['earth.streamflow'].provider, 'canonical_url': 'https://waterdata.usgs.gov/'}])
        with patch.object(app.runtime, 'compose_tool_result', side_effect=AssertionError('must stay typed')):
            _, answer = app.compose_external_result('今流量が大きい河川観測所を確認', result)
        self.assertIn('216.907 m³/s', answer)
        self.assertIn('P：暫定値', answer)
        self.assertNotIn('217,000', answer)
        self.assertIn('03:50:00-04:00', answer)
        nested = {**result, 'data': {'items': [{'data': result['data']}], 'render_nested_evidence': True}}
        self.assertIn('216.907 m³/s', app.external_tool_summary(nested))

    def test_quiz_translates_without_sending_correct_answer_marker(self):
        def request(host, path, params, *args):
            if host == 'opentdb.com':
                return {'response_code': 0, 'results': [{'question': 'Which year?', 'difficulty': 'hard',
                    'correct_answer': '1997', 'incorrect_answers': ['1996', '1998', '1999']}]}
            self.assertEqual('api.mymemory.translated.net', host)
            self.assertEqual('Which year?', params['q'])
            return {'responseStatus': 200, 'responseData': {'translatedText': '何年ですか？'}}
        args = tools.compile_provider_arguments('games.trivia', '映画好き向けに、少し難しめのクイズを一つお願い。')
        with patch.object(tools, '_request', side_effect=request):
            result = tools._simple_http('games.trivia', args)
        self.assertEqual('何年ですか？', result['data']['items'][0]['question'])
        self.assertNotIn('correct_answer', json.dumps(result['data']))
        self.assertIn('上級', app.external_tool_summary(result))

    def test_wrong_quiz_difficulty_is_not_silently_accepted(self):
        with patch.object(tools, '_request', return_value={'response_code': 0, 'results': [{
                'question': 'Easy question', 'difficulty': 'easy', 'correct_answer': 'A', 'incorrect_answers': ['B']}]}):
            with self.assertRaisesRegex(tools.ExternalToolError, 'PROVIDER_NO_RELEVANT_RESULT'):
                tools._simple_http('games.trivia', {'difficulty': 'hard', 'hide_answer': True})

    def test_current_disasters_exclude_closed_stale_and_future_observations(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).isoformat()
        events = [{'title': 'Recent', 'closed': None, 'geometry': [{'date': today}]},
                  {'title': 'Stale', 'closed': None, 'geometry': [{'date': '2020-01-01T00:00:00Z'}]},
                  {'title': 'Closed', 'closed': today, 'geometry': [{'date': today}]},
                  {'title': 'Future', 'closed': None, 'geometry': [{'date': '2099-01-01T00:00:00Z'}]}]
        with patch.object(tools, '_request', return_value={'events': events}):
            result = tools._simple_http('earth.natural_event', {'status': 'open', 'recent_observation_days': 30})
        self.assertEqual(['Recent'], [row['title'] for row in result['data']['items']])
        self.assertEqual(2, result['data']['stale_open_events_excluded'])
        answer = app.external_tool_summary(result)
        self.assertIn('直近30日', answer)
        self.assertIn('最新登録観測日', answer)

    def test_public_court_page_recovers_opinion_body_after_api_auth_failure(self):
        body = 'The court held that copyright is not infringed under these circumstances. ' * 5
        def request(host, path, params, *args):
            if path.endswith('/search/'):
                return {'results': [{'caseName': 'Verified v. Example', 'absolute_url': '/opinion/123/example/',
                                     'opinions': [{'id': 123}], 'dateFiled': '2026-10-10'}]}
            raise tools.ExternalToolError('PROVIDER_ACCESS_DENIED')
        with patch.object(tools, '_request', side_effect=request), patch.object(tools, '_text_request', return_value=
                '<nav>Irrelevant site menu</nav><div id="opinion-content"><p>' + body + '</p></div>'):
            result = tools._simple_http('law.us_courts', {'query': 'copyright'})
        self.assertTrue(result['data']['request_complete'])
        self.assertIn('The court held', result['data']['items'][0]['opinion_texts'][0])
        self.assertNotIn('Irrelevant site menu', result['data']['items'][0]['opinion_texts'][0])
        self.assertIn('公開判決ページから取得', app.external_tool_summary(result))

    def test_court_page_navigation_is_never_misidentified_as_opinion_body(self):
        self.assertEqual('', tools._court_public_opinion_text('<main>' + 'Search result navigation. ' * 100 + '</main>'))
        page = '<div id="opinion-content"><script><span>Bad injected text</span></script><p>' + 'Actual opinion. ' * 20 + '</p></div>'
        self.assertNotIn('Bad injected text', tools._court_public_opinion_text(page))

    def test_translation_failure_does_not_dump_foreign_question(self):
        with patch.object(tools, '_request', return_value={'response_code': 0, 'results': [{
                'question': 'Untranslated question', 'correct_answer': 'A', 'incorrect_answers': ['B']}]}):
            result = tools._simple_http('games.trivia', {'language': 'ja', 'hide_answer': True})
        answer = app.external_tool_summary(result)
        self.assertFalse(result['data']['request_complete'])
        self.assertNotIn('Untranslated question', answer)
        self.assertIn('日本語訳を取得できなかった', answer)

    def test_quiz_respects_selected_response_language_and_explicit_override(self):
        from olcr_api.response_language import response_language_scope
        with response_language_scope('en'):
            self.assertEqual('en', tools.compile_provider_arguments('games.trivia', '映画のクイズ')['language'])
            self.assertEqual('ja', tools.compile_provider_arguments('games.trivia', '日本語で映画のクイズ')['language'])
        result = tools._result('games.trivia', 'quiz', {'language': 'en', 'hide_answer': True,
                               'items': [{'question': 'Question?', 'choices': ['A', 'B'], 'difficulty': 'hard'}]}, [])
        self.assertIn('Quiz (provider difficulty: Hard)', app.external_tool_summary(result))
