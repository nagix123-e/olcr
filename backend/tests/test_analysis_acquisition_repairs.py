"""Acquisition counterexamples: independent scopes, null slots and recovery."""
import json
import unittest
from unittest.mock import patch

import test_autonomous_api_analysis as support
from olcr_api import app, external_tools as tools
from olcr_api.analysis_coverage import missing_coverage
from olcr_api.analysis_scope import filter_scope
from olcr_api.analysis_recovery import recovery_requests


class AcquisitionRepairTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def result(self, rows, tool="econ.worldbank", **meta):
        return {"tool_id": tool, "provider": tool,
                "data": {"items": rows, "has_payload": bool(rows), "semantic_status": "DATA", **meta},
                "sources": [{"canonical_url": "https://data.worldbank.org/"}]}

    def test_country_foreground_does_not_filter_international_background(self):
        text = "米国の物価を確認し、各国GDP成長率も背景として比較して"
        args = tools.compile_provider_arguments("econ.worldbank", text)
        groups = {r["indicator"]:r["countries"] for r in args["requests"]}
        self.assertEqual(["all"], groups["NY.GDP.MKTP.KD.ZG"])
        self.assertEqual(["US"], groups["FP.CPI.TOTL.ZG"])
        rows = [{"country":"France", "date":"2024", "indicator":{"id":"NY.GDP.MKTP.KD.ZG"}, "value":1},
                {"country":"France", "date":"2024", "indicator":{"id":"FP.CPI.TOTL.ZG"}, "value":2}]
        filtered, rejected = filter_scope({"items":rows},text)
        self.assertEqual(1,rejected)
        self.assertEqual("NY.GDP.MKTP.KD.ZG",filtered["items"][0]["indicator"]["id"])

    def test_latest_values_use_nonempty_observations_and_explicit_dates_do_not_backfill(self):
        payload=[{"pages":1},[{"country":{"id":"JP","value":"Japan"},"countryiso3code":"JPN","date":"2020","indicator":{"id":"ST.INT.ARVL"},"value":40}]]
        with patch.object(tools,"_request",return_value=payload) as fetch:
            result=tools.execute("econ.worldbank",{"countries":["JP"],"indicator":"ST.INT.ARVL","limit":3})
            self.assertEqual(3,fetch.call_args.args[2]["mrnev"])
            self.assertEqual("2020",result["data"]["items"][0]["date"])
        with patch.object(tools,"_request",return_value=payload) as fetch:
            tools.execute("econ.worldbank",{"countries":["JP"],"indicator":"ST.INT.ARVL","limit":3,"start_year":2023,"end_year":2024})
            self.assertNotIn("mrnev",fetch.call_args.args[2])
            self.assertEqual("2023:2024",fetch.call_args.args[2]["date"])

    def test_missing_key_recovers_measure_without_downgrading_to_failed_optional_provider(self):
        text="米国の雇用と物価の変化を比較して"
        labor=self.result([{"country":"United States","date":str(y),"value":v,"indicator":"unemployment"} for y,v in [(2023,4),(2024,5)]],"econ.us_labor")
        inflation=self.result([{"country":"United States","countryiso3code":"USA","date":str(y),"value":v,"indicator":{"id":"FP.CPI.TOTL.ZG"}} for y,v in [(2023,3),(2024,2)]],indicator="FP.CPI.TOTL.ZG")
        def execute(tool,args):
            if tool=="econ.us_labor":return labor
            self.assertEqual(["FP.CPI.TOTL.ZG"],args["indicators"])
            return inflation
        with patch.object(app,"_laya_select_analysis_apis",return_value=["econ.fred","econ.us_labor"]), \
             patch.object(app,"_brain_plan_analysis_apis",return_value=(["econ.fred","econ.us_labor"],support._recipe(["econ.fred","econ.us_labor"]))), \
             patch.object(app,"external_tool_credential_setup",side_effect=lambda t:{"tool_id":t,"provider":"FRED"} if t=="econ.fred" else None), \
             patch.object(app,"execute_external_tool",side_effect=execute) as call, \
             patch.object(app,"_recover_analysis_evidence",side_effect=AssertionError("measure recovered")):
            outcome=app._run_autonomous_analysis(text,{})
        self.assertEqual(2,call.call_count)
        self.assertEqual("complete",outcome["tool"]["analysis_status"])
        self.assertIn("acquisition_recovery",outcome["tool"]["results"][-1])

    def test_recovery_does_not_repeat_attempts_or_substitute_annual_for_seasonal(self):
        need=[{"key":"air","sources":["environment.air_quality"]}]
        attempted=[{"tool_id":"econ.worldbank","arguments":{"indicator":"EN.ATM.PM25.MC.M3"}}]
        self.assertEqual([],recovery_requests("日本の大気汚染を比較",[],need,tools.compile_provider_arguments,attempted))
        self.assertEqual([],recovery_requests("日本の大気汚染を季節別に比較",[],need,tools.compile_provider_arguments))
        self.assertEqual([],recovery_requests("World Bankだけで日本の大気汚染を比較",[],need,tools.compile_provider_arguments))

    def test_eurostat_price_and_cpi_are_independent_requests_and_measures(self):
        text="EUの電力価格と消費者物価を比較して"
        args=tools.compile_provider_arguments("econ.eurostat",text)
        self.assertEqual({"nrg_pc_204","prc_hicp_manr"},{r["dataset"] for r in args["requests"]})
        cpi=self.result([{"country":"Germany","geo":"DE","date":"2024","value":2,"indicator":"hicp"}],"econ.eurostat")
        gaps=missing_coverage(text,{"econ.eurostat"},lambda t:tools.CANONICAL_ALIASES.get(t,t),[cpi])
        self.assertTrue(any(gap["key"]=="electricity" for gap in gaps))

    def test_typed_eurostat_series_survive_generic_150_row_budget(self):
        payload={"label":"Electricity prices","rows":[{"time":f"{2000+i//2}-S{1+i%2}","geo":"DE","value":i,"unit":"KWH","currency":"EUR"} for i in range(180)]}
        with patch.object(tools,"_provider_records",return_value=payload["rows"]):
            result=tools._external_result("econ.eurostat","prices",payload,"https://ec.europa.eu/eurostat/")
        self.assertEqual(180,len(result["data"]["items"]))
        self.assertTrue(all(r["unit"]==" EUR/kWh" for r in result["data"]["items"]))

    def test_law_body_is_not_limited_to_privacy_laws(self):
        listing=b'<DataRoot><LawNameListInfo><LawName>Artificial intelligence act</LawName><LawId>LAW1</LawId></LawNameListInfo></DataRoot>'
        body=b'<Law><LawBody><MainProvision><Article Num="1"><ArticleTitle>Article 1</ArticleTitle><Paragraph><Sentence>Public bodies must publish an implementation report.</Sentence></Paragraph></Article></MainProvision></LawBody></Law>'
        with patch.object(tools,"_xml_request",side_effect=[listing,body]):
            result=tools.execute("japan.law",{"query":"intelligence","include_text":True,"limit":2})
        self.assertIn("must publish",result["data"]["items"][0]["text"])
        self.assertEqual("law_text_excerpt",result["data"]["evidence_kind"])

    def test_solar_observations_are_not_only_a_geomagnetic_proxy(self):
        kp=[["time_tag","Kp"],["2025-01-01 00:00","2"]]
        solar=[{"time-tag":"2025-01","ssn":100,"f10.7":150}]
        with patch.object(tools,"_request",side_effect=[kp,solar]):
            result=tools.execute("space.space_weather",{"include_solar":True,"limit":5})
        from olcr_api.analysis_recovery import flattened_results
        leaves=list(flattened_results([result]))
        rows=[r for leaf in leaves for r in leaf['data']['items']]
        self.assertEqual({"geomagnetic_kp","solar_ssn","solar_f10.7"},{r["indicator"] for r in rows})
        self.assertEqual({"monthly","3hourly"},{r["frequency"] for r in rows})
        self.assertEqual(2,len(result['data']['items']))
        self.assertNotEqual(result['data']['items'][0]['sources'][0]['canonical_url'], result['data']['items'][1]['sources'][0]['canonical_url'])
        with patch.object(app.runtime,'_generate_brain',side_effect=AssertionError('numeric synthesis is deterministic')):
            answer,_,_=app._compose_autonomous_analysis('太陽活動と宇宙天気の変化を説明',[result],{})
        solar_line=next(line for line in answer.split('## 分析',1)[1].splitlines() if '太陽黒点数' in line)
        self.assertIn('observed-solar-cycle-indices.json',solar_line)
        self.assertNotIn('noaa-planetary-k-index.json',solar_line)
        self.assertNotIn('1国・地域',answer)

    def test_noaa_dict_records_and_header_tables_share_the_same_decoder(self):
        solar=[{'time-tag':'2025-01','ssn':100,'f10.7':150}]
        for kp in ([["time_tag","Kp"],["2025-01-01 00:00",2]], [{'time_tag':'2025-01-01 00:00','Kp':2}]):
            with self.subTest(shape=type(kp[0]).__name__), patch.object(tools,'_request',side_effect=[kp,solar]):
                result=tools.execute('space.space_weather',{'include_solar':True,'limit':5})
            self.assertTrue(result['data']['request_complete'])
            self.assertEqual(2,result['data']['items'][0]['data']['items'][0]['value'])
            packet=result['data']['items'][1]
            summary=app.external_tool_summary({'tool_id':'space.space_weather','provider':'NOAA','data':packet['data']})
            self.assertIn('月平均太陽黒点数',summary)
            self.assertNotIn('Kp指数を確認できる観測値がありません',summary)

    def test_a_recovered_inflation_value_does_not_validate_fiscal_calendar_alignment(self):
        from olcr_api.analysis_semantics import analytical_gaps
        macro=self.result([{"country":"United States","date":"2024","indicator":"inflation","value":3}])
        spending=self.result([{"country":"United States","date":"2024","indicator":"obligations","value":10,"fiscal_year":2024}],"gov.us_spending",fiscal_year_basis=True)
        gaps=analytical_gaps("雇用と物価と支出の変化を比較",[macro,spending])
        self.assertIn("comparison_alignment",{gap["key"] for gap in gaps})

    def test_live_driver_uses_final_request_context_for_credential_failures(self):
        from scripts.analysis_chat_live_e2e import case_outcome
        task={"state":"completed","selected_context":[{"analysis_status":"partial"},
            {"analysis_status":"unverified","analysis_request_id":"request","credentials_missing":["energy.us_eia"]}]}
        result=case_outcome(task,"done",200,"不足を明示")
        self.assertEqual("unverified",result["analysis_status"])
        self.assertEqual(["energy.us_eia"],result["credentials_missing"])

    def test_prefectural_adapter_keeps_geography_units_and_observation_sources(self):
        from olcr_api import regional_statistics as region
        pop, jobs = '0201010000000010000', '0301100000010010000'
        meta={'GET_META_REGION_INF':{'RESULT':{'status':'0'},'METADATA_INF':{'CLASS_INF':{'CLASS_OBJ':{'CLASS':[
            {'@regionCode':'13000','@name':'東京都','@level':'3','@toDate':'999912'}]}}}}}
        values=[]
        for code, metric, year, value, stat in [('13000',pop,2015,'100','20020101'),('13000',pop,2020,'110','20020101'),
                ('13000',pop,2025,'111','20020101'),('13000',jobs,2015,'50','00200502'),('13000',jobs,2020,'55','00200502'),
                ('00000',pop,2020,'999999','20020101')]:
            values.append({'VALUE':{'@regionCode':code,'@indicator':metric,'@time':f'{year}CY00','@unit':'p',
                '@stat':stat,'@regionRank':'3','@isSeasonal':'1','@isProvisional':'0','$':value}})
        data={'GET_STATS':{'RESULT':{'status':'0'},'STATISTICAL_DATA':{'CLASS_INF':{'CLASS_OBJ':[
            {'@id':'regionCode','CLASS':[{'@code':'13000','@name':'東京都'}]}, {'@id':'unit','CLASS':[{'@code':'p','$':'人'}]}]},
            'DATA_INF':{'DATA_OBJ':values}}}}
        with patch.object(region,'prefecture_indicator',side_effect=[pop,jobs]):
            result,_=region.retrieve_prefectures({'population':True,'employment':True,'trend':True},lambda host,path,args:meta if path.endswith('getRegionInfo') else data)
        self.assertEqual(5,len(result['items']))
        self.assertTrue(result['request_complete'])
        self.assertEqual({'20020101','00200502'},{r['source_statistic'] for r in result['items']})
        self.assertEqual({'jp_regional_population','jp_regional_employment'},{r['indicator'] for r in result['items']})
        self.assertNotIn(999999,[r['value'] for r in result['items']])
        summary=app.external_tool_summary({'tool_id':'econ.jp_estat','provider':'e-Stat','data':result})
        self.assertIn('都道府県別人口・就業者数',summary)
        self.assertNotIn('消費者物価指数',summary)

    def test_partial_child_evidence_is_not_discarded_by_comparison_fanout(self):
        def child(tool,args):
            return tools._result(tool,'scope',{'items':[{'date':'2024','value':1}],
                'semantic_status':'PARTIAL' if args['country']=='JP' else 'DATA',
                'request_complete':args['country']!='JP'},[{'canonical_url':'https://data.worldbank.org/'}])
        with patch.object(tools,'_simple_http',side_effect=child):
            result=tools.execute('econ.worldbank',{'requests':[{'country':'JP'},{'country':'DE'}]})
        self.assertEqual(2,len(result['data']['items']))
        self.assertFalse(result['data']['request_complete'])
        self.assertEqual(1,len(result['data']['missing_requests']))

    def test_composition_uses_same_year_total_and_never_normalizes_top_five_to_100(self):
        from olcr_api.analysis_semantics import trade_composition
        rows=[{'reporter':'Japan','partner':'World','year':2024,'flow_code':'X','commodity_code':c,'commodity':c,'trade_value_usd':v}
              for c,v in [('TOTAL',100),('01',30),('02',20)]]
        rows.append({**rows[0],'year':2023,'trade_value_usd':1000})
        evidence=self.result(rows,'trade.un_comtrade');evidence['sources'][0]['source_id']='S1'
        answer=trade_composition([evidence])
        self.assertIn('| 01 | 01 | 30 | 30.00 |',answer)
        self.assertIn('| 02 | 02 | 20 | 20.00 |',answer)
        self.assertNotIn('60.00',answer)
        compiled=tools.compile_provider_arguments('trade.un_comtrade','日本の貿易構成を比較')
        self.assertEqual('AG2,TOTAL',compiled['commodity_code'])

    def test_linked_document_fetch_retains_catalog_after_one_failure_and_its_own_provenance(self):
        from olcr_api.analysis_documents import materialize_documents
        original=self.result([{'title':'Climate rule A','url':'https://example.test/a'},
                              {'title':'Climate rule B','url':'https://example.test/b'}], 'law.example', evidence_kind='document_catalog')
        page={'text':'Climate change rule. '*50,'content_type':'text/html','final_url':'https://example.test/b','timestamp':1}
        with patch('builtins.print'):
            results,trace=materialize_documents('気候政策を比較',[original],patch_fetch:=unittest.mock.Mock(side_effect=[ValueError('unavailable'),page]))
        self.assertEqual(2,patch_fetch.call_count)
        self.assertEqual(['failed','success'],[row['status'] for row in trace])
        self.assertEqual(2,len(results[0]['data']['items']))
        self.assertIn('body_excerpt',results[0]['data']['items'][1])
        self.assertNotIn('body_excerpt',original['data']['items'][1])
        self.assertEqual('https://example.test/b',results[0]['sources'][-1]['canonical_url'])

    def test_dataset_landing_page_and_json_metadata_are_not_document_text(self):
        from olcr_api.analysis_documents import materialize_documents
        catalog=self.result([{'title':'Needs dataset','url':'https://example.test/dataset'}],'humanitarian.hdx',evidence_kind='dataset_catalog')
        fetch=unittest.mock.Mock()
        _,trace=materialize_documents('人道ニーズ',[catalog],fetch)
        self.assertEqual([],trace);fetch.assert_not_called()
        law=self.result([{'title':'Climate rule','url':'https://example.test/rdf'}],'law.example')
        _,trace=materialize_documents('気候政策',[law],lambda url:{'text':'Climate '*80,'content_type':'application/json'})
        self.assertEqual('failed',trace[0]['status'])

    def test_global_country_comparison_excludes_provider_income_and_region_aggregates(self):
        registry=[{'pages':1},[{'iso2Code':'JP','region':{'id':'EAS'}},{'iso2Code':'EU','region':{'id':'NA'}}]]
        observations=[{'pages':1},[
            {'country':{'id':'JP','value':'Japan'},'countryiso3code':'JPN','date':'2024','indicator':{'id':'x'},'value':1},
            {'country':{'id':'EU','value':'European Union'},'countryiso3code':'EUU','date':'2024','indicator':{'id':'x'},'value':400}]]
        tools._worldbank_country_members.cache_clear()
        try:
            with patch.object(tools,'_request',side_effect=lambda host,path,args:registry if path=='/v2/country/all' else observations):
                result=tools.execute('econ.worldbank',{'countries':['all'],'indicator':'x','limit':1})
            self.assertEqual(['JPN'],[row['countryiso3code'] for row in result['data']['items']])
        finally:tools._worldbank_country_members.cache_clear()

    def test_answer_compaction_cannot_attach_later_table_rows_to_an_earlier_caption(self):
        import re
        from olcr_api.analysis_presentation import bounded_lines
        text='\n'.join(f'Scope {letter}\n| Item | Value |\n|---|---:|\n| {letter} | {index*100} |\n' for index,letter in enumerate('ABCDEFGHIJKLMNOP',1))
        shown=bounded_lines(text,400)
        scope=None
        for line in shown.splitlines():
            if line.startswith('Scope '):scope=line[-1]
            row=re.match(r'\| ([A-Z]) \|',line)
            if row:self.assertEqual(scope,row[1])
        self.assertLessEqual(len(shown),400)

    def test_answer_compaction_keeps_fenced_commands_balanced(self):
        from olcr_api.analysis_presentation import bounded_lines
        text='Command:\n```sh\npython3 '+ 'x'*200 +'\n```\nFollowing text.'
        shown=bounded_lines(text,100)
        self.assertEqual(0,shown.count('```'))

    def test_requested_year_points_do_not_expand_to_unrequested_census_years(self):
        from olcr_api import regional_statistics
        args=regional_statistics.compile_prefecture_arguments('日本の人口と雇用を都道府県別に2015年と2020年で比較')
        self.assertEqual([2015,2020],args['requested_years'])

    def test_cellar_accepts_legal_documents_not_matching_vocabulary_labels(self):
        def binding(code, title='Environment protection', work='http://publications.europa.eu/resource/cellar/1234-abcd'):
            return {key: {'value': value} for key,value in dict(celex=code,title=title,work=work).items()}
        payload={'results':{'bindings':[
            {'title':{'value':'Environment'},'work':{'value':'http://data.europa.eu/a4g/AwardCriterion'}} ,
            binding('32020R1234'), binding('32020R1234'),
            binding('62021CJ0123'),  # CELEX case law also remains a legal document.
            binding('32020R9999','Unrelated transport rules'),
            binding('32020R5678',work='https://example.org/vocabulary'),
            binding('not-celex')]}}
        with patch.object(tools,'_request',return_value=payload) as request:
            result=tools.execute('law.eu_cellar',{'query':'environment','limit':10})
        query=request.call_args.args[2]['query']
        self.assertIn('cdm:resource_legal_id_celex',query)
        self.assertIn('cdm:expression_title',query)
        self.assertNotIn('skos',query)
        data=result['data']
        self.assertEqual(['32020R1234','62021CJ0123'],[row['celex'] for row in data['items']])
        self.assertTrue(data['items'][0]['url'].endswith('uri=CELEX:32020R1234'))
        self.assertEqual('document_catalog',data['evidence_kind'])
        self.assertFalse(data['request_complete'])
        self.assertNotIn('body_excerpt',data['items'][0])


if __name__=="__main__":unittest.main()
