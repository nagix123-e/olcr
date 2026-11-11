#!/usr/bin/env python3
"""Live isolated map chat: actual Embedding/Laya/Brain and official statistics.

No mocks, credentials or user conversation DB. Writes public evidence only.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
profile = Path(tempfile.mkdtemp(prefix='olcr-map-statistics-', dir='/private/tmp'))
os.environ['OLCR_DB_PATH'] = str(profile/'test.sqlite')
os.environ.setdefault('OLCR_COMPONENTS_ROOT', str(Path.home()/'Library/Application Support/OLCR/components'))
os.environ.setdefault('OLCR_LAYA_DEVICE', 'cpu')
os.environ['OLCR_VECTOR_ENABLED'] = 'true'
os.environ.setdefault('OLCR_GRAPHIFY_ENABLED', 'false')

from fastapi.testclient import TestClient
from olcr_api import app as host
from olcr_api.brain_models import brain_scope

configuration = {'mode':'unified', 'selections':{slot:'ollama:ornith-1.5:9b' for slot in ('brain','coder','short_coding','general_inference')},
                 'efforts':{slot:None for slot in ('brain','coder','short_coding','general_inference')}, 'fallback_models':[]}
host.brain_models.save(configuration)
settings = host.settings.with_overrides({'external_access_enabled':True, 'task_manager_enabled':False,
                                        'conversation_memory_enabled':False, 'web_mode':'off'})
host.settings = settings
host.runtime.settings = settings
host.brain_models.settings = settings
payload = {'message':'ピンの二地点における人口密度を、同じ最新の国勢調査年の総人口と同年総面積を使って比較して。地域範囲、出典、年、単位、差と倍率も示して。',
           'project_id':None, 'location_map':{'type':'location_map','schema_version':1,'points':[
               {'lat':35.6812,'lon':139.7671,'name':'日本, 東京都, 千代田区'},
               {'lat':34.6937,'lon':135.5023,'name':'日本, 大阪府, 大阪市'}]}}
print('LIVE_PROFILE='+str(profile), flush=True)
print('LIVE_LAYA_READY='+str(host.laya_provider.loaded), flush=True)
started=time.monotonic()
with brain_scope(configuration), TestClient(host.app) as client:
    response = client.post('/api/chat', json=payload)
    response.raise_for_status()
    result = response.json()
record = host.db.conversation(result['conversation_id'])
(profile/'request.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
(profile/'response.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
(profile/'conversation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
text = result.get('response','')
tool = result.get('tool') or {}
task = result.get('task') or {}
routing = next((item.get('api_routing') for item in task.get('selected_context', []) if item.get('api_routing')), {})
checks = {'http_200':response.status_code==200, 'laya_loaded':host.laya_provider.loaded,
          'laya_inference_ran':host.laya_provider.local.last_trace.get('laya_question_type')=='choice',
          'both_places':all(s in text for s in ('千代田区','大阪市')), 'official_source':'dashboard.e-stat.go.jp' in text,
          'density_unit':'人/km²' in text, 'same_year':'2020' in text,
          'actual_population':all(s in text for s in ('66,680','2,752,412')), 'comparison':'倍' in text,
          'scope':'行政区域' in text, 'not_cpi':'消費者物価指数' not in text,
          'two_real_evidence_packets':len(tool.get('results', []))==2,
          'no_missing_coverage':not tool.get('missing_coverage') and '未取得の分析項目' not in text,
          'embedding_used':routing.get('retrieval')=='embedding',
          'brain_recipe_validated':bool(routing.get('brain_recipe')) and not routing['brain_recipe'].get('planner_fallback'),
          'actual_model_calls':bool(task.get('model_calls')),
          'no_provider_failure':'取得できなかったAPI' not in text}

summary={'status':'PASS' if all(checks.values()) else 'FAIL','checks':checks,'elapsed_seconds':round(time.monotonic()-started,2),
         'mock_used':False,'entrypoint':'/api/chat','profile':str(profile),
         'laya_last_outcome':host.laya_provider.local.last_trace.get('laya_decision_outcome'),
         'analysis_renderer':'existing deterministic tabular analysis',
         'routing_note':'The live Laya evaluation may abstain; the existing validated domain recovery then supplies the provider candidate to the Brain recipe.'}
(profile/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2));print(text)
sys.exit(0 if all(checks.values()) else 1)
