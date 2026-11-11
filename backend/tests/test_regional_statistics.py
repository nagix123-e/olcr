import json
from pathlib import Path
import unittest
from unittest.mock import patch
from copy import deepcopy
from olcr_api import regional_statistics as regional
from olcr_api.external_tools import compile_provider_arguments, execute, ExternalToolError
from olcr_api.analysis_coverage import evidence_needs, missing_coverage

FIXTURES = Path(__file__).parent/'fixtures/regional_statistics'

def fixture(name):
    return json.loads((FIXTURES/(name+'.json')).read_text())

class RegionalStatisticsTests(unittest.TestCase):
    def setUp(self):
        regional.region.cache_clear();regional.indicator.cache_clear()

    def fetch(self, host, path, params):
        self.assertEqual(host,'dashboard.e-stat.go.jp')
        self.assertNotIn('appId',params)
        if path.endswith('getRegionInfo'): return fixture('chiyoda' if params['SearchRegionWord']=='千代田区' else 'osaka')
        if path.endswith('getIndicatorInfo'): return fixture('population' if params['SearchIndicatorWord']=='総人口' else 'area')
        if path.endswith('getData'): return fixture('data')
        self.fail('Unexpected endpoint')

    def test_compiler_separates_each_pin_and_preserves_explicit_year(self):
        args=compile_provider_arguments('econ.jp_estat','2020年の人口密度を比較\nRetrieve evidence for this place only: "日本, 東京都, 千代田区"')
        self.assertEqual(args['dataset'],regional.DATASET)
        self.assertEqual((args['place_name'],args['start_year'],args['end_year']),('千代田区',2020,2020))
        self.assertNotIn('unsupported_capability',args)
        with self.assertRaises(ExternalToolError):
            compile_provider_arguments('econ.jp_estat','千代田区と大阪市の人口密度')
        self.assertFalse(regional.supported('千代田区の可住地人口密度'))
        cpi=compile_provider_arguments('econ.jp_estat','日本の消費者物価指数の推移')
        self.assertNotIn('dataset',cpi)
        self.assertGreater(cpi['limit'],1)

    def test_real_response_fixture_converts_hectares_and_joins_only_same_year(self):
        rows=regional.normalize(fixture('data'),'13101','千代田区','0201010000000010000','0101010000000010010')
        row=rows[0]
        self.assertEqual(row['year'],2020)
        self.assertEqual(row['population'],66680)
        self.assertEqual(row['area_km2'],11.66)
        self.assertAlmostEqual(row['value'],66680/11.66)
        self.assertEqual(row['unit'],'人/km²')
        # A newer area observation cannot be joined to older population.
        self.assertNotIn(2024,[r['year'] for r in rows])

    def test_rejects_wrong_geography_unknown_units_and_unmatched_periods(self):
        with self.assertRaisesRegex(ValueError,'REGION_MISMATCH'):
            regional.normalize(fixture('data'),'13101','大阪市','0201010000000010000','0101010000000010010')
        bad=fixture('data')
        bad['GET_STATS']['STATISTICAL_DATA']['CLASS_INF']['CLASS_OBJ'][1]['CLASS'][0]['$']='acres'
        with self.assertRaisesRegex(ValueError,'UNIT_MISMATCH'):
            regional.normalize(bad,'13101','千代田区','0201010000000010000','0101010000000010010')
        missing=fixture('data');values=missing['GET_STATS']['STATISTICAL_DATA']['DATA_INF']['DATA_OBJ']
        for item in values:
            if item['VALUE']['@indicator']=='0201010000000010000': item['VALUE']['@time']=str(int(item['VALUE']['@time'][:4])-100)+'CY00'
        with self.assertRaisesRegex(ValueError,'EMPTY'):
            regional.normalize(missing,'13101','千代田区','0201010000000010000','0101010000000010010')

    def test_adapter_and_coverage_use_official_local_data_without_cpi_credential(self):
        results=[]
        for place in ('千代田区','大阪市'):
            prompt=f'人口密度を比較\nRetrieve evidence for this place only: "日本, {place}"'
            with patch('olcr_api.external_tools._request',side_effect=self.fetch):
                results.append(execute('econ.jp_estat',compile_provider_arguments('econ.jp_estat',prompt)))
        message='日本の千代田区と大阪市の人口密度を比較'
        self.assertEqual(evidence_needs(message)[0]['key'],regional.DATASET)
        self.assertEqual(missing_coverage(message,{'econ.jp_estat'},lambda x:x,results),[])
        evidence=[{'data':r['data'],'sources':[{'source_id':f'S{i}'}]} for i,r in enumerate(results,1)]
        summary=regional.summarize_evidence(evidence)
        self.assertIn('6,496.87',summary);self.assertIn('2.14倍',summary)
        self.assertIn('行政区域全体',summary);self.assertNotIn('消費者物価',summary)
        self.assertTrue(all('dashboard.e-stat.go.jp' in r['sources'][0]['canonical_url'] for r in results))

    def test_no_common_year_never_compares_different_years(self):
        rows=regional.normalize(fixture('data'),'13101','千代田区','0201010000000010000','0101010000000010010')
        first={'data':{'dataset':regional.DATASET,'geo_area':'千代田区','geo_area_code':'13101','items':rows[:1]},'sources':[{'source_id':'S1'}]}
        second=deepcopy(first);second['data'].update(geo_area='大阪市',geo_area_code='27100');second['data']['items'][0]['year']=2015
        self.assertIn('共通の年',regional.summarize_evidence([first,second]))
        self.assertNotIn('倍',regional.summarize_evidence([first,second]))
