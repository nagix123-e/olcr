"""User-selected generation models, downstream of the existing task router.

Auxiliary semantic/embedding/vision roles never consult this configuration.
Configuration is conversation-scoped and credentials never enter SQLite.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import calendar
import json
import os
from pathlib import Path
import threading
import time
from urllib import request, error
from typing import Any

from .model_registry import DEFAULT_MANIFEST_PATH, ModelBinding, ModelRegistry, ROLE_TO_MODEL
from .ollama import ModelFailure
from .brain_providers import BrainProviders
from .model_assets import asset_path

ORNITH = 'ollama:ornith-1.5:9b'
ORNITH_MLX = 'mlx:ornith_1_5_9b_8bit'
BONSAI = 'mlx:ternary_bonsai_2_27b'
INITIAL = {'mode': 'split', 'selections': {'brain': ORNITH_MLX, 'coder': ORNITH_MLX,
            'short_coding': BONSAI, 'general_inference': BONSAI},
           'efforts': {'brain': None, 'coder': None, 'short_coding': None, 'general_inference': None},
           'fallback_models': [], 'quick_models': {}}
# General conversation follows the inference selection; implementation follows coding.
ROLE_SLOTS = {'normal_chat': 'general_inference', 'long_implementation': 'coder',
              'short_coding_fix_debug': 'short_coding', 'planner_replanner': 'general_inference'}
PURPOSE_LABELS = {
    'semantic_embedding': ('Embedding', 'Embedding'),
    'typed_local_decisions': ('Routing', '振り分け'),
    'planner_replanner': ('Planning and inference', '計画・推論'),
    'short_coding_fix_debug': ('Short coding and fixes', '短いコーディング・修正'),
    'long_implementation': ('Implementation', '実装'),
    'general_image': ('Vision · light / fast', '画像理解 · light / fast'),
    'gui_grounding': ('Vision · heavy / UI', '画像・画面理解 · heavy / UI'),
    'translation': ('Translation', '翻訳'),
    'normal_chat': ('General chat', '汎用チャット'),
}
PURPOSE_MODEL_LABELS = {
    'embeddinggemma': 'EmbeddingGemma',
    'laya_multilingual': 'Laya multilingual',
    'ternary_bonsai_2_27b': 'Ternary Bonsai 2 · 27B',
    'ornith_1_5_9b': 'Ornith 1.5 · 9B',
    'ui_venus_mlx_6bit': 'UI-Venus · heavy / UI',
    'fastvlm_mlx_bf16': 'FastVLM · light / fast',
    'qwen3_vl_2b_instruct': 'Qwen3-VL',
    'plamo_3_translate_2b': 'PLaMo 3 Translate 2B',
}
_config: ContextVar[dict | None] = ContextVar('brain_configuration', default=None)

@contextmanager
def brain_scope(configuration: dict):
    token = _config.set(configuration)
    try:
        yield
    finally:
        _config.reset(token)

def active_configuration(fallback: dict | None = None) -> dict:
    return _config.get() or fallback or json.loads(json.dumps(INITIAL))

class BrainModels:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self._inventory: dict[str, dict] = {}
        self._inventory_at = 0.0
        self._lock = threading.RLock()
        with db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS brain_configurations(scope TEXT PRIMARY KEY, configuration_json TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS brain_providers(id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL)')
            con.execute('''CREATE TABLE IF NOT EXISTS brain_usage_events(
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, model_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                created_at REAL NOT NULL, call_index INTEGER NOT NULL DEFAULT 0,
                UNIQUE(task_id, model_id, call_index))''')
            # Additive migration: retain old reported-token events and resets.
            columns={row['name'] for row in con.execute('PRAGMA table_info(brain_usage_events)')}
            for name,definition in {'total_tokens':'INTEGER', 'tokens_reported':'INTEGER NOT NULL DEFAULT 1',
                                    'breakdown_reported':'INTEGER NOT NULL DEFAULT 1',
                                    'status':"TEXT NOT NULL DEFAULT 'success'"}.items():
                if name not in columns: con.execute(f'ALTER TABLE brain_usage_events ADD COLUMN {name} {definition}')
            con.execute('CREATE INDEX IF NOT EXISTS brain_usage_model_time ON brain_usage_events(model_id, created_at)')
            con.execute('CREATE TABLE IF NOT EXISTS brain_usage_resets(model_id TEXT PRIMARY KEY, reset_at REAL NOT NULL)')
            con.execute('''CREATE TABLE IF NOT EXISTS brain_provider_rate_limits(
                model_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, values_json TEXT NOT NULL)''')
        self.providers = BrainProviders(db, settings, usage_recorder=self.record_api_usage)

    def preflight(self,model_id,external_enabled=None):
        from .model_availability import availability
        if isinstance(model_id,str) and model_id.startswith('api:'):
            _,provider,model=model_id.split(':',2)
            return self.providers.model_availability(provider,model,external_enabled)
        if isinstance(model_id,str) and model_id.startswith(('mlx:','ollama:')):
            cached=self.providers.availability_store.get('local',0,self.settings.ollama_endpoint if model_id.startswith('ollama:') else 'mlx',model_id)
            if cached['state']=='unavailable':return cached
        if model_id in {BONSAI,ORNITH_MLX}:
            root=Path(__file__).resolve().parents[2]
            python=Path(os.environ.get('OLCR_MLX_PYTHON',str(root/'.venv-mlx/bin/python'))).expanduser()
            if not python.is_file():return availability('unavailable','local_runtime_missing')
            if model_id==BONSAI:
                pack=asset_path('bonsai/Ternary-Bonsai-2-27B-mlx-2bit')
                from .mlx_profile_runtime import available as mlx_profile_runtime_available
                installed=(mlx_profile_runtime_available() and (pack/'runtime').is_dir()
                           and any(pack.glob('*.safetensors')))
            else:
                pack=asset_path('ornith/Ornith-1.5-9B-MLX-8bit')
                installed=(pack/'config.json').is_file() and (pack/'model.safetensors.index.json').is_file() and len(list(pack.glob('model-*-of-*.safetensors')))==2
            return availability('available','local_files_present') if installed else availability('unavailable','local_model_missing')
        if isinstance(model_id, str) and model_id.startswith('mlx:'):
            from .model_profiles import _data
            descriptor = next((row for row in _data()['models'].values() if row.get('brain_id') == model_id), None)
            if descriptor:
                root=Path(__file__).resolve().parents[2]
                python=Path(os.environ.get('OLCR_MLX_PYTHON',str(root/'.venv-mlx/bin/python'))).expanduser()
                assets=getattr(getattr(self,'purpose_models',None),'downloads',None)
                entry=(assets.presets().get(descriptor['id']) if assets else None)
                installed=bool(entry and assets.installed(entry))
                return availability('available','local_files_present') if installed and python.is_file() else availability('unavailable','local_model_missing')
        if isinstance(model_id,str) and model_id.startswith('ollama:'):
            # Inventory only; never load a model or send a completion as a probe.
            now=time.time();cached=getattr(self,'_preflight_ollama',None)
            if not cached or now-cached[0]>=10:
                try:
                    with request.urlopen(self.settings.ollama_endpoint.rstrip('/')+'/api/tags',timeout=1) as response:
                        names=[item.get('name') for item in json.load(response).get('models',[])]
                    cached=(now,names,False)
                except (error.URLError,TimeoutError,ValueError,OSError):cached=(now,[],True)
                self._preflight_ollama=cached
            if cached[2]:return availability('unavailable','local_service_unavailable',cached[0]+10)
            name=model_id.removeprefix('ollama:')
            if name in cached[1] or name+':latest' in cached[1]:return availability('available','local_model_installed')
            return availability('unavailable','local_model_missing',cached[0]+10)
        return availability()

    def note_local_result(self,model_id,failure=None):
        if not isinstance(model_id,str) or not model_id.startswith(('mlx:','ollama:')):return
        store=self.providers.availability_store;base=self.settings.ollama_endpoint if model_id.startswith('ollama:') else 'mlx'
        if failure is None:store.success('local',0,base,model_id)
        elif failure.category in {'timeout','unavailable'}:store.failure('local',0,base,model_id,failure)

    def configuration(self, conversation_id: str | None = None) -> dict:
        scope = conversation_id or 'global'
        with self.db.connect() as con:
            row = con.execute('SELECT configuration_json FROM brain_configurations WHERE scope=?', (scope,)).fetchone()
            if row is None and scope != 'global':
                row = con.execute("SELECT configuration_json FROM brain_configurations WHERE scope='global'").fetchone()
        value = json.loads(row[0]) if row else json.loads(json.dumps(INITIAL))
        # Existing profiles predate per-role effort controls.
        value.setdefault('efforts', {})
        value.setdefault('fallback_models', [])
        value.setdefault('quick_models', {})
        for role in INITIAL['efforts']:
            value['efforts'].setdefault(role, None)
        return value

    def save(self, value: dict, conversation_id: str | None = None) -> dict:
        if value.get('mode') not in {'split', 'unified'} or set(value.get('selections', {})) != set(INITIAL['selections']):
            raise ValueError('Invalid Brain configuration')
        catalog = {m['id']: m for m in self.catalog()['models']}
        current = self.configuration(conversation_id)
        for slot, model in value['selections'].items():
            changed = model != current['selections'].get(slot)
            if not isinstance(model, str) or not model or (changed and model not in catalog):
                raise ValueError('Choose an installed general language model or configured provider model')
            if changed and not catalog[model]['available']:
                raise ValueError('Selected Brain model is unavailable; previous configuration was preserved')
        supplied_efforts = value.get('efforts')
        requested_efforts = supplied_efforts if supplied_efforts is not None else current['efforts']
        if not isinstance(requested_efforts, dict) or set(requested_efforts) != set(INITIAL['efforts']):
            raise ValueError('Invalid Brain effort configuration')
        efforts = {}
        for slot, model_id in value['selections'].items():
            model = catalog.get(model_id, {})
            local = model_id.startswith(('mlx:', 'ollama:')) or model.get('local') is True
            supported = ['off', 'auto', 'on'] if local else (model.get('effort_levels') or [])
            selected_effort = requested_efforts.get(slot)
            unchanged_effort = (model_id == current['selections'].get(slot)
                                and selected_effort == current['efforts'].get(slot))
            if selected_effort is not None and selected_effort not in supported and not unchanged_effort:
                raise ValueError('Selected effort is not supported by this model')
            if model_id != current['selections'][slot] and selected_effort is None:
                selected_effort = None if local else (model.get('effort_default') if model.get('effort_default') in supported else None)
            efforts[slot] = selected_effort
        fallbacks = value.get('fallback_models', current.get('fallback_models', []))
        if (not isinstance(fallbacks, list) or len(fallbacks) > 16
                or any(not isinstance(model, str) or not model for model in fallbacks)
                or len(set(fallbacks)) != len(fallbacks)):
            raise ValueError('Choose distinct available models for the fallback sequence')
        # Catalog availability is transient (credentials, network, Ollama, or
        # a disconnected provider). Preserve existing choices while changing
        # another slot; only newly added fallback candidates must be available.
        previous_fallbacks = current.get('fallback_models', [])
        if any(model not in previous_fallbacks and
               (model not in catalog or not catalog[model]['available']) for model in fallbacks):
            raise ValueError('Choose distinct available models for the fallback sequence')
        quick_models = value.get('quick_models')
        if quick_models is None: quick_models = current.get('quick_models', {})
        if (not isinstance(quick_models, dict) or len(quick_models)>256
                or any(not isinstance(key,str) or type(enabled) is not bool for key,enabled in quick_models.items())):
            raise ValueError('Invalid Quick mode configuration')
        for model_id, enabled in quick_models.items():
            if enabled and not catalog.get(model_id, {}).get('quick_supported') and not current.get('quick_models', {}).get(model_id):
                raise ValueError('Quick mode is not supported by this model')
        value = {'mode': value['mode'], 'selections': dict(value['selections']), 'efforts': efforts,
                 'fallback_models': fallbacks, 'quick_models': dict(quick_models)}
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO brain_configurations VALUES(?,?)',
                        (conversation_id or 'global', json.dumps(value)))
        return value

    def quick_for_model(self, model_id: str) -> bool:
        configuration = _config.get() or self.configuration()
        return configuration.get('quick_models', {}).get(model_id) is True

    def effort_for_role(self, role: str) -> str | None:
        configuration = _config.get() or self.configuration()
        slot = ROLE_SLOTS.get(role)
        if slot is None:
            return None
        if configuration.get('mode') == 'unified':
            slot = 'brain'
        selected = configuration.get('selections', {}).get(slot)
        effort = configuration.get('efforts', {}).get(slot)
        if not isinstance(effort, str):
            return None
        model = next((item for item in self.catalog()['models'] if item.get('id') == selected), None)
        return effort if model and effort in (model.get('effort_levels') or []) else None

    def _local_models(self) -> list[dict]:
        with self._lock:
            if time.monotonic() - self._inventory_at > 10:
                inventory = {}
                try:
                    with request.urlopen(self.settings.ollama_endpoint.rstrip('/') + '/api/tags', timeout=3) as response:
                        tags = json.load(response).get('models', [])
                    for tag in tags:
                        name = tag.get('name')
                        if not isinstance(name, str): continue
                        payload = json.dumps({'model': name}).encode()
                        req = request.Request(self.settings.ollama_endpoint.rstrip('/') + '/api/show',
                                              data=payload, headers={'Content-Type': 'application/json'})
                        with request.urlopen(req, timeout=3) as response: info = json.load(response)
                        capabilities = set(info.get('capabilities') or [])
                        # Dedicated perception, embedding, audio and reranking assets are not Brains.
                        excluded = {'embedding', 'audio', 'tts', 'rerank', 'reranking'}
                        known_brain = name == 'ornith-1.5:9b'
                        if not known_brain and ('completion' not in capabilities or capabilities & excluded): continue
                        inventory['ollama:' + name] = {'id': 'ollama:' + name,
                            'label': 'Ornith 1.5 · 9B' if known_brain else name,
                            'provider': 'Ollama', 'available': True, 'kind': 'language', 'local': True,
                            'revision': tag.get('digest') or 'UNKNOWN', 'size_bytes': tag.get('size'),
                            'vision_supported': 'vision' in capabilities}
                except (error.URLError, TimeoutError, ValueError, OSError):
                    pass
                self._inventory = inventory
                self._inventory_at = time.monotonic()
            models = [dict(m) for m in self._inventory.values()]
        if not any(m['id'] == ORNITH for m in models):
            models.append({'id': ORNITH, 'label': 'Ornith 1.5 · 9B', 'provider': 'Ollama',
                           'available': False, 'kind': 'language', 'local': True})
        root = Path(__file__).resolve().parents[2]
        mlx_python = Path(os.environ.get('OLCR_MLX_PYTHON', str(root / '.venv-mlx/bin/python'))).expanduser()
        pack = asset_path('bonsai/Ternary-Bonsai-2-27B-mlx-2bit')
        from .mlx_profile_runtime import available as mlx_profile_runtime_available
        available = (mlx_python.is_file() and mlx_profile_runtime_available()
                     and (pack / 'runtime').is_dir() and any(pack.glob('*.safetensors')))
        models.append({'id': BONSAI, 'label': 'Ternary Bonsai 2 · 27B', 'provider': 'MLX',
                       'available': available, 'kind': 'language', 'local': True})
        ornith_pack = asset_path('ornith/Ornith-1.5-9B-MLX-8bit')
        ornith_available = (mlx_python.is_file()
                            and (ornith_pack / 'config.json').is_file()
                            and (ornith_pack / 'model.safetensors.index.json').is_file()
                            and len(list(ornith_pack.glob('model-*-of-*.safetensors'))) == 2)
        models.append({'id': ORNITH_MLX, 'label': 'Ornith 1.5 · 9B · MLX 8-bit',
                       'provider': 'MLX', 'available': ornith_available,
                       'kind': 'language', 'local': True})
        try:
            from .model_profiles import _data
            downloads=getattr(getattr(self,'purpose_models',None),'downloads',None)
            for model in _data()['models'].values():
                model_id=model.get('brain_id')
                if not model_id or any(item['id']==model_id for item in models):
                    continue
                descriptor=downloads.presets().get(model['id']) if downloads else None
                available=bool(descriptor and downloads.installed(descriptor))
                if model_id.startswith('ollama:'):
                    available=any(item['id']==model_id and item.get('available') for item in models)
                models.append({'id':model_id,'label':model.get('label') or model_id,
                    'provider':model.get('runtime') or 'Local','available':available,
                    'kind':'language','local':True,'profile_model_id':model['id']})
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
        for model in models:
            model.update(effort_levels=['off', 'auto', 'on'], effort_default='auto', effort_transport='local_thinking')
        return models

    def catalog(self) -> dict:
        # Settings are rebuilt when the user changes External Access. Keep the
        # provider catalog aligned with that live Settings instance rather than
        # the object captured when this manager was created at backend startup.
        self.providers.settings = self.settings
        models, providers = self.providers.catalog()
        return {'models': self._local_models() + models, 'providers': providers,
                'purpose_models': self._purpose_models()}

    def _purpose_models(self) -> list[dict]:
        """Expose the canonical model assigned to every internal runtime role.

        This inventory is separate from selectable Brain language models: embedding,
        routing, and perception assets must be visible in Settings without becoming
        valid candidates for a generative Brain slot.
        """
        registry = ModelRegistry(getattr(self.settings, 'model_manifest_path', None)
                                  or DEFAULT_MANIFEST_PATH)
        entries = registry.entries()
        result = []
        for role in PURPOSE_LABELS:
            model_id = ROLE_TO_MODEL.get(role)
            if model_id is None:
                continue
            entry = entries.get(model_id)
            if entry is None:
                continue
            english, japanese = PURPOSE_LABELS.get(role, (role, role))
            deployment = str(entry.get('deployment_repo') or model_id)
            if deployment.startswith('ollama/'):
                model = deployment.removeprefix('ollama/')
            else:
                model = model_id
            result.append({
                'id': model_id,
                'role': role,
                'purpose': japanese,
                'purpose_en': english,
                'label': PURPOSE_MODEL_LABELS.get(model_id, model),
                'provider': str(entry.get('runtime') or 'Local'),
                'deployment': deployment,
                'runtime': str(entry.get('runtime') or ''),
                'local_path': str(entry.get('local_path') or ''),
                'ready': str(entry.get('ready') or '').upper() == 'YES',
            })
            choices = getattr(self, 'purpose_models', None)
            if choices is not None and role in choices.bindings():
                selected = choices.choice(role)
                result[-1]['selected'] = selected
                if selected['protocol'] != 'preset':
                    result[-1].update(id=selected['model'], label=selected['model'],
                                     provider=selected.get('provider', selected['protocol']),
                                     deployment=selected.get('base_url', ''),
                                     runtime=selected['protocol'], local_path='', ready=True)
        return result

    def selected_model_for_role(self, role: str | None,
                                configuration: dict | None = None) -> str | None:
        slot = ROLE_SLOTS.get(role or '')
        if slot is None:
            return None
        configuration = configuration or _config.get() or self.configuration()
        if configuration.get('mode') == 'unified':
            slot = 'brain'
        return configuration.get('selections', {}).get(slot)

    def record_api_usage(self, request_id: str, model_id: str, status: str,
                         input_tokens=None, output_tokens=None, total_tokens=None,
                         provider_rate_limits=None, created_at=None) -> None:
        self.record_usage('api-request:'+request_id, model_id, input_tokens, output_tokens,
                          created_at=created_at, total_tokens=total_tokens, status=status,
                          provider_rate_limits=provider_rate_limits, replace=True)

    def record_usage(self, task_id: str, model_id: str | None, input_tokens: Any,
                     output_tokens: Any, created_at: float | None = None,
                     call_index: int = 0, provider_rate_limits: dict | None = None, *,
                     usage_recorded: bool = False, total_tokens=None, status='success', replace=False) -> None:
        if not model_id or not isinstance(task_id, str) or not task_id:
            return
        valid=lambda value:isinstance(value,int) and not isinstance(value,bool) and value>=0
        breakdown=valid(input_tokens) and valid(output_tokens)
        known_sum=(input_tokens if valid(input_tokens) else 0)+(output_tokens if valid(output_tokens) else 0)
        total=total_tokens if valid(total_tokens) and total_tokens>=known_sum else known_sum if breakdown else None
        with self.db.connect() as con:
            if not usage_recorded:
                # Unknown counts are explicitly marked, rather than displayed as
                # zero consumption. A request is counted before network I/O.
                conflict=('DO UPDATE SET input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, '
                          'total_tokens=excluded.total_tokens, tokens_reported=excluded.tokens_reported, '
                          'breakdown_reported=excluded.breakdown_reported, status=excluded.status') if replace else 'DO NOTHING'
                con.execute(f'''INSERT INTO brain_usage_events
                    (task_id,model_id,input_tokens,output_tokens,created_at,call_index,total_tokens,tokens_reported,breakdown_reported,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,model_id,call_index) {conflict}''',
                    (task_id,model_id,input_tokens if valid(input_tokens) else 0,output_tokens if valid(output_tokens) else 0,
                     time.time() if created_at is None else created_at,call_index,total,int(total is not None),int(breakdown and total==known_sum),status))
            clean = {str(k): str(v)[:80] for k, v in (provider_rate_limits or {}).items()
                     if str(k).startswith(('x-ratelimit-', 'x-goog-quota-')) or str(k) == 'retry-after'}
            if clean:
                con.execute('INSERT OR REPLACE INTO brain_provider_rate_limits VALUES(?,?,?)',
                            (model_id, time.time(), json.dumps(clean)))

    def usage(self) -> dict:
        """Return measured calls and reported tokens this UTC week or after a reset.

        Provider quota and billing totals are deliberately null unless a provider
        exposes an authoritative value; token counts are local request telemetry.
        """
        now = time.time()
        utc = time.gmtime(now)
        week_start = calendar.timegm((utc.tm_year, utc.tm_mon, utc.tm_mday - utc.tm_wday,
                                      0, 0, 0, 0, 0, 0))
        with self.db.connect() as con:
            rows = con.execute('''WITH windowed AS (
                SELECT e.* FROM brain_usage_events e LEFT JOIN brain_usage_resets r ON r.model_id=e.model_id
                WHERE e.created_at>=MAX(?,COALESCE(r.reset_at,0))
            ), counts AS (
                SELECT model_id,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                    SUM(CASE WHEN tokens_reported THEN COALESCE(total_tokens,input_tokens+output_tokens) ELSE 0 END) AS total_tokens,
                    COUNT(*) AS request_count,SUM(tokens_reported) AS token_reported_requests,
                    SUM(CASE WHEN tokens_reported AND NOT breakdown_reported THEN 1 ELSE 0 END) AS total_only_requests,
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_requests,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_requests,MAX(created_at) AS last_used
                FROM windowed GROUP BY model_id
            ) SELECT m.model_id,COALESCE(c.input_tokens,0) AS input_tokens,COALESCE(c.output_tokens,0) AS output_tokens,
                COALESCE(c.total_tokens,0) AS total_tokens,COALESCE(c.request_count,0) AS request_count,
                COALESCE(c.token_reported_requests,0) AS token_reported_requests,
                COALESCE(c.total_only_requests,0) AS total_only_requests,
                COALESCE(c.failed_requests,0) AS failed_requests,COALESCE(c.pending_requests,0) AS pending_requests,
                c.last_used,MAX(?,COALESCE(r.reset_at,0)) AS window_start,
                q.observed_at AS rate_limits_observed_at,q.values_json AS rate_limits_json
                FROM (SELECT model_id FROM brain_usage_events UNION SELECT model_id FROM brain_provider_rate_limits) m
                LEFT JOIN counts c ON c.model_id=m.model_id
                LEFT JOIN brain_usage_resets r ON r.model_id=m.model_id
                LEFT JOIN brain_provider_rate_limits q ON q.model_id=m.model_id''', (week_start,week_start)).fetchall()
        result = {}
        for row in rows:
            model_id = str(row['model_id'])
            input_tokens, output_tokens = int(row['input_tokens']), int(row['output_tokens'])
            result[model_id] = {
                'input_tokens': input_tokens, 'output_tokens': output_tokens,
                'total_tokens': int(row['total_tokens']),
                **{key:int(row[key]) for key in ('request_count','token_reported_requests','total_only_requests','failed_requests','pending_requests')},
                'unreported_requests': int(row['request_count'])-int(row['token_reported_requests']),
                'window_start': float(row['window_start']), 'window_kind': 'weekly',
                'last_used': float(row['last_used']) if row['last_used'] is not None else None,
                'limit_tokens': None, 'cost_usd': None,
                'provider_rate_limits': json.loads(row['rate_limits_json']) if row['rate_limits_json'] else None,
                'rate_limits_observed_at': float(row['rate_limits_observed_at']) if row['rate_limits_observed_at'] is not None else None,
            }
        return {'models': result, 'week_starts_at': week_start}

    def reset_usage(self, model_id: str) -> dict:
        known = {str(item['id']) for item in self.catalog()['models']}
        if model_id not in known:
            raise ValueError('Unknown Brain model')
        reset_at = time.time()
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO brain_usage_resets(model_id,reset_at) VALUES(?,?)',
                        (model_id, reset_at))
        return {'model_id': model_id, 'reset_at': reset_at}

    def resolve(self, binding: ModelBinding, registry) -> ModelBinding:
        slot = ROLE_SLOTS.get(binding.role)
        if slot is None: return binding
        configuration = _config.get() or self.configuration()
        selected = configuration['selections']['brain' if configuration['mode'] == 'unified' else slot]
        if selected == BONSAI:
            return replace(registry.binding('planner_replanner'), role=binding.role)
        if selected == ORNITH_MLX:
            root = Path(__file__).resolve().parents[2]
            pack = asset_path('ornith/Ornith-1.5-9B-MLX-8bit')
            candidate = next((m for m in self.catalog()['models']
                              if m['id'] == ORNITH_MLX and m['available']), None)
            if candidate is None:
                raise ModelFailure('unavailable', 'Selected Ornith MLX model is not installed')
            return ModelBinding(binding.role, ORNITH_MLX, 'Ornith-1.5-9B-MLX-8bit',
                                'ornith-ai/Ornith-1.5-9B-MLX-8bit',
                                'b4b70543d60c81e7c418a001b74cd4352212bd44',
                                str(pack), 'b4b70543d60c81e7c418a001b74cd4352212bd44',
                                'MLX', str(pack), True, binding.manifest_path,
                                binding.manifest_sha256)
        if selected.startswith('mlx:'):
            from .model_profiles import _data
            descriptor = next((row for row in _data()['models'].values() if row.get('brain_id') == selected), None)
            if descriptor:
                downloads=getattr(getattr(self,'purpose_models',None),'downloads',None)
                entry=downloads.presets().get(descriptor['id']) if downloads else None
                if not entry or not downloads.installed(entry):
                    raise ModelFailure('unavailable','Selected MLX model is not installed')
                pack=asset_path(entry['relative'])
                return ModelBinding(binding.role, selected, descriptor['repo'], descriptor['repo'],
                    descriptor['revision'], str(pack), descriptor['revision'], 'MLX', str(pack), True,
                    binding.manifest_path, binding.manifest_sha256)
        if selected == ORNITH:
            return replace(registry.binding('normal_chat'), role=binding.role)
        if selected.startswith('ollama:'):
            name = selected.removeprefix('ollama:')
            candidate = next((m for m in self.catalog()['models'] if m['id'] == selected and m['available']), None)
            if candidate is None: raise ModelFailure('unavailable', 'Selected Brain model is unavailable; choose an installed model')
            return ModelBinding(binding.role, selected, name, 'CUSTOM', 'UNKNOWN', name,
                                candidate.get('revision', 'UNKNOWN'), 'Ollama', 'Ollama model store', True,
                                binding.manifest_path, binding.manifest_sha256)
        if selected.startswith('api:'):
            _, provider, model = selected.split(':', 2)
            metadata = self.providers.model_metadata(provider, model)
            if not metadata or model not in metadata['model_ids']:
                raise ModelFailure('configuration', 'Selected Brain provider model is not configured')
            return ModelBinding(binding.role, selected, model, 'USER_CONFIGURED', 'UNKNOWN', metadata['base_url'],
                                'UNKNOWN', provider, 'REMOTE', self.providers.configured(provider, metadata.get('account_id', 1)),
                                binding.manifest_path, binding.manifest_sha256)
        raise ModelFailure('configuration', 'Unsupported selected Brain provider')
