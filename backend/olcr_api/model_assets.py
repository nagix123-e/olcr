"""Explicit, resumable acquisition of curated model weights."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from urllib import request
from .model_registry import ModelRegistry, ROLE_TO_MODEL
from .model_runtime_profiles import (
    SCHEMA_VERSION as ASSET_RECORD_SCHEMA_VERSION,
    asset_identity_for_entry,
    make_asset_record,
    read_asset_record,
    runtime_profile_for_asset,
    write_asset_record,
)

ROOT = Path(__file__).resolve().parents[2]


def _size_bytes(entry):
    value = entry.get('artifact_size_bytes_upstream') or entry.get('artifact_size_bytes_deployment')
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    display = entry.get('artifact_size_ollama')
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB|TB)\s*', str(display or ''), re.IGNORECASE)
    if not match:
        return None
    scale = {'KB': 10**3, 'MB': 10**6, 'GB': 10**9, 'TB': 10**12}[match.group(2).upper()]
    return int(float(match.group(1)) * scale)


def asset_path(relative: str) -> Path:
    bundled = ROOT / 'models' / relative
    user = Path(os.environ.get('OLCR_MODEL_ASSETS_DIR', str(ROOT / 'models'))) / relative
    return user if (user / '.olcr-complete.json').is_file() else bundled


class ModelDownloads:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.db_path).parent / 'models'
        os.environ.setdefault('OLCR_MODEL_ASSETS_DIR', str(self.root))
        os.environ.setdefault('OLCR_AUDIO_MODEL_CACHE', str(self.root / 'audio/hub'))
        from .optional_runtimes import runtime as components
        if components.installed('laya'):
            os.environ['OLCR_LAYA_CACHE'] = str(components.path('laya'))
        elif (self.root / 'laya/multilingual/.olcr-complete.json').is_file():
            os.environ['OLCR_LAYA_CACHE'] = str(self.root / 'laya')
        if not os.environ.get('OLCR_LAYA_CACHE'):
            bundled = ROOT / 'models/laya'
            os.environ['OLCR_LAYA_CACHE'] = str(bundled if (bundled / 'multilingual/model.safetensors').is_file() else self.root / 'laya')
        self.jobs = {}
        self.cancel_events = {}
        self.lock = threading.RLock()
        self._ollama_inventory_at = 0.0
        self._ollama_inventory = []

    def _ollama_models(self):
        now = time.monotonic()
        with self.lock:
            if now - self._ollama_inventory_at < 3:
                return list(self._ollama_inventory)
            try:
                with request.urlopen(self.settings.ollama_endpoint.rstrip('/') + '/api/tags', timeout=3) as response:
                    self._ollama_inventory = json.load(response).get('models', [])
            except Exception:
                self._ollama_inventory = []
            self._ollama_inventory_at = time.monotonic()
            return list(self._ollama_inventory)

    def presets(self):
        registry = ModelRegistry(self.settings.model_manifest_path)
        ids = sorted(set(ROLE_TO_MODEL.values()))
        rows = {}
        for model_id in ids:
            entry = registry.entries()[model_id]
            deployment = entry['deployment_repo']
            local = str(entry.get('local_path', '')).removeprefix('olcr/').removeprefix('models/')
            if model_id == 'laya_multilingual': local = 'laya/multilingual'
            rows[model_id] = {'id': model_id, 'repo': deployment, 'revision': entry['deployment_revision'],
                              'relative': local, 'size_bytes': _size_bytes(entry), 'license': entry.get('license', '')}
        rows['ornith_1_5_9b_8bit'] = {'id': 'ornith_1_5_9b_8bit', 'repo': 'ornith-ai/Ornith-1.5-9B-MLX-8bit',
            'revision': 'b4b70543d60c81e7c418a001b74cd4352212bd44', 'relative': 'ornith/Ornith-1.5-9B-MLX-8bit',
            'size_bytes': 10_000_000_000, 'weight_gb': 9.4, 'license': '',
            'runtime': 'MLX-LM 0.32.0', 'managed': True}
        from .audio_transcription import _MODELS
        for language, model in _MODELS.items():
            rows['transcription_' + language] = {'id': 'transcription_' + language, 'repo': model, 'revision': None,
                'relative': '', 'size_bytes': 3_700_000_000, 'license': ''}
        try:
            from .model_profiles import _data
            for model_id, model in _data()['models'].items():
                if model_id in rows or model.get('type') not in {'huggingface', 'ollama'}:
                    continue
                row = {**model, 'id': model_id, 'managed': True}
                if model['type'] == 'ollama':
                    row['repo'] = 'ollama/' + model['repo']
                    row['relative'] = ''
                rows[model_id] = row
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
        for row in rows.values():
            try:
                identity = asset_identity_for_entry(row)
                profile = runtime_profile_for_asset(identity.asset_id, identity.revision)
            except ValueError:
                # Unknown assets remain visible as downloads, but must not be
                # presented as runtime-compatible until explicitly profiled.
                continue
            row.update(
                artifact_format=identity.artifact_format,
                loader_id=identity.loader_id,
                runtime_profile_id=identity.runtime_profile_id,
                package_lock_id=profile.package_lock_id,
            )
        return rows

    def _asset_directories(self, entry):
        relative = str(entry.get('relative') or '').strip()
        if not relative:
            return []
        configured_root = Path(os.environ.get('OLCR_MODEL_ASSETS_DIR', str(self.root)))
        candidates = [configured_root / relative, ROOT / 'models' / relative]
        result = []
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
        return result

    @staticmethod
    def _asset_files_present(entry, path):
        if entry['id'] == 'laya_multilingual':
            return all((path / name).is_file() for name in (
                'model.safetensors', 'rl_agent_config.json', 'tokenizer/tokenizer.json'))
        if not (path / 'config.json').is_file():
            return False
        if entry['id'].startswith('whisper_'):
            return any((path / name).is_file() and (path / name).stat().st_size > 0
                       for name in ('weights.npz', 'weights.safetensors', 'model.safetensors'))
        index = path / 'model.safetensors.index.json'
        if index.is_file():
            try:
                files = set(json.loads(index.read_text())['weight_map'].values())
                return bool(files) and all((path / file).is_file() and (path / file).stat().st_size > 0
                                           for file in files)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        return any(file.stat().st_size > 0 for file in path.glob('*.safetensors'))

    def _asset_directory_with_files(self, entry):
        return next((path for path in self._asset_directories(entry)
                     if self._asset_files_present(entry, path)), None)

    def _is_user_asset_path(self, path):
        try:
            Path(path).resolve().relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _record_asset_payload(self, entry, path):
        """Migrate metadata only after the model's required files validate."""
        identity = asset_identity_for_entry(entry)
        current = read_asset_record(path, identity)
        if current is not None and current.schema_version == ASSET_RECORD_SCHEMA_VERSION:
            return current
        if not self._asset_files_present(entry, path):
            raise RuntimeError('MODEL_DOWNLOAD_INCOMPLETE')
        record = make_asset_record(path, identity)
        write_asset_record(path, record)
        return record

    def downloaded(self, entry):
        """Return whether model payload exists, independent of runtime readiness."""
        if entry['id'].startswith('transcription_'):
            from .audio_transcription import _snapshot
            try:
                path = _snapshot(entry['repo'])
                decoder = path / 'audio_detokenizer'
                return all((decoder / name).is_file() for name in ('config.json', 'model.safetensors'))
            except (RuntimeError, ImportError): return False
        if entry['id'] == 'laya_multilingual':
            cache = Path(os.environ.get('OLCR_LAYA_CACHE', str(self.root / 'laya')))
            return self._asset_files_present(entry, cache / 'multilingual')
        if str(entry.get('repo') or '').startswith('ollama/'):
            rows = self._ollama_models()
            expected_name = str(entry['repo'])[7:]
            revision = str(entry.get('revision') or '')
            return any(row.get('name') == expected_name
                       and (not revision or str(row.get('digest', '')).startswith(revision))
                       for row in rows)
        return self._asset_directory_with_files(entry) is not None

    def installed(self, entry):
        """Return whether downloaded model files are ready for this runtime."""
        if not self.downloaded(entry):
            return False
        if entry['id'].startswith('transcription_') or str(entry.get('repo') or '').startswith('ollama/'):
            return True
        if entry['id'].startswith('whisper_'):
            from .audio_mlx import whisper_enabled
            if not whisper_enabled(): return False
        uses_mlx_vlm = (entry.get('id') in {'ui_venus_mlx_6bit', 'fastvlm_mlx_bf16'}
                        or 'MLX-VLM' in str(entry.get('runtime') or ''))
        uses_mlx_text = (entry.get('id') == 'ternary_bonsai_2_27b'
                         or (entry.get('managed') and 'MLX' in str(entry.get('runtime') or '')))
        if uses_mlx_vlm:
            from .mlx_vlm_runtime import available as mlx_vlm_available
            if not mlx_vlm_available(): return False
        elif uses_mlx_text:
            from .mlx_profile_runtime import available as mlx_profile_available
            if not mlx_profile_available(): return False
        if entry.get('id', '').startswith('plamo_3_translate_'):
            from .mlx_profile_runtime import available as mlx_profile_available
            from .plamo_translation import runtime_available, runtime_python
            if not mlx_profile_available() or not runtime_available(str(runtime_python())): return False
        if entry['id'] == 'ternary_bonsai_2_27b':
            path = self._asset_directory_with_files(entry)
            if path is None or not (path / 'runtime').is_dir(): return False
        return True

    def catalog(self):
        with self.lock:
            jobs = [dict(value) for value in self.jobs.values()]
        return {'presets': [{**entry, 'downloaded': self.downloaded(entry)}
                            for entry in self.presets().values()], 'jobs': jobs}

    def start(self, ids, consent):
        if consent is not True: raise ValueError('MODEL_DOWNLOAD_CONSENT_REQUIRED')
        presets = self.presets()
        if not isinstance(ids, list) or not ids or any(model not in presets for model in ids):
            raise ValueError('MODEL_DOWNLOAD_UNKNOWN_PRESET')
        ids = list(dict.fromkeys(ids))
        with self.lock:
            if any(job['status'] in {'queued', 'downloading'} for job in self.jobs.values()):
                raise ValueError('MODEL_DOWNLOAD_IN_PROGRESS')
            job = {'id': uuid.uuid4().hex, 'models': ids, 'status': 'queued', 'completed': [], 'current': None, 'error': None}
            self.cancel_events[job['id']] = threading.Event()
            self.jobs[job['id']] = job
        threading.Thread(target=self._run, args=(job, presets), daemon=True, name='olcr-model-download').start()
        return dict(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job['status'] not in {'queued', 'downloading'}:
                return False
            self.cancel_events[job_id].set()
            job['status'] = 'cancelling'
            return True

    def _run(self, job, presets):
        cancelled = self.cancel_events[job['id']]
        try:
            for model in job['models']:
                if cancelled.is_set():
                    with self.lock: job.update(status='cancelled', current=None)
                    return
                with self.lock: job.update(status='downloading', current=model)
                entry = presets[model]
                if not self.installed(entry):
                    if self.downloaded(entry):
                        payload_path = self._asset_directory_with_files(entry)
                        if payload_path is not None and self._is_user_asset_path(payload_path):
                            self._record_asset_payload(entry, payload_path)
                        self._prepare_runtime(entry)
                        if entry['id'] == 'ternary_bonsai_2_27b':
                            self._copy_bonsai_runtime(entry, self._asset_directory_with_files(entry))
                    else:
                        self._download(entry)
                if not self.installed(entry): raise RuntimeError('MODEL_DOWNLOAD_INCOMPLETE')
                with self.lock: job['completed'].append(model)
                if cancelled.is_set():
                    with self.lock: job.update(status='cancelled', current=None)
                    return
            with self.lock: job.update(status='complete', current=None)
        except Exception:
            # Provider bodies and filesystem errors can contain private details.
            with self.lock: job.update(status='failed', error='MODEL_DOWNLOAD_FAILED')

    def _download(self, entry):
        if entry['id'] == 'laya_multilingual':
            from .optional_runtimes import runtime as components
            os.environ['OLCR_LAYA_CACHE'] = str(components.ensure('laya'))
            return
        if entry['id'].startswith('transcription_'):
            from .audio_transcription import prepare_audio_model
            prepare_audio_model(entry['id'][-2:]); return
        if entry['repo'].startswith('ollama/'):
            req = request.Request(self.settings.ollama_endpoint.rstrip('/') + '/api/pull',
                data=json.dumps({'model': entry['repo'][7:], 'stream': False}).encode(), headers={'Content-Type': 'application/json'})
            with request.urlopen(req, timeout=7200) as response:
                value = json.load(response)
            if value.get('status') != 'success': raise RuntimeError('MODEL_DOWNLOAD_FAILED')
            with self.lock:
                self._ollama_inventory_at = 0.0
            return
        self._prepare_runtime(entry)
        from huggingface_hub import snapshot_download
        target = self.root / entry['relative']
        if entry['id'] == 'laya_multilingual':
            # Never write into the bundled/recommended read-only asset path.
            target = self.root / 'laya/multilingual'
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=entry['repo'], revision=entry['revision'], local_dir=target)
        if entry['id'] == 'ternary_bonsai_2_27b':
            self._copy_bonsai_runtime(entry, target)
        has_weights = any(target.glob('*.safetensors'))
        if entry['id'].startswith('whisper_'):
            has_weights = has_weights or any((target / name).is_file() for name in ('weights.npz', 'weights.safetensors', 'model.safetensors'))
        if not has_weights: raise RuntimeError('MODEL_DOWNLOAD_INCOMPLETE')
        self._record_asset_payload(entry, target)
        if entry['id'] == 'laya_multilingual':
            from .laya_runtime import LAYA_DEFAULT_MODEL_REVISION
            (target.parent / 'olcr-laya-cache.json').write_text(json.dumps({'model_revision': LAYA_DEFAULT_MODEL_REVISION, 'multilingual': True}))
            os.environ['OLCR_LAYA_CACHE'] = str(target.parent)

    def _prepare_runtime(self, entry):
        if entry['id'].startswith('whisper_'):
            from .audio_mlx import ensure_whisper_runtime
            ensure_whisper_runtime()
        uses_mlx_vlm = (entry.get('id') in {'ui_venus_mlx_6bit', 'fastvlm_mlx_bf16'}
                        or 'MLX-VLM' in str(entry.get('runtime') or ''))
        uses_mlx_text = (entry.get('id') == 'ternary_bonsai_2_27b'
                         or (entry.get('managed') and 'MLX' in str(entry.get('runtime') or '')))
        if uses_mlx_vlm:
            from .mlx_vlm_runtime import ensure as ensure_mlx_vlm
            ensure_mlx_vlm()
        elif uses_mlx_text:
            from .mlx_profile_runtime import ensure as ensure_mlx_profile
            ensure_mlx_profile()
        if entry['id'].startswith('plamo_3_translate_'):
            from .mlx_profile_runtime import ensure as ensure_mlx_profile
            from .plamo_translation import ensure_runtime
            ensure_mlx_profile()
            ensure_runtime()

    @staticmethod
    def _copy_bonsai_runtime(entry, target):
        if target is None or (target / 'runtime').is_dir():
            return
        runtime = ROOT / 'models/bonsai/Ternary-Bonsai-2-27B-mlx-2bit/runtime'
        if not runtime.is_dir(): raise RuntimeError('BONSAI_RUNTIME_UNAVAILABLE')
        shutil.copytree(runtime, target / 'runtime')
