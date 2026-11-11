"""Regression contracts for geographic intent, pin identity and empty catalogs."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from olcr_api import external_tools as tools, map_analysis
from olcr_api.chat_maps import MapPoint, ChatMap, map_context


class MapAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.points = [MapPoint(lat=60.1699, lon=24.9384, name='フィンランド, ヘルシンキ'),
                       MapPoint(lat=41.9028, lon=12.4964, name='イタリア, ローマ')]

    def compile(self, tool, message, index=0):
        return tools.compile_provider_arguments(tool, message, map_point=self.points[index], map_points=self.points)

    def test_task_capability_does_not_depend_on_places_or_embed_adjacent_apis(self):
        contexts = [map_context(ChatMap(points=self.points)),
                    map_context(ChatMap(points=[MapPoint(lat=0, lon=0, name='架空のラベル')]))]
        cases = [('歴史と現在の街の特徴を説明して', ['knowledge.wikimedia']),
                 ('川が都市の発展に果たした役割を比較して', ['knowledge.wikimedia']),
                 ('夏と冬の気候を比較して', ['knowledge.wikimedia']),
                 ('明日の降水確率と最高気温', ['weather.open_meteo']),
                 ('今日の日の出と昼の長さ', ['astronomy.sunrise']),
                 ('標高差を教えて', ['geo.elevation']),
                 ('半径100kmの地震', ['earth.earthquakes']),
                 ('3km以内の博物館を探して', ['geo.poi']),
                 ('車で移動するときの道路距離', ['geo.routing'])]
        for message, expected in cases:
            for context in contexts:
                self.assertEqual(expected, map_analysis.sources(message + context))

    def test_sunrise_uses_each_pin_and_local_timezone_response(self):
        a = self.compile('astronomy.sunrise', '今日の昼の長さを比較して')
        b = self.compile('astronomy.sunrise', '今日の昼の長さを比較して', 1)
        self.assertEqual(a['latitude'], self.points[0].lat)
        self.assertEqual(b['longitude'], self.points[1].lon)
        payload = {'date': '2026-10-26', 'tzid': 'Europe/Helsinki', 'sunrise': '2026-10-26T07:27:23+02:00',
                   'sunset': '2026-10-26T16:39:57+02:00', 'day_length': 33154}
        with patch.object(tools, '_request', return_value=payload) as network:
            result = tools.execute('astronomy.sunrise', a)
        self.assertEqual(network.call_args.args[:2], ('api.sunrise-sunset.org', '/v2'))
        self.assertEqual(network.call_args.args[2]['lat'], self.points[0].lat)
        self.assertEqual(result['data']['tzid'], 'Europe/Helsinki')

    def test_earthquake_radius_and_shared_exact_window_zero_is_data(self):
        reference = datetime(2026, 10, 26, 1, 2, 3, tzinfo=timezone.utc)
        args = tools.compile_provider_arguments('earth.earthquakes', '半径100kmで直近30日間にマグニチュード4以上の地震',
            map_point=self.points[0], map_points=self.points, map_reference_time=reference)
        with patch.object(tools, '_request', return_value={'features': []}) as network:
            result = tools.execute('earth.earthquakes', args)
        params = network.call_args.args[2]
        self.assertEqual(params['maxradiuskm'], 100)
        self.assertEqual(params['latitude'], 60.1699)
        self.assertEqual(params['minmagnitude'], 4)
        self.assertEqual(params['endtime'], reference.isoformat())
        self.assertNotIn('minlatitude', params)
        self.assertEqual(result['data']['count'], 0)
        self.assertIsNone(result['data']['max_magnitude'])
        self.assertTrue(result['data']['has_payload'])

    def test_routing_is_one_joint_a_to_b_request(self):
        args = tools.compile_provider_arguments('geo.routing', '車で道路距離と時間を教えて', map_points=self.points)
        self.assertEqual(args['start'], '24.9384,60.1699')
        self.assertEqual(args['end'], '12.4964,41.9028')
        self.assertEqual(args['origin'], self.points[0].name)
        with self.assertRaises(tools.ExternalToolError):
            tools.compile_provider_arguments('geo.routing', '車で移動', map_points=self.points[:1])

    def test_wikipedia_query_is_entity_not_task_or_country(self):
        p = MapPoint(lat=34.3925, lon=132.4536, name='日本, 広島県, 広島市, 広島平和記念公園')
        args = tools.compile_provider_arguments('knowledge.wikimedia', '設けられた経緯を説明して', map_point=p, map_points=[p])
        self.assertEqual(args['query'], '広島平和記念公園')
        self.assertEqual(args['request_text'], '設けられた経緯を説明して')
        self.assertNotIn('geometry', args)

    def test_weather_tomorrow_is_resolved_from_provider_local_date(self):
        args = self.compile('weather.open_meteo', '明日の気温と湿度を教えて')
        payload = {'timezone': 'Europe/Helsinki', 'current': {'time': '2026-10-26T23:15'},
                   'daily': {'time': ['2026-10-26', '2026-10-27'], 'temperature_2m_max': [9, 10]},
                   'hourly': {'time': ['2026-10-27T10:00'], 'relative_humidity_2m': [70]}, 'daily_units': {'temperature_2m_max': '°C'}}
        with patch.object(tools, '_request', return_value=payload):
            result = tools.execute('weather.open_meteo', args)
        self.assertEqual(result['data']['items'], [{'date': '2026-10-27', 'temperature_2m_max': 10,
                                                  'humidity_min': 70, 'humidity_max': 70}])

    def test_article_compaction_preserves_each_viewpoint_and_units(self):
        parser = map_analysis._Sections()
        parser.feed('<h2>地理</h2><p>面積は10 km<sup>2</sup>。</p><h2>歴史</h2><h3>古代</h3><p>都が置かれた。</p>')
        parser.flush()
        self.assertIn('10 km²', parser.sections[0][1])
        self.assertEqual(parser.sections[1][0], '歴史 / 古代')
        data = {'title': 'city', 'lead': 'lead', 'sections': ['地形\n' + 'a'*1000, '歴史\n' + 'b'*1000],
                'extract': 'a'*2000, 'map_pin': 'B', 'request_scope': {'latitude': 0}}
        packet = map_analysis.composition_data(data, 600)
        self.assertIn('地形', packet['extract']); self.assertIn('歴史', packet['extract'])
        self.assertNotIn('request_scope', packet)

    def test_parked_domain_is_not_a_verified_facility_description(self):
        self.assertEqual('', map_analysis.facility_description({'description': 'Buy this domain. More domains at Seo.Domains', 'text': 'museum'}))
        description = '当美術館は近代美術の作品を収蔵し、常設展示と企画展で国内外の美術を紹介しています。' * 2
        self.assertEqual(description, map_analysis.facility_description({'description': description, 'text': '図録を通販で発売中です。'}))

    def test_facility_uses_exhibit_prose_instead_of_site_boilerplate_or_news(self):
        detail='「日本列島の自然と私たち」をテーマとする日本館では、日本列島の自然と生い立ち、人間と自然のかかわりの歴史を展示しています。'
        result=map_analysis.facility_description({'description':'自然科学博物館の公式サイトです。利用案内、展示案内、イベント情報をご覧いただけます。',
            'text':'2026年10月 常設展・所蔵作品データベースの一時停止のお知らせ。'*2+'\n\n'+detail})
        self.assertEqual(detail,result)
        self.assertEqual('',map_analysis.facility_description({'text':'2025.11.30 取手コレクション展 終了。収蔵作品の展示。'*3}))
        self.assertEqual('',map_analysis.facility_description({'text':'# Museum / Vintage Watch Collection の特別なコレクションをご案内いたします。'}))

    def test_grounding_reads_year_after_real_newline_and_not_url_digits(self):
        from olcr_api.context_policy import evidence_text
        text = evidence_text({'extract': '歴史\n1945年8月6日\n1949年', 'url': 'https://example.org/2006'})
        self.assertIn('\n1945', text)
        self.assertNotIn('2006', text)

    def test_source_internal_date_conflict_is_not_quoted_as_settled_history(self):
        passages = map_analysis.article_passages([
            ('歴史','1949年11月1日に「記念都市建設法」が制定された。世界平和を願う目的で公園の建設計画が作られた。'),
            ('沿革','1949年8月6日 「記念都市建設法」が制定される。')], '設けられた経緯を説明して')
        text = '\n'.join(p['text'] for p in passages)
        self.assertNotIn('11月1日',text)
        self.assertNotIn('8月6日',text)
        self.assertIn('建設計画',text)

    def test_source_captions_and_references_are_not_explanatory_evidence(self):
        parser = map_analysis._Sections()
        parser.feed('<h2>地学</h2><p>断層運動で形成された構造湖である。</p><figure><figcaption>地溝湖の空撮。</figcaption></figure>')
        parser.flush()
        passages = map_analysis.article_passages(parser.sections + [('参考文献','地形写真集。断層と構造湖の写真。')], '地形を説明して')
        self.assertEqual(1,len(passages))
        self.assertIn('断層運動',passages[0]['text'])
        self.assertNotIn('空撮',passages[0]['text'])

    def test_industry_history_omits_unrequested_unaligned_shipments(self):
        passages=map_analysis.article_passages([('工業',
            '製造品出荷額は2兆円である。1937年に工場を設立したことから工業化が始まった。')],
            '代表的な製造業と発展した経緯を説明して')
        text='\n'.join(p['text'] for p in passages)
        self.assertNotIn('2兆',text);self.assertIn('1937年',text)

    def test_rail_explanation_does_not_add_unrequested_timetable_numbers(self):
        passages=map_analysis.article_passages([('乗り入れ路線',
            '東北本線、仙山線、仙石線などの複数の鉄道路線が乗り入れている。\n\n新幹線は27往復運行され、東京駅まで最短1時間30分である。')],
            '接続する鉄道路線と都市の位置から説明して')
        rail='\n'.join(p['text'] for p in passages if p['aspect']=='rail')
        self.assertIn('東北本線',rail);self.assertNotIn('27往復',rail)

    def test_sight_has_its_feature_and_selected_history_from_its_own_source(self):
        intro,history='この寺の見どころは本尊の大仏である。','752年に大仏の開眼会が行われた。'
        row={'data':{'map_task_kind':'knowledge.wikimedia','map_pin':'A','title':'公園',
            'related_articles':[{'title':'寺','page_url':'https://example.org/temple',
                'extract':intro+'\n\n'+history,'passages':[{'text':history}]}]},
            'sources':[{'source_id':'S1','url':'https://example.org/park'},
                       {'source_id':'S2','url':'https://example.org/temple'}]}
        answer,accepted=map_analysis.compose_narrative('見どころを1つ、その歴史とともに説明して',[row],
            lambda *args:{'text':json.dumps({'selections':{'A:sight:0':['F1']}})},lambda *args:None,8000,{})
        self.assertTrue(accepted);self.assertIn(intro,answer);self.assertIn(history,answer)
        self.assertNotIn('[S1]',answer);self.assertIn('[S2]',answer)

    def test_missing_viewpoint_in_summary_does_not_count_as_completed_analysis(self):
        row={'data':{'map_task_kind':'knowledge.wikimedia','map_pin':'A','title':'公園',
                     'extract':'1945年に建設された公園である。'},'sources':[{'source_id':'S1','url':'https://example.org'}]}
        answer,accepted=map_analysis.compose_narrative('設けられた経緯と現在の役割を説明して',[row],
            lambda *args: self.fail('missing evidence must be detected before synthesis'),lambda *args:None,8000,{})
        self.assertFalse(accepted);self.assertIn('現在の役割',answer)

    def test_shared_status_needs_positive_evidence_from_both_places(self):
        rows=[{'data':{'passages':[{'aspect':'heritage','text':text}]},'sources':[{'source_id':sid}]} for text,sid in [
            ('世界文化遺産に登録された文化財である。','S1'),('世界遺産のリストに登録されている。','S2')]]
        result=map_analysis.common_observations('文化財の共通点と違いを説明して',rows)
        self.assertEqual(1,len(result));self.assertEqual(2,len(result[0]['evidence']))
        rows[1]['data']['passages'][0]['text']='世界遺産には登録されていない。'
        self.assertEqual([],map_analysis.common_observations('文化財の共通点を説明して',rows))

    def test_extractive_comparison_cannot_substitute_the_other_pin_or_invent_prose(self):
        rows = [{'data': {'map_task_kind': 'knowledge.wikimedia', 'map_pin': pin, 'title': title,
                          'passages': [{'aspect': 'architecture', 'text': text}]},
                 'sources': [{'source_id': sid, 'url': url}]} for pin, title, text, sid, url in [
                     ('A', '城', '連立式の天守を持つ城郭建築である。', 'S1', 'https://example.org/a'),
                     ('B', '神社', '海上に社殿と回廊が建てられている。', 'S2', 'https://example.org/b')]]
        outputs = [dict(selections={'A:architecture':['F2'], 'B:architecture':['F2']}),
                   dict(selections={'A:architecture':['F1'], 'B:architecture':['F2']}, text='両者とも水上建築。')]
        recipe, statuses = {}, []
        answer, accepted = map_analysis.compose_narrative('建築の特徴を比較して', rows,
            lambda *args: {'text': json.dumps(outputs.pop(0), ensure_ascii=False)},
            lambda result, status: statuses.append(status), 8000, recipe)
        self.assertTrue(accepted)
        self.assertEqual(['grounding_guard_fallback', 'success'], statuses)
        self.assertIn('連立式の天守', answer); self.assertIn('海上に社殿', answer)
        self.assertNotIn('両者とも', answer)

    def test_retrieval_keeps_short_geology_and_current_role_paragraphs(self):
        passages = map_analysis.article_passages([
            ('地学的知見', '断層運動で生まれた構造湖であり、周辺の盆地と関わる。'),
            ('歴史', '1951年に設立計画が建設委員会で審議された。'),
            ('', '毎年追悼式典が行われ、平和を祈念する場所である。')],
            '地形と設けられた経緯、現在の役割を説明して')
        self.assertEqual({'terrain','history','current'}, {p['aspect'] for p in passages})
        self.assertTrue(any('構造湖' in p['text'] for p in passages))

    def test_normals_require_thirteen_cells_and_shared_period(self):
        text = '| 月 | 1月 | 2月 | 年\n'
        for label in ['日平均気温 °C （°F）','降水量 mm （inch）','降雪量 cm （inch）']:
            text += '| ' + label + '\n' + '\n'.join('| ' + str(n) + '\n (32)' for n in range(13)) + '\n'
        text += '| 出典：気象庁（平均値：1991年 - 2020年、極値：現在）'
        normal = map_analysis.climate_normals([('気候', text)])
        self.assertEqual([1991,2020],normal['period'])
        self.assertEqual(list(map(float,range(12))),normal['monthly']['temperature'])
        rows = [{'data': {'map_task_kind':'knowledge.wikimedia','climate_normals':normal}, 'sources':[{'source_id':'S1'}]},
                {'data': {'map_task_kind':'knowledge.wikimedia','climate_normals':{**normal,'period':[1961,1990]}},'sources':[{'source_id':'S2'}]}]
        self.assertIn('分析は未完了',map_analysis.summarize(rows,'夏と冬の気候を比較して'))
        self.assertIsNone(map_analysis.climate_normals([('気候',text.replace('| 12\n (32)',''))]))

    def test_wikipedia_facility_fallback_excludes_outside_radius(self):
        def article(title, *args, **kwargs):
            return {'data': ({'related_facilities': ['近い博物館', '遠い博物館'], 'page_url': 'https://ja.wikipedia.org/wiki/station'}
                if title == '駅' else {'title': title, 'official_websites': ['https://museum.example.org/'],
                                      'page_url': 'https://ja.wikipedia.org/wiki/museum'})}
        def summary(host, path, params):
            return {'coordinates': {'lat': 35.001 if '近い' in unquote(path) else 40,
                                    'lon': 139}, 'extract': '科学資料を展示する博物館。'}
        with patch.object(map_analysis, '_article', side_effect=article), patch.object(tools, '_request', side_effect=summary):
            rows, _ = map_analysis._wiki_nearby({'location': '日本, 駅', 'latitude': 35, 'longitude': 139, 'radius_m': 3000})
        self.assertEqual(['近い博物館'], [row['name'] for row in rows])
        self.assertLess(rows[0]['distance_m'], 3000)


if __name__ == '__main__':
    unittest.main()
