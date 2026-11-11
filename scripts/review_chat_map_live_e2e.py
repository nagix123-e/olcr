#!/usr/bin/env python3
"""Independently check recorded live map evidence; content still needs review.

No model/API calls and no imports of the production router or renderer. The
oracle below describes the 25 test requests, not the production implementation.
"""
import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo


EXPECTED_PROVIDERS = {
    **{f'MAP-G{i:02}': ['knowledge.wikimedia'] for i in [1, 2, 3, 4, 5, 6, 14, 19, 20, 23, 24, 25]},
    **{f'MAP-G{i:02}': ['weather.open_meteo'] for i in [7, 8, 13, 15, 16]},
    **{f'MAP-G{i:02}': ['astronomy.sunrise'] for i in [9, 17]},
    **{f'MAP-G{i:02}': ['geo.elevation'] for i in [10, 18]},
    **{f'MAP-G{i:02}': ['earth.earthquakes'] for i in [11, 22]},
    'MAP-G12': ['geo.poi'], 'MAP-G21': ['geo.routing'],
}


def distance(a, b):
    lat_a, lat_b = map(math.radians, [a[0], b[0]])
    delta_lat, delta_lon = map(math.radians, [b[0] - a[0], b[1] - a[1]])
    return 6371 * 2 * math.asin(min(1, math.sqrt(math.sin(delta_lat / 2)**2 +
        math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2)**2)))


def check_case(case):
    packets = case.get('tool', {}).get('results', [])
    answer = case.get('response', '')
    routing = next((c['api_routing'] for c in case.get('task', {}).get('selected_context', []) if c.get('api_routing')), {})
    recipe = routing.get('brain_recipe') or {}
    checks = {
        'http_200': case.get('http_status') == 200,
        'provider_matches_independent_oracle': case.get('provider_ids') == EXPECTED_PROVIDERS[case['id']],
        'actual_embedding': routing.get('retrieval') == 'embedding',
        'actual_laya': case.get('laya_ran') is True,
        'actual_brain_plan': any(c.get('role') == 'brain_analysis_planner' and c.get('status') == 'success'
                                 and c.get('actual_model') not in {None, 'UNKNOWN'} for c in case.get('task', {}).get('model_calls', [])),
        'all_pins_and_sources': bool(packets) and not any(case.get(k) for k in ['missing_pins', 'missing_coverage', 'api_failures']),
        'synthesis_not_reported_incomplete': '分析は未完了' not in answer,
    }
    source_text = {}
    sources = {s['source_id']: s.get('canonical_url') or s.get('url') for s in case.get('sources', [])}
    for index, packet in enumerate(packets):
        data, scope = packet['data'], packet['data']['request_scope']
        kind, tag = data['map_task_kind'], str(index)
        if data.get('map_pin'):
            point = case['pins'][ord(data['map_pin']) - 65]
            checks['original_pin_' + tag] = (scope.get('latitude'), scope.get('longitude')) == (point['lat'], point['lon'])
        for source_id, url in sources.items():
            if url == data.get('page_url'):
                source_text[source_id] = data.get('extract', '')
            for item in data.get('related_articles', []):
                if item.get('page_url') == url:
                    source_text[source_id] = item.get('extract', '')
            for item in data.get('items', []):
                if item.get('coordinate_source') == url:
                    source_text[source_id] = item.get('wikipedia_description', '')
                if item.get('description_source') == url:
                    source_text[source_id] = item.get('official_description', '')

        if kind == 'weather.open_meteo':
            local_date = datetime.fromisoformat(packet['fetched_at']).astimezone(ZoneInfo(data['timezone'])).date()
            count = re.search(r'今後(\d+)日', case['message'])
            count = int(count[1]) if count else 1
            offset = 1 if '明日' in case['message'] else 0
            checks['local_forecast_window_' + tag] = [r['date'] for r in data['items']] == [str(local_date + timedelta(days=offset + i)) for i in range(count)]
            fields = ['temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max', 'wind_speed_10m_max', 'humidity_min', 'humidity_max']
            checks['forecast_fields_' + tag] = all(all(isinstance(r.get(f), (int, float)) for f in fields) for r in data['items'])
            checks['forecast_units_' + tag] = all(data['units'].get(k) == v for k, v in {
                'temperature_2m_max': '°C', 'precipitation_probability_max': '%', 'wind_speed_10m_max': 'km/h'}.items())
            if case['id'] == 'MAP-G08':
                best = min(r['precipitation_probability_max'] for r in data['items'])
                tail = answer.split('降水確率が最も低い日:')[-1]
                checks['all_minimum_ties'] = all(r['date'] in tail for r in data['items'] if r['precipitation_probability_max'] == best)
        elif kind == 'astronomy.sunrise':
            duration = (datetime.fromisoformat(data['sunset']) - datetime.fromisoformat(data['sunrise'])).total_seconds()
            checks['daylight_arithmetic_' + tag] = abs(duration - data['day_length']) <= 1
            checks['sunrise_point_' + tag] = abs(data['lat'] - scope['latitude']) < 1e-5 and abs(data['lng'] - scope['longitude']) < 1e-5
            checks['sunrise_timezone_' + tag] = data['tzid'] in answer and data['date'] in answer
        elif kind == 'geo.elevation':
            checks['elevation_point_resolution_' + tag] = (data['latitude'], data['longitude']) == (scope['latitude'], scope['longitude']) and math.isfinite(data['elevation_m']) and data['pixel_spacing_m'] > 0
        elif kind == 'earth.earthquakes':
            start, end = map(datetime.fromisoformat, [scope['starttime'], scope['endtime']])
            checks['catalog_window_' + tag] = abs((end - start).total_seconds() - 30 * 86400) < 1
            checks['radius_magnitude_' + tag] = scope['maxradiuskm'] == 100 and scope['minmagnitude'] == 4 and all(
                e['magnitude'] >= 4 and distance((scope['latitude'], scope['longitude']), (e['coordinates'][1], e['coordinates'][0])) <= 100.1
                and start <= datetime.fromisoformat(e['time_utc']) <= end for e in data['events'])
            checks['count_max_' + tag] = data['count'] == len(data['events']) and data['max_magnitude'] == max((e['magnitude'] for e in data['events']), default=None)
            checks['zero_is_not_failure_' + tag] = data['count'] != 0 or data['max_magnitude'] is None and '該当なし' in answer
        elif kind == 'geo.routing':
            a, b = case['pins']
            checks['joint_original_pins'] = scope['start'] == f"{a['lon']},{a['lat']}" and scope['end'] == f"{b['lon']},{b['lat']}" and data['map_pins'] == ['A', 'B']
            route = data['routes'][0]
            checks['road_distance_duration'] = route['distance_m'] / 1000 >= distance((a['lat'], a['lon']), (b['lat'], b['lon'])) * .95 and f"{route['distance_m']/1000:.2f} km" in answer and f"{route['duration_s']/60:.1f}分" in answer
        elif kind == 'geo.poi':
            checks['facility_radius_count'] = 0 < len(data['items']) <= 5 and scope['radius_m'] == 3000 and all(distance(
                (scope['latitude'], scope['longitude']), (r['latitude'], r['longitude'])) <= 3 for r in data['items'])
        if data.get('climate_normals'):
            normals = data['climate_normals']
            checks['monthly_normal_cells_' + tag] = all(len(v) == 12 for v in normals['monthly'].values())

    if recipe.get('_grounded_paragraphs'):
        evidence = [e for p in recipe['_grounded_paragraphs'] for e in p['evidence']]
        evidence += [e for p in recipe.get('_grounded_common', []) for e in p['evidence']]
        checks['quotes_match_the_cited_sources'] = all(e['quote'] in source_text.get(e['source_id'], '') for e in evidence)
    if case['id'] in {'MAP-G15', 'MAP-G18'} and len(packets) == 2:
        if case['id'] == 'MAP-G15':
            a, b = [p['data']['items'][0] for p in packets]
            checks['temperature_deltas'] = all(f"{abs(a[k]-b[k]):.1f}℃" in answer for k in ['temperature_2m_max', 'temperature_2m_min'])
        else:
            checks['elevation_delta'] = f"{abs(packets[0]['data']['elevation_m']-packets[1]['data']['elevation_m']):,.2f} m" in answer
    if case['id'] == 'MAP-G22' and len(packets) == 2:
        checks['shared_catalog_window'] = all(packets[0]['data']['request_scope'][k] == packets[1]['data']['request_scope'][k] for k in ['starttime', 'endtime'])
    if case['id'] == 'MAP-G23':
        checks['shared_normal_period'] = len({tuple(p['data']['climate_normals']['period']) for p in packets}) == 1
    return {'id': case['id'], 'evidence_checks_pass': all(checks.values()), 'checks': checks,
            'content_review': 'REQUIRED: relevance, completeness and comparison quality are not established by these checks'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    results = [check_case(json.loads(path.read_text())) for path in sorted(args.profile.glob('MAP-G[0-9][0-9].json'))]
    report = {'profile': str(args.profile), 'total': len(results), 'evidence_checks_pass': sum(r['evidence_checks_pass'] for r in results), 'cases': results}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}, ensure_ascii=False))
    for result in results:
        if not result['evidence_checks_pass']:
            print(result['id'], [k for k, passed in result['checks'].items() if not passed])
