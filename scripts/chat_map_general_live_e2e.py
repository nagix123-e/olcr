#!/usr/bin/env python3
"""Run all 25 general map prompts through the live isolated OLCR chat API.

Uses actual local Embedding/Laya/Brain models and public provider APIs. It does
not open the desktop app or touch the user's conversation database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
FIXTURE = ROOT / 'backend/tests/fixtures/chat_map_general_tasks_25.json'

profile = Path(tempfile.mkdtemp(prefix='olcr-map-general-live-', dir='/private/tmp'))
os.environ['OLCR_DB_PATH'] = str(profile / 'test.sqlite')
os.environ.setdefault('OLCR_COMPONENTS_ROOT', str(Path.home() / 'Library/Application Support/OLCR/components'))
os.environ.setdefault('OLCR_LAYA_DEVICE', 'cpu')
os.environ['OLCR_VECTOR_ENABLED'] = 'true'
os.environ.setdefault('OLCR_GRAPHIFY_ENABLED', 'false')

from fastapi.testclient import TestClient
from olcr_api import app as host
from olcr_api.brain_models import brain_scope
from review_chat_map_live_e2e import EXPECTED_PROVIDERS


# One point is placed at the named feature where it is a landmark/station;
# general city requests use a representative city-center point.
POINTS = {
    '京都市': (35.0116, 135.7681), '広島平和記念公園': (34.3925, 132.4536),
    '豊田市': (35.0824, 137.1563), '諏訪湖': (36.0458, 138.0805),
    '奈良公園': (34.6851, 135.8430), '仙台駅': (38.2600, 140.8824),
    '福岡市': (33.5902, 130.4017), '札幌市': (43.0618, 141.3545),
    '那覇市': (26.2124, 127.6809), '松本駅': (36.2301, 137.9634),
    '上野駅': (35.7138, 139.7773), 'マーライオン公園': (1.2868, 103.8545),
    'ロンドン': (51.5072, -0.1276), 'パリ': (48.8566, 2.3522),
    '金沢市': (36.5613, 136.6562), '静岡市': (34.9756, 138.3828),
    'ヘルシンキ': (60.1699, 24.9384), 'ローマ': (41.9028, 12.4964),
    '軽井沢駅': (36.3425, 138.6357), '草津温泉バスターミナル': (36.6217, 138.5966),
    '浜松市': (34.7108, 137.7261), '東京駅': (35.6812, 139.7671),
    '横浜駅': (35.4662, 139.6227), '熊本駅': (32.7898, 130.6881),
    '姫路城': (34.8394, 134.6939), '厳島神社': (34.2960, 132.3198),
    '函館市': (41.7687, 140.7291), '長崎市': (32.7503, 129.8777),
}

configuration = {
    'mode': 'unified',
    'selections': {slot: 'ollama:ornith-1.5:9b' for slot in ('brain', 'coder', 'short_coding', 'general_inference')},
    'efforts': {slot: None for slot in ('brain', 'coder', 'short_coding', 'general_inference')},
    'fallback_models': [],
}
host.brain_models.save(configuration)
settings = host.settings.with_overrides({
    'external_access_enabled': True, 'task_manager_enabled': False,
    'conversation_memory_enabled': False, 'web_mode': 'off',
})
host.settings = settings
host.runtime.settings = settings
host.brain_models.settings = settings

cases = json.loads(FIXTURE.read_text())['cases']
selected_case_ids = set(filter(None, os.environ.get('OLCR_MAP_CASE_IDS', '').split(',')))
if selected_case_ids:
    cases = [case for case in cases if case['id'] in selected_case_ids]
    if len(cases) != len(selected_case_ids):
        raise ValueError('Unknown map case ID')
run_started = time.monotonic()
outcomes = []
print(f'LIVE_PROFILE={profile}', flush=True)
print(f'CASE_COUNT={len(cases)} LAYA_READY_BEFORE_RUN={host.laya_provider.loaded}', flush=True)

with brain_scope(configuration), TestClient(host.app) as client:
    for index, case in enumerate(cases, start=1):
        points = []
        for loc in case['locations']:
            name = loc['place_name']
            key = next((k for k in POINTS if k in name), None)
            if key is None:
                raise RuntimeError(f'No test pin coordinate for {name}')
            lat, lon = POINTS[key]
            points.append({'lat': lat, 'lon': lon, 'name': name})
        request = {
            'message': case['message'], 'project_id': None,
            'location_map': {'type': 'location_map', 'schema_version': 1, 'points': points},
        }
        started = time.monotonic()
        entry: dict = {'id': case['id'], 'message': case['message'], 'pin_count': len(points),
                       'pins': points, 'coordinate_sensitive': case['coordinate_sensitive'],
                       'acceptance_criteria': case['acceptance_criteria']}
        try:
            response = client.post('/api/chat', json=request, timeout=240)
            entry['http_status'] = response.status_code
            response.raise_for_status()
            result = response.json()
            task = result.get('task') or {}
            tool = result.get('tool') or {}
            routing = next((item.get('api_routing') for item in task.get('selected_context', [])
                            if item.get('api_routing')), {})
            result_packets = tool.get('results', [])
            all_evidence_places = [packet.get('data', {}).get('map_place') for packet in result_packets
                                   if isinstance(packet, dict)]
            all_evidence_pins = [packet.get('data', {}).get('map_pin') for packet in result_packets
                                 if isinstance(packet, dict)]
            covered = set(filter(None, all_evidence_pins))
            for packet in result_packets:
                if isinstance(packet, dict):
                    covered.update(packet.get('data', {}).get('map_pins') or [])
            missing_pins = [chr(65 + i) for i in range(len(points)) if chr(65 + i) not in covered]
            serialized_task = {
                'state': task.get('state'), 'route': task.get('route'),
                'reason_category': task.get('reason_category'),
                'selected_context': task.get('selected_context'),
                'model_calls': task.get('model_calls'),
            }
            laya_trace = dict(host.laya_provider.local.last_trace)
            entry.update({
                'conversation_id': result.get('conversation_id'),
                'response': result.get('response', ''),
                'sources': result.get('sources', []),
                'task': serialized_task,
                'tool': tool,
                'laya_trace': laya_trace,
                'laya_selected': routing.get('laya_selected', []),
                'brain_selected': routing.get('brain_selected', []),
                'brain_recipe_fallback': routing.get('brain_planning_fallback') or (routing.get('brain_recipe') or {}).get('planner_fallback'),
                'provider_ids': sorted({packet.get('tool_id') for packet in result_packets if isinstance(packet, dict)}),
                'evidence_pins': sorted(covered), 'evidence_places': all_evidence_places,
                'missing_pins': missing_pins,
                'missing_coverage': tool.get('missing_coverage') or [],
                'api_failures': tool.get('failures') or [],
                'external_sources_present': bool(result.get('sources') or result_packets),
                'laya_ran': laya_trace.get('laya_question_type') == 'choice',
                'brain_planned': bool(routing.get('brain_recipe') or routing.get('brain_planning_fallback')),
                'coordinate_dispatched': bool(result_packets) and all(
                    ((packet.get('data', {}).get('request_scope') or {}).get('latitude') == points[ord(packet['data']['map_pin']) - 65]['lat']
                     and (packet['data'].get('request_scope') or {}).get('longitude') == points[ord(packet['data']['map_pin']) - 65]['lon'])
                    if packet.get('data', {}).get('map_pin') else
                    ((packet.get('data', {}).get('request_scope') or {}).get('start') == f"{points[0]['lon']},{points[0]['lat']}"
                     and (packet['data'].get('request_scope') or {}).get('end') == f"{points[1]['lon']},{points[1]['lat']}")
                    for packet in result_packets),
            })
            entry['expected_provider_ids'] = EXPECTED_PROVIDERS[case['id']]
            entry['provider_capabilities_match'] = set(entry['provider_ids']) == set(entry['expected_provider_ids'])
            entry['synthesis_complete'] = '依頼された分析は未完了' not in entry['response']
            if not result_packets:
                entry['outcome'] = 'NO_API_EVIDENCE'
            elif entry['missing_pins'] or entry['missing_coverage'] or entry['api_failures']:
                entry['outcome'] = 'PARTIAL_OR_PROVIDER_FAILURE'
            elif not entry['provider_capabilities_match']:
                entry['outcome'] = 'WRONG_PROVIDER_CAPABILITY'
            elif case['coordinate_sensitive'] and not entry['coordinate_dispatched']:
                entry['outcome'] = 'EVIDENCE_RETURNED_COORDINATE_REQUIREMENT_UNMET'
            elif not entry['synthesis_complete']:
                entry['outcome'] = 'GROUNDED_SYNTHESIS_FAILED'
            else:
                entry['outcome'] = 'ROUTING_AND_SCOPE_PASS_CONTENT_REVIEW_REQUIRED'
            conversation = host.db.conversation(result['conversation_id'])
            (profile / f'{case["id"]}-conversation.json').write_text(
                json.dumps(conversation, ensure_ascii=False, indent=2, default=str))
        except Exception as exc:
            entry.update({'outcome': 'EXECUTION_ERROR', 'error': f'{type(exc).__name__}: {str(exc)[:500]}'})
        entry['elapsed_seconds'] = round(time.monotonic() - started, 2)
        outcomes.append(entry)
        (profile / f'{case["id"]}.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2, default=str))
        print(json.dumps({k: entry.get(k) for k in ('id', 'outcome', 'provider_ids', 'missing_pins', 'elapsed_seconds', 'error')}, ensure_ascii=False), flush=True)

counts: dict[str, int] = {}
for item in outcomes:
    counts[item['outcome']] = counts.get(item['outcome'], 0) + 1
summary = {
    'status': 'COMPLETED' if len(outcomes) == len(cases) else 'INCOMPLETE',
    'case_count': len(cases), 'completed_requests': len(outcomes), 'outcomes': counts,
    'elapsed_seconds': round(time.monotonic() - run_started, 2),
    'mock_used': False, 'entrypoint': '/api/chat', 'profile': str(profile),
    'actual_models': {'embedding': 'Ollama embeddinggemma:latest', 'brain': 'Ollama ornith-1.5:9b', 'laya': 'local OLCR Laya'},
    'content_acceptance': 'Routing/scope checks are automated; task-specific response content is reviewed separately.',
}
(profile / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(profile / 'results.json').write_text(json.dumps(outcomes, ensure_ascii=False, indent=2, default=str))
print('FINAL_SUMMARY=' + json.dumps(summary, ensure_ascii=False), flush=True)
