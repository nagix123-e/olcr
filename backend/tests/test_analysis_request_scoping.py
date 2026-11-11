import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_autonomous_api_analysis as support
from test_autonomous_api_analysis import _recipe
import olcr_api.app as app


class AnalysisRequestScopingTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def test_all_curated_gui_analysis_prompts_enter_analysis_dispatch(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/multi_api_analysis_cases.json').read_text())['cases']
        missed = [case['id'] for case in cases if not app._autonomous_analysis_requested(case['prompt'])]
        self.assertEqual([], missed)
        self.assertTrue(app._autonomous_analysis_requested('難民データと人口・所得を比較して'))

    def test_user_named_sources_survive_domain_narrowing(self):
        prompt = 'Congress.govの法案とFederal Registerの規則、USAspending.govの支出を組み合わせて比較して'
        selected = app._analysis_candidates_for_domain(prompt, ['econ.jp_estat'])
        self.assertIn('politics.us_congress', selected)
        self.assertIn('policy.us_federal_register', selected)
        self.assertIn('gov.us_spending', selected)
        self.assertNotIn('econ.jp_estat', selected)

    def test_named_source_coverage_survives_a_partial_brain_plan(self):
        prompt = 'World BankとUN Comtradeの所得・貿易データを組み合わせて比較して'
        ids = ['econ.worldbank', 'trade.un_comtrade']
        outputs = {tool: {'tool_id': tool, 'data': {'items': [{'value': 1}]},
                         'sources': [{'canonical_url': 'https://example.invalid/' + tool}]} for tool in ids}
        with patch.object(app, 'laya_provider', SimpleNamespace(loaded=False)), \
             patch.object(app, '_brain_plan_analysis_apis', return_value=([ids[0]], _recipe([ids[0]], request=prompt))), \
             patch.object(app, 'external_tool_credential_setup', return_value=None), \
             patch.object(app, 'compile_provider_arguments', side_effect=lambda tool, request, seed: {'query': request}), \
             patch.object(app, 'execute_external_tool', side_effect=lambda tool, args: outputs[tool]) as execute, \
             patch.object(app, '_compose_autonomous_analysis', return_value=('Combined evidence', [], True)), \
             patch.object(app, 'external_tool_display_blocks', return_value=[]):
            result = app._run_autonomous_analysis(prompt, {'candidate_ids': ids})
        self.assertNotIn('blocked', result)
        self.assertEqual(ids, [call.args[0] for call in execute.call_args_list])

    def test_country_scope_does_not_read_latin_america_as_united_states(self):
        self.assertNotIn('US', app._api_country_regions('ラテンアメリカの雇用'))
        selected = app._analysis_candidates_for_domain('カナダの住宅と雇用を比較して', ['econ.jp_estat', 'econ.us_labor', 'econ.oecd'])
        self.assertNotIn('econ.jp_estat', selected)
        self.assertNotIn('econ.us_labor', selected)

    def test_samples_with_wrong_year_or_region_do_not_count_as_analysis_evidence(self):
        self.assertFalse(app._analysis_result_matches_scope('2025年の自然災害', {'items': [{'date': '2026-10-20'}]}))
        self.assertFalse(app._analysis_result_matches_scope('ラテンアメリカの紛争', {'items': [{'region': 'Africa'}]}))
        self.assertTrue(app._analysis_result_matches_scope('2025年の自然災害', {'items': [{'date': '2025-06-20'}]}))
        self.assertTrue(app._analysis_result_matches_scope('2025年の自然災害', {'items': [{'value': 0}]}))
        self.assertFalse(app._analysis_result_matches_scope('2025年の自然災害', {'items': []}))

    def test_complementary_clauses_are_preserved_even_without_provider_names(self):
        prompt = '最近の地震活動とカリフォルニアの大気質を組み合わせて分析して'
        required = app._requested_analysis_sources(prompt)
        self.assertIn('earth.earthquakes', required)
        self.assertIn('environment.air_quality', required)

    def test_world_bank_retains_all_requested_indicators(self):
        arguments = app.compile_provider_arguments('econ.worldbank',
            '2025年の日本のGDP成長率、失業率、インフレ率を比較して', {})
        self.assertEqual({'NY.GDP.MKTP.KD.ZG', 'SL.UEM.TOTL.ZS', 'FP.CPI.TOTL.ZG'}, set(arguments['indicators']))
        access = app.compile_provider_arguments('econ.worldbank',
            '南アジアの電力アクセスと再生可能電力比率を比較して', {})
        self.assertEqual({'EG.ELC.RNEW.ZS', 'EG.ELC.ACCS.ZS'}, set(access['indicators']))

    def test_model_written_angle_cannot_replace_the_user_scope(self):
        arguments = app.compile_provider_arguments('econ.worldbank',
            '2025年の日本のGDP成長率を調べて\n\nSource-specific retrieval instruction: 2024年の米国の失業率を取得', {})
        self.assertEqual(['JP'], arguments['countries'])
        self.assertEqual('NY.GDP.MKTP.KD.ZG', arguments['indicator'])
        self.assertEqual(2025, arguments['start_year'])
        self.assertNotIn('SL.UEM.TOTL.ZS', arguments.get('indicators', []))

    def test_same_chat_selects_history_by_current_subject(self):
        for role, text in [('user', 'Explain NOAA geomagnetic Kp observations'),
                           ('assistant', 'NOAA geomagnetic Kp observations cover three hours.'),
                           ('user', 'Compare CPI inflation rates'),
                           ('assistant', 'CPI inflation rates rose by 2 percent.')]:
            app.db.add_message('conversation', role, text, time.time(), str(time.time_ns()))
        selected = app.active_conversation_context('conversation', request='NOAA geomagnetic observations')
        self.assertIn('NOAA', selected)
        self.assertNotIn('CPI', selected)
        self.assertEqual('', app.runtime._self_context('発電モデルの構成を比較して'))

    def test_retrieval_settings_patch_preserves_other_settings_and_rejects_bad_budget(self):
        original = app.settings.computer_use_enabled
        result = self.client.patch('/api/settings/retrieval', json={
            'context_budget': 4096, 'rag_min_relevance': .7, 'model_context_budgets': {'mlx:small': 2048}})
        self.assertEqual(200, result.status_code)
        self.assertEqual(original, app.settings.computer_use_enabled)
        self.assertEqual(.7, app.settings.rag_min_relevance)
        invalid = self.client.patch('/api/settings/retrieval', json={'model_context_budgets': {'mlx:small': 0}})
        self.assertEqual(422, invalid.status_code)
        self.assertEqual({'mlx:small': 2048}, app.settings.model_context_budgets)
