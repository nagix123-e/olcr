"""Failures found by the 32-turn, real GUI/API r08 execution."""
import copy
import json
import unittest
from unittest.mock import patch

from olcr_api import external_tools as tools
from olcr_api.analysis_presentation import bounded_lines, display_result
from olcr_api.analysis_coverage import evidence_needs, missing_coverage
from olcr_api.evidence_quality import recovery_queries, web_source_matches as source_relevant


class ProviderContractTests(unittest.TestCase):
    def test_unspecified_fx_counterpart_uses_major_pair_and_panel_is_explicit(self):
        args = tools.compile_provider_arguments('currency.frankfurter', '日本の輸出入と為替の動向を調べて')
        self.assertEqual('JPY', args['base'])
        self.assertEqual('USD', args['quote'])
        natural = tools.compile_provider_arguments('currency.frankfurter','2026-08-01から2026-10-01のJPY USDの為替動向を比較して')
        self.assertEqual(('JPY','USD'),(natural['base'],natural['quote']))
        payload = [{'date': '2026-01-01', 'base': 'JPY', 'quote': 'USD', 'rate': .006},
                   {'date': '2026-02-01', 'base': 'JPY', 'quote': 'USD', 'rate': .007}]
        with patch.object(tools, '_request', return_value=payload) as wire:
            result = tools.execute('currency.frankfurter', args)
        self.assertEqual('/v2/rates', wire.call_args.args[1])
        self.assertEqual('USD', wire.call_args.args[2]['quotes'])
        self.assertEqual('USD/JPY', result['data']['items'][0]['unit'])
        panel_args = tools.compile_provider_arguments('currency.frankfurter', '日本の全通貨の為替動向を調べて')
        self.assertIsNone(panel_args['quote'])
        with patch.object(tools, '_request', return_value=payload) as wire:
            tools.execute('currency.frankfurter', panel_args)
        self.assertNotIn('quotes', wire.call_args.args[2])

    def test_explicit_fx_pair_and_conversion_remain_compatible(self):
        args = tools.compile_provider_arguments('currency.frankfurter', '100 USDをJPYに換算', {'base': 'USD', 'quote': 'JPY', 'amount': 100})
        with patch.object(tools, '_request', return_value={'base': 'USD', 'quote': 'JPY', 'rate': 150, 'date': '2026-01-01'}) as wire:
            data = tools.execute('currency.frankfurter', args)['data']
        self.assertEqual('/v2/rate/USD/JPY', wire.call_args.args[1])
        self.assertEqual('15000', data['converted_amount'])

    def test_greenhouse_gases_are_not_replaced_by_co2_only(self):
        ghg = tools.compile_provider_arguments('econ.worldbank', '日本の温室効果ガス排出の変化を比較して')
        co2 = tools.compile_provider_arguments('econ.worldbank', '日本のCO2排出量を調べて')
        self.assertIn('EN.GHG.ALL.MT.CE.AR5', ghg.get('indicators') or [ghg['indicator']])
        self.assertIn('EN.GHG.CO2.MT.CE.AR5', co2.get('indicators') or [co2['indicator']])

    def test_country_trial_overview_does_not_send_policy_as_a_disease(self):
        text = '英国議会の医療政策議論と臨床試験の動向を整理して'
        args = tools.compile_provider_arguments('health.clinical_trials', text, {'query': text})
        self.assertEqual('United Kingdom', args['location'])
        self.assertEqual('', args['condition'])
        with patch.object(tools, '_request', return_value={'studies': []}) as wire:
            tools.execute('health.clinical_trials', args)
        params = wire.call_args.args[2]
        self.assertNotIn('query.cond', params)
        self.assertEqual('United Kingdom', params['query.locn'])

    def test_sanctions_catalogue_does_not_search_for_the_whole_instruction(self):
        text = '国連と米国の制裁リストの登録内容を比較し、紛争関連性を調べて'
        xml = b'<ROOT><INDIVIDUAL><FIRST_NAME>Example</FIRST_NAME></INDIVIDUAL></ROOT>'
        for tool in ('sanctions.unsc', 'sanctions.us_ofac'):
            args = tools.compile_provider_arguments(tool, text, {'query': text})
            self.assertEqual('', args['query'])
        self.assertEqual('DPRK', tools.compile_provider_arguments('sanctions.unsc', '北朝鮮の制裁対象を比較して')['query'])

    def test_hdx_nested_catalogue_retains_records_instead_of_only_success_flag(self):
        payload = {'success': True, 'result': {'count': 1, 'results': [{'title': 'Ukraine humanitarian needs', 'notes': 'Needs assessment'}]}}
        result = tools._external_result('humanitarian.hdx', 'Ukraine', payload, 'https://data.humdata.org/')
        self.assertEqual('Ukraine humanitarian needs', result['data']['items'][0]['title'])
        self.assertEqual('dataset_catalog', result['data']['evidence_kind'])
        self.assertFalse(result['data']['request_complete'])
        args=tools.compile_provider_arguments('humanitarian.hdx','ウクライナの人道支援状況を調べて')
        self.assertEqual('Ukraine',args['query'])

    def test_federal_spending_uses_financial_accounts_not_award_totals(self):
        args = tools.compile_provider_arguments('gov.us_spending', '2024年の米国の雇用・物価と連邦政府支出を比較して')
        self.assertTrue(args['aggregate_spending'])
        with patch.object(tools, '_post_request', return_value={'total': 123, 'end_date': '2024-09-30', 'results': []}) as wire:
            result = tools.execute('gov.us_spending', args)
        self.assertEqual('/api/v2/spending/', wire.call_args.args[1])
        self.assertEqual({'type': 'agency', 'filters': {'fy': '2024', 'quarter': '4'}}, wire.call_args.args[2])
        self.assertEqual(123, result['data']['items'][0]['value'])
        self.assertTrue(result['data']['fiscal_year_basis'])
        self.assertEqual('financial_account_obligations', result['data']['evidence_kind'])
        self.assertTrue(result['data']['request_complete'])
        with patch.object(tools, '_post_request', return_value={'total': 123, 'end_date': '2024-06-30'}):
            self.assertFalse(tools.execute('gov.us_spending', args)['data']['request_complete'])
        args = tools.compile_provider_arguments('gov.us_spending', '2024年の米国の連邦政府支出と実支出を確認して')
        with patch.object(tools, '_post_request', return_value={'total': 123, 'end_date': '2024-09-30'}):
            self.assertFalse(tools.execute('gov.us_spending', args)['data']['request_complete'])

    def test_uk_debate_search_uses_hansard_and_keeps_bill_search_separate(self):
        args = tools.compile_provider_arguments('politics.uk_parliament', '英国議会の医療政策議論を整理して')
        self.assertFalse(args.get('unsupported_capability', False))
        payload = {'Results': [{'Title': 'Health debate', 'SittingDate': '2026-01-01', 'House': 'Commons'}]}
        with patch.object(tools, '_request', return_value=payload) as wire:
            result = tools.execute('politics.uk_parliament', args)
        self.assertEqual(('hansard-api.parliament.uk', '/search/debates.json'), wire.call_args.args[:2])
        self.assertEqual('Health debate', result['data']['items'][0]['title'])
        self.assertFalse(result['data']['request_complete'])

    def test_canadian_proceedings_are_not_politician_name_search(self):
        args = tools.compile_provider_arguments('politics.ca_openparliament', 'カナダ議会の審議と経済指標を照らして')
        payload = {'objects': [{'time': '2026-01-01', 'attribution': {'en': 'Speaker'}, 'content': {'en': '<p>Economic discussion.</p>'}}]}
        with patch.object(tools, '_request', return_value=payload) as wire:
            result = tools.execute('politics.ca_openparliament', args)
        self.assertEqual('/speeches/', wire.call_args.args[1])
        self.assertIn('Economic discussion.', result['data']['items'][0]['speech_excerpt'])

    def test_github_rate_limit_is_not_retried_as_an_auth_failure(self):
        with patch.dict('os.environ', {'OLCR_GITHUB_TOKEN': 'test-token'}), patch.object(tools, '_request', side_effect=tools.ExternalToolError('PROVIDER_RATE_LIMITED', http_status=403)) as wire:
            with self.assertRaises(tools.ExternalToolError):
                tools.execute('dev.github', {'endpoint': 'search', 'query': 'agent'})
        self.assertEqual(1, wire.call_count)

    def test_power_generation_uses_eia_fuels_and_preserves_monthly_granularity(self):
        args = tools.compile_provider_arguments('energy.us_eia', '2024年の米国の電力構成と大気質の季節的傾向を整理して')
        self.assertEqual('electricity_generation', args['dataset'])
        self.assertEqual('monthly', args['frequency'])
        self.assertIn('energy.us_eia', next(need['sources'] for need in evidence_needs('米国の電力構成を比較して') if need['key']=='electricity'))
        payload = {'response': {'total': '2', 'data': [
            {'period':'2024-01','location':'US','sectorid':'99','fueltypeid':'ALL','fuelTypeDescription':'all fuels','generation':'500','generation-units':'thousand MWh'},
            {'period':'2024-01','location':'US','sectorid':'99','fueltypeid':'NG','fuelTypeDescription':'natural gas','generation':'200','generation-units':'thousand MWh'}]}}
        with patch.dict('os.environ', {'OLCR_EIA_API_KEY':'test-key'}), patch.object(tools,'_request',return_value=payload) as wire:
            result=tools.execute('energy.us_eia',args)
        self.assertEqual('/v2/electricity/electric-power-operational-data/data/',wire.call_args.args[1])
        self.assertEqual('monthly',wire.call_args.args[2]['frequency'])
        self.assertEqual(2,len({row['indicator'] for row in result['data']['items']}))
        self.assertFalse(result['data']['request_complete'])

    def test_air_quality_area_is_not_assigned_an_arbitrary_station(self):
        args = tools.compile_provider_arguments('environment.air_quality', '米国の電力構成と大気質の季節的傾向を整理して')
        self.assertTrue(args['unsupported_capability'])
        self.assertEqual('United States', args['location'])


class EvidencePresentationTests(unittest.TestCase):
    def test_display_selection_preserves_metrics_countries_and_raw_evidence(self):
        rows = [{'country': c, 'indicator': m, 'date': str(y), 'value': y} for m in ('A','B') for c in ('JP','US') for y in range(2000,2026)]
        result = {'data': {'items': rows, 'raw_metadata': {'duplicate': rows}}}
        original = copy.deepcopy(result)
        sampled = display_result(result, row_limit=4)
        shown = sampled['data']['items']
        self.assertEqual({('A','JP'),('A','US'),('B','JP'),('B','US')}, {(x['indicator'],x['country']) for x in shown})
        self.assertTrue(all(x['date']=='2025' for x in shown))
        self.assertTrue(sampled['data']['display_truncated'])
        self.assertEqual(original, result)

    def test_evidence_bound_keeps_qualifications_and_intact_numeric_lines(self):
        text = '\n'.join(f'Observation {n}: 123456.789 USD' for n in range(500)) + '\nOnly a sample; no causal conclusion.'
        shown = bounded_lines(text, 1200)
        self.assertLessEqual(len(shown), 1200)
        self.assertIn('Only a sample; no causal conclusion.', shown)
        self.assertTrue(all('123456.789 USD' in line for line in shown.splitlines() if line.startswith('Observation')))

    def test_japanese_economic_request_accepts_english_primary_evidence(self):
        self.assertTrue(source_relevant('日本の地域別人口と雇用を比較して', {'title':'Japan regional population and employment statistics','text':'Population estimates and employment by prefecture.'}))
        self.assertFalse(source_relevant('日本の地域別人口と雇用を比較して', {'title':'United States population statistics','text':'Population in the United States.'}))
        self.assertFalse(source_relevant('日本の地域別人口と雇用を比較して', {'title':'Furniture catalogue','text':'Chairs and tables.'}))

    def test_annual_pollution_cannot_claim_seasonal_coverage(self):
        result={'tool_id':'econ.worldbank','data':{'items':[{'indicator':'EN.ATM.PM25.MC.M3','country':'United States','date':'2024','value':8}]}}
        missing=missing_coverage('米国の大気質の季節的傾向を比較して', {'statistics.world_bank'}, lambda x: tools.CANONICAL_ALIASES.get(x,x), [result])
        self.assertIn('air',{x['key'] for x in missing})

    def test_eia_multiyear_summary_keeps_all_requested_years_and_seasonal_means(self):
        evidence = {'tool_id':'energy.us_eia','data':{'items':[
            {'date':str(year),'series':'generation','value':value,'unit':'MWh'}
            for year,value in ((2022,100),(2023,110),(2024,120))]}}
        text=app._deterministic_eia_analysis([evidence],'2022年から2024年の米国の発電量')
        self.assertIn('2024の120',text)
        self.assertIn('差 +20',text)
        evidence['data']={'dataset':'electricity_generation','frequency':'monthly','items':[
            {'date':'2024-01','fuel':'gas','value':100,'unit':'MWh'},
            {'date':'2024-02','fuel':'gas','value':200,'unit':'MWh'}]}
        text=app._deterministic_eia_analysis([evidence],'米国の発電の季節傾向')
        self.assertIn('月平均 150 MWh',text)
        self.assertIn('2か月',text)
        self.assertIn('欠測月は補完していません',text)

    def test_recovery_searches_separate_missing_dimensions(self):
        needs = evidence_needs('米国の雇用・物価と連邦政府支出を比較して')
        queries = recovery_queries('米国の雇用・物価と連邦政府支出を比較して', needs)
        self.assertGreater(len(queries), 1)
        self.assertTrue(any('consumer price inflation' in q for _,q in queries))
        self.assertTrue(any('federal spending outlays' in q for _,q in queries))


import test_autonomous_api_analysis as support
import olcr_api.app as app
from olcr_api.models import Task, TaskState, Route


class AnalysisPipelineTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def test_worldwide_output_is_bounded_without_dropping_raw_observations(self):
        rows = [{'country': f'Area {n}', 'indicator': 'SP.POP.TOTL', 'date': str(y), 'value': n*100+y} for n in range(250) for y in (2021,2024)]
        result = {'tool_id': 'econ.worldbank', 'provider': 'World Bank', 'data': {'items': rows, 'indicator':'SP.POP.TOTL'}, 'sources':[{'title':'WB','canonical_url':'https://data.worldbank.org/'}]}
        with patch.object(app.runtime, 'context_budget', return_value=4000):
            response, sources, _ = app._compose_autonomous_analysis('各国の人口変化を比較して', [result])
        self.assertLess(len(response), 4500)
        self.assertIn('省略', response)
        self.assertEqual(500, len(result['data']['items']))
        self.assertTrue(sources)

    def test_final_inference_keeps_selected_role_and_bounds_generation(self):
        with patch.object(app.runtime, '_generate_brain', return_value={'text': '一般的な説明です。'}) as model:
            task, answer = app.runtime.infer_without_sources('気温と消費の関係', failure_reason='NO_EVIDENCE')
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertEqual(app.settings.planner_model, model.call_args.kwargs['model_name'])
        self.assertFalse(model.call_args.kwargs['thinking_override'])
        self.assertLessEqual(model.call_args.kwargs['max_tokens'], 384)
        self.assertIn('未確認', answer)

    def test_one_recovery_failure_cannot_skip_other_missing_dimensions(self):
        task = Task('recovery'); task.transition(TaskState.ROUTING); task.route=Route.RETRIEVAL
        task.transition(TaskState.SEARCHING); task.transition(TaskState.COMPLETED)
        task.selected_context=[{'fallback_sources':[{'title':'Spend','url':'https://example.invalid/spend','text':'Verified expenditure.'}]}]
        with patch.object(app.runtime,'execute',side_effect=[RuntimeError('unavailable'),(task,'Verified expenditure.')]) as recovery:
            recovered, answer = app._recover_analysis_evidence('米国の雇用と支出', [{'key':'employment','label':'雇用'},{'key':'spending','label':'支出'}])
        self.assertEqual(2,recovery.call_count)
        self.assertIn('Verified expenditure',answer)
        self.assertIn('https://example.invalid/spend',json.dumps(recovered.selected_context))

    def test_unhcr_normalized_rows_are_displayed_as_their_actual_measure(self):
        response=app.external_tool_summary({'tool_id':'humanitarian.unhcr','provider':'UNHCR','data':{'items':[{'country':'Ukraine','year':'2025','indicator':'idps','value':3712000,'country_dimension':'coo'}]}})
        self.assertIn('3712000',response)
        self.assertIn('国内避難民',response)
        self.assertNotIn('確認できませんでした',response)
