"""Official Japanese municipal density from discovered Dashboard series.

No credentials, guessed area codes, cached numeric values or national substitutes.
The existing external-tool transport owns HTTP policy and response limits.
"""
from __future__ import annotations
import json
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from urllib.parse import urlencode

HOST = 'dashboard.e-stat.go.jp'
BASE = '/api/1.0/Json/'
DATASET = 'regional_population_density'
PREFECTURE_DATASET = 'prefecture_population_employment'
CREDIT = 'このサービスは、統計ダッシュボードのAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。'
DEFINITION = '総人口（国勢調査・常住人口）÷同年の行政区域の総面積（km²）。昼間人口・可住地人口密度・ピン周辺の小地域密度とは異なる。'


def supported(request: str) -> bool:
    return bool(re.search(r'人口密度|population\s+density', request, re.I)
                and re.search(r'[一-龯々ヶケぁ-んァ-ヶ]+[市区町村]', request)
                and not re.search(r'可住地|人口集中地区|昼間|高齢|歳以上|歳以下|年齢|男女|男性|女性|外国人|日本人|habitable|daytime|densely inhabited|elderly|female|\bmale\b', request, re.I))


def prefecture_supported(request: str) -> bool:
    return bool(re.search(r'日本|都道府県|Japan', request, re.I)
                and re.search(r'地域別|県別|都道府県|prefectur|regional', request, re.I)
                and re.search(r'人口|雇用|就業|population|employment', request, re.I)
                and not re.search(r'市区町村|人口密度|高齢|年齢|男性|女性|外国人|都市圏|city|municipal|density', request, re.I))


def compile_prefecture_arguments(text: str) -> dict:
    from .analysis_scope import requested_years
    years = sorted(requested_years(text))
    return {'dataset': PREFECTURE_DATASET, 'query': text, 'request_text': text,
            'population': bool(re.search(r'人口|population', text, re.I)),
            'employment': bool(re.search(r'雇用|就業|employment', text, re.I)),
            'start_year': years[0] if years else 2010, 'end_year': years[-1] if years else None,
            'requested_years': years,
            'explicit_period': bool(years), 'trend': bool(re.search(r'変化|推移|trend|changes?', text, re.I))}


@lru_cache(maxsize=4)
def prefecture_indicator(search: str, expected_name: str, fetch) -> str:
    payload = root(fetch(HOST, BASE+'getIndicatorInfo', {'SearchIndicatorWord': search,
                   'RegionalRank': '3', 'Cycle': '3'}), 'GET_META_INDICATOR_INF')
    rows = objects(payload.get('METADATA_INF', {}).get('CLASS_INF', {}).get('CLASS_OBJ'))
    matches = [row for row in rows if row.get('@name') == expected_name and any(
        str((item.get('cycle') or {}).get('@code')) == '3'
        and str((item.get('RegionalRank') or {}).get('@code')) == '3'
        and str((item.get('IsSeasonal') or {}).get('@code')) == '1'
        for item in objects(row.get('CLASS')))]
    if len(matches) != 1 or not re.fullmatch(r'\d{19}', str(matches[0].get('@code', ''))):
        raise ValueError('PROVIDER_INDICATOR_UNAVAILABLE')
    return str(matches[0]['@code'])


def retrieve_prefectures(arguments: dict, fetch) -> tuple[dict, str]:
    payload = root(fetch(HOST, BASE+'getRegionInfo', {'ParentRegionCode': '00000', 'RegionLevel': '3'}), 'GET_META_REGION_INF')
    groups = objects(payload.get('METADATA_INF', {}).get('CLASS_INF', {}).get('CLASS_OBJ'))
    regions = {str(row.get('@regionCode')): str(row['@name']) for group in groups
               for row in objects(group.get('CLASS')) if str(row.get('@level')) == '3'
               and row.get('@toDate') == '999912' and re.fullmatch(r'\d{5}', str(row.get('@regionCode', '')))}
    named = {code: name for code, name in regions.items() if name in str(arguments.get('request_text') or '')}
    regions = named or regions
    if not regions or len(regions) > 50:
        raise ValueError('PROVIDER_REGION_MISMATCH')
    series = {}
    if arguments.get('population'):
        series[prefecture_indicator('総人口', '総人口（総数）', fetch)] = ('jp_regional_population', '総人口（国勢調査／人口推計）')
    if arguments.get('employment'):
        series[prefecture_indicator('就業者', '就業者数（国勢調査）', fetch)] = ('jp_regional_employment', '就業者数（国勢調査）')
    if not series:
        raise ValueError('INVALID_TOOL_ARGUMENTS')
    params = {'IndicatorCode': ','.join(series), 'RegionCode': ','.join(regions), 'Cycle': '3',
              'RegionalRank': '3', 'IsSeasonalAdjustment': '1', 'MetaGetFlg': 'Y',
              'TimeFrom': str(arguments.get('start_year', 2010))+'CY00'}
    if arguments.get('end_year'):
        params['TimeTo'] = str(arguments['end_year'])+'CY00'
    stats = root(fetch(HOST, BASE+'getData', params), 'GET_STATS').get('STATISTICAL_DATA', {})
    metadata = {c.get('@id'): {str(row.get('@code')): row for row in objects(c.get('CLASS'))}
                for c in objects(stats.get('CLASS_INF', {}).get('CLASS_OBJ'))}
    rows, seen = [], {}
    for item in objects(stats.get('DATA_INF', {}).get('DATA_OBJ')):
        for value in objects(item.get('VALUE')):
            code, metric = str(value.get('@regionCode')), str(value.get('@indicator'))
            period = str(value.get('@time') or '')
            if code not in regions or metric not in series or not re.fullmatch(r'\d{4}CY00', period):
                continue
            if arguments.get('requested_years') and int(period[:4]) not in arguments['requested_years']:
                continue
            if (str(value.get('@regionRank')) != '3' or str(value.get('@isSeasonal')) != '1'
                    or str(value.get('@isProvisional')) != '0' or not str(value.get('@stat') or '')):
                continue
            if metadata.get('regionCode', {}).get(code, {}).get('@name') != regions[code]:
                raise ValueError('PROVIDER_REGION_MISMATCH')
            unit = str(metadata.get('unit', {}).get(str(value.get('@unit')), {}).get('$', ''))
            if unit not in {'人', '千人', '万人'}:
                raise ValueError('PROVIDER_UNIT_MISMATCH')
            try: number = Decimal(str(value.get('$', '')).replace(',', ''))
            except InvalidOperation: continue
            if not number.is_finite() or number < 0:
                continue
            number *= {'人': 1, '千人': 1000, '万人': 10000}[unit]
            key = (code, metric, period)
            if key in seen:
                if seen[key] != number: raise ValueError('PROVIDER_CONFLICTING_VALUES')
                continue
            seen[key] = number
            rows.append({'country': 'Japan', 'geo_area': regions[code], 'geo_area_code': code,
                         'year': int(period[:4]), 'date': period[:4], 'indicator': series[metric][0],
                         'series': series[metric][1], 'value': float(number), 'unit': '人',
                         'source_indicator': metric, 'source_statistic': str(value['@stat']), 'geo_level': 'prefecture'})
    required_points = 2 if arguments.get('trend') else 1
    absent = [{'region': name, 'indicator': series[metric][0]} for code, name in regions.items() for metric in series
              if sum(c == code and m == metric for c, m, p in seen) < required_points]
    if arguments.get('explicit_period'):
        absent.extend({'region': name, 'year': year, 'indicator': series[metric][0]}
                      for code, name in regions.items() for metric in series
                      for year in (arguments.get('requested_years') or range(int(arguments['start_year']), int(arguments['end_year'])+1))
                      if (code, metric, f'{year}CY00') not in seen)
    return {'dataset': PREFECTURE_DATASET, 'items': rows, 'has_payload': bool(rows), 'result_kind': 'items',
            'semantic_status': 'DATA' if rows else 'EMPTY', 'item_count': len(rows),
            'request_complete': bool(rows) and not absent, 'missing_requests': absent,
            'definition': '都道府県の行政区域全体。総人口は国勢調査／人口推計、就業者数は国勢調査に基づく人数。共通年を確認して比較する。就業率や年次の労働力調査とは異なる。',
            'requested_regions': list(regions.values()), 'source_credit': CREDIT,
            'default_period': not arguments.get('explicit_period')}, 'https://'+HOST+BASE+'getData?'+urlencode(params)


def compile_arguments(text: str) -> dict:
    # The host separates each pin before compilation. Never choose the first
    # member of an ambiguous two-place prompt at the provider boundary.
    location = text
    match = re.search(r'Retrieve evidence for this place only:\s*("[^\n]*")', text)
    if match:
        location = json.loads(match.group(1))
    names = []
    for part in re.split(r'[,、\s]+', location):
        part = re.sub(r'^(?:日本|Japan)', '', part, flags=re.I)
        part = re.sub(r'^(?:東京都|北海道|京都府|大阪府|[一-龯]{2,3}県)', '', part)
        names.extend(re.findall(r'[一-龯々ヶケぁ-んァ-ヶ]+?(?:市|区|町|村)', part))
    names = list(dict.fromkeys(names))
    if len(names) != 1:
        raise ValueError('LOCATION_AMBIGUOUS' if names else 'LOCATION_NOT_FOUND')
    years = sorted(set(int(y) for y in re.findall(r'(?<!\d)((?:19|20)\d{2})(?:年|\b)', text)))
    result = {'dataset': DATASET, 'place_name': names[0], 'query': names[0], 'limit': 3}
    if years:
        result.update(start_year=min(years), end_year=max(years))
    return result


def objects(value):
    return [value] if isinstance(value, dict) else [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def root(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, dict) or str((value.get('RESULT') or {}).get('status')) != '0':
        raise ValueError('PROVIDER_BAD_RESPONSE')
    return value


@lru_cache(maxsize=128)
def region(place_name: str, fetch) -> tuple[str, str]:
    payload = root(fetch(HOST, BASE+'getRegionInfo', {'SearchRegionWord': place_name}), 'GET_META_REGION_INF')
    groups = objects(payload.get('METADATA_INF', {}).get('CLASS_INF', {}).get('CLASS_OBJ'))
    matches = {str(row.get('@regionCode')): str(row.get('@name')) for group in groups
               for row in objects(group.get('CLASS')) if row.get('@name') == place_name
               and row.get('@toDate') == '999912' and str(row.get('@level')) in {'8','9','10','11','12','13'}
               and re.fullmatch(r'\d{5}', str(row.get('@regionCode', '')))}
    if len(matches) != 1:
        raise ValueError('LOCATION_AMBIGUOUS' if matches else 'LOCATION_NOT_FOUND')
    return next(iter(matches.items()))


@lru_cache(maxsize=8)
def indicator(search: str, expected_name: str, fetch) -> str:
    payload = root(fetch(HOST, BASE+'getIndicatorInfo', {'SearchIndicatorWord': search, 'RegionalRank': '4'}), 'GET_META_INDICATOR_INF')
    rows = objects(payload.get('METADATA_INF', {}).get('CLASS_INF', {}).get('CLASS_OBJ'))
    matches = [row for row in rows if row.get('@name') == expected_name
               and any(str((c.get('cycle') or {}).get('@code')) == '3'
                       and str((c.get('RegionalRank') or {}).get('@code')) == '4'
                       and str((c.get('IsSeasonal') or {}).get('@code')) == '1' for c in objects(row.get('CLASS')))]
    if len(matches) != 1 or not re.fullmatch(r'\d{19}', str(matches[0].get('@code', ''))):
        raise ValueError('PROVIDER_INDICATOR_UNAVAILABLE')
    return str(matches[0]['@code'])


def normalize(payload, code: str, name: str, population_id: str, area_id: str) -> list[dict]:
    stats = root(payload, 'GET_STATS').get('STATISTICAL_DATA', {})
    metadata = {c.get('@id'): {str(row.get('@code')): row for row in objects(c.get('CLASS'))}
                for c in objects(stats.get('CLASS_INF', {}).get('CLASS_OBJ'))}
    actual_name = metadata.get('regionCode', {}).get(code, {}).get('@name')
    if actual_name != name:
        raise ValueError('PROVIDER_REGION_MISMATCH')
    rows = {}
    for item in objects(stats.get('DATA_INF', {}).get('DATA_OBJ')):
        for value in objects(item.get('VALUE')):
            period = str(value.get('@time') or '')
            if (value.get('@regionCode') != code or not re.fullmatch(r'\d{4}CY00', period)
                    or str(value.get('@regionRank')) != '4' or str(value.get('@isSeasonal')) != '1'
                    or str(value.get('@isProvisional')) != '0'):
                continue
            indicator_id = value.get('@indicator')
            if indicator_id not in {population_id, area_id}: continue
            unit = str(metadata.get('unit', {}).get(str(value.get('@unit')), {}).get('$', ''))
            try: number = Decimal(str(value.get('$', '')).replace(',', ''))
            except InvalidOperation: continue
            if not number.is_finite() or number < 0: continue
            expected_stat = '00200521' if indicator_id == population_id else '00200502'
            if str(value.get('@stat')) != expected_stat: raise ValueError('PROVIDER_SOURCE_MISMATCH')
            if indicator_id == population_id and number != number.to_integral_value(): raise ValueError('PROVIDER_BAD_RESPONSE')
            if indicator_id == population_id and unit != '人': raise ValueError('PROVIDER_UNIT_MISMATCH')
            if indicator_id == area_id and unit not in {'ha', 'km²', 'km2', 'k㎡', '㎢'}: raise ValueError('PROVIDER_UNIT_MISMATCH')
            key = (period, indicator_id)
            converted = number / 100 if indicator_id == area_id and unit == 'ha' else number
            if key in rows and rows[key] != converted: raise ValueError('PROVIDER_CONFLICTING_VALUES')
            rows[key] = converted
    result = []
    for period in sorted({p for p, _ in rows}, reverse=True):
        population, area = rows.get((period, population_id)), rows.get((period, area_id))
        if population is None or area is None or area <= 0: continue
        result.append({'geo_area': name, 'geo_area_code': code, 'year': int(period[:4]), 'period': period[:4],
                       'indicator': DATASET, 'value': float(population / area), 'unit': '人/km²',
                       'population': int(population), 'area_km2': float(area), 'definition': DEFINITION,
                       'population_indicator': population_id, 'area_indicator': area_id,
                       'population_source': '国勢調査', 'area_source': '社会・人口統計体系',
                       'value_status': '確報', 'geographic_scope': '行政区域全体'})
    if not result: raise ValueError('PROVIDER_EMPTY')
    return result


def retrieve(arguments: dict, fetch) -> tuple[dict, str]:
    place = arguments.get('place_name')
    if not isinstance(place, str) or not re.fullmatch(r'[一-龯々ヶケぁ-んァ-ヶ]{1,30}[市区町村]', place):
        raise ValueError('INVALID_TOOL_ARGUMENTS')
    code, name = region(place, fetch)
    population_id = indicator('総人口', '総人口（総数）', fetch)
    area_id = indicator('総面積', '総面積（北方地域及び竹島を除く）', fetch)
    params = {'IndicatorCode': population_id+','+area_id, 'RegionCode': code, 'Cycle': '3',
              'RegionalRank': '4', 'IsSeasonalAdjustment': '1', 'MetaGetFlg': 'Y',
              'TimeFrom': str(arguments.get('start_year', 1985))+'CY00'}
    if arguments.get('end_year'): params['TimeTo'] = str(arguments['end_year'])+'CY00'
    payload = fetch(HOST, BASE+'getData', params)
    rows = normalize(payload, code, name, population_id, area_id)
    rows = rows[:min(10, max(1, int(arguments.get('limit', 3))))]
    data = {'dataset': DATASET, 'indicator': DATASET, 'series': '総面積当たり常住人口密度',
            'geo_area': name, 'geo_area_code': code, 'country': 'Japan', 'items': rows,
            'unit': '人/km²', 'definition': DEFINITION, 'has_payload': True, 'semantic_status': 'DATA',
            'result_kind': 'items', 'item_count': len(rows), 'evidence_field_count': len(rows)*3,
            'default_period': 'start_year' not in arguments, 'source_credit': CREDIT}
    return data, 'https://'+HOST+BASE+'getData?'+urlencode(params)


def summarize_evidence(evidence_rows: list[dict]) -> str | None:
    """Verified same-year arithmetic used by the shared analysis fallback."""
    if not evidence_rows or any((e.get('data') or {}).get('dataset') != DATASET for e in evidence_rows):
        return None
    by_place = {}
    for evidence in evidence_rows:
        data = evidence['data']
        source = next((s.get('source_id') for s in evidence.get('sources', []) if s.get('source_id')), '')
        by_place[data['geo_area_code']] = (data['geo_area'], {r['year']: r for r in data['items']}, source)
    periods = [set(rows) for _, rows, _ in by_place.values()]
    common = set.intersection(*periods)
    if not common:
        return '取得した地域間で共通の年の人口・面積が揃わないため、人口密度の数値比較はできません。'
    year = max(common)
    lines = [f'今回のAPI応答で比較できる最新の共通年は{year}年です。行政区域全体の常住人口密度を比較します。',
             '| 地域 | 総人口（人） | 総面積（km²） | 人口密度（人/km²） | 出典 |',
             '|---|---:|---:|---:|---|']
    values = []
    for name, rows, source in by_place.values():
        row = rows[year]
        density = Decimal(str(row['population'])) / Decimal(str(row['area_km2']))
        values.append((name, density, source))
        lines.append(f"| {name} | {row['population']:,} | {row['area_km2']:,.2f} | {density:,.2f} | [{source}] |")
    if len(values) == 2:
        high, low = sorted(values, key=lambda item: item[1], reverse=True)
        lines.append(f'{high[0]}の方が{high[1]-low[1]:,.2f} 人/km²高く、' +
                     (f'{high[1]/low[1]:.2f}倍です' if low[1] else '比較対象が0のため倍率は算出できません') +
                     f' [{high[2]}] [{low[2]}]。')
    lines.extend(['計算式：総人口 ÷ 総面積。面積はAPIのhaをkm²へ換算しています（100 ha = 1 km²）。',
                  '人口は国勢調査、面積は社会・人口統計体系の同年の確報です。ピン周辺の小地域や昼間人口の密度ではなく、各行政区域全体の比較です。', CREDIT])
    return '\n\n'.join([lines[0], '\n'.join(lines[1:3+len(values)]), *lines[3+len(values):]])
