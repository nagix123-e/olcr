"""Validated, persisted model choices for non-Brain capabilities.

Each consumer keeps its existing contract. Credentials use the native bundle;
SQLite stores connection metadata only. A failed probe never changes a binding.
"""
from __future__ import annotations
from .permissions import capability_enabled, full_access

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
import wave
from urllib import request, error

from .brain_providers import endpoint, NoRedirect
from .laya_runtime import LayaRuntimeError
from .model_registry import ModelRegistry, ROLE_TO_MODEL
from .ollama import ModelFailure, OllamaProvider
from .semantic import EmbeddingFailure, OllamaEmbeddingProvider
from .model_assets import ModelDownloads, asset_path

ROLES = {
    'semantic_embedding': ('Embedding', 'Embedding', ('ollama', 'openai')),
    'typed_local_decisions': ('単純選択機 · Laya / Jev', 'Decisions · Laya / Jev', ('jev',)),
    'general_image': ('Vision · light', 'Vision · light', ('ollama', 'openai', 'brain')),
    'gui_grounding': ('Vision · heavy / UI', 'Vision · heavy / UI', ('ollama', 'openai', 'brain')),
    'transcription_ja': ('文字起こし · 日本語', 'Transcription · Japanese', ('openai',)),
    'transcription_en': ('文字起こし · 英語', 'Transcription · English', ('openai',)),
    'translation': ('翻訳', 'Translation', ('ollama', 'openai')),
}
HANDLES = {role: 'OLCR_MODEL_' + role.upper() for role in ROLES}
VISION_ROLES = {'general_image', 'gui_grounding'}
MAX_TRANSLATION_CHARS = 12_000


def vision_probe_image() -> bytes:
    """A valid 256px RGB PNG shared by preset and connected-model probes.

    The former embedded 1px PNG had an invalid IDAT CRC. Strict image APIs
    rejected it before inference, incorrectly failing image-capable models.
    """
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    size = 256
    pixels = (b'\x00' + b'\xff\xff\xff' * size) * size
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels))
            + chunk(b'IEND', b''))


def completion_text(raw):
    choices = raw.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError('VISION_RESPONSE_INVALID')
    message = choices[0].get('message')
    if not isinstance(message, dict):
        raise ValueError('VISION_RESPONSE_INVALID')
    content = message.get('content')
    if isinstance(content, list):
        content = ''.join(part.get('text', '') for part in content
                          if isinstance(part, dict) and isinstance(part.get('text'), str))
    return content


def vectors(value, count):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError('EMBEDDING_COUNT_MISMATCH')
    dimensions = set()
    for row in value:
        if not isinstance(row, list) or not row:
            raise ValueError('EMBEDDING_EMPTY')
        dimensions.add(len(row))
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in row):
            raise ValueError('EMBEDDING_NON_FINITE')
        if not any(x != 0 for x in row):
            raise ValueError('EMBEDDING_ZERO_VECTOR')
    if len(dimensions) > 1:
        raise ValueError('EMBEDDING_DIMENSION_MISMATCH')
    return value


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('DECISION_PROBABILITY_INVALID')
    return float(value)


class PurposeModels:
    def __init__(self, db, settings, brain=None):
        self.db, self.settings, self.brain = db, settings, brain
        self.lock = threading.RLock()
        self.credentials = {handle + slot: os.environ.get(handle + slot, '') for handle in HANDLES.values() for slot in ('_A', '_B')}
        self.pending = {}
        self._catalog_at = 0
        self._local_catalog = []
        self.downloads = ModelDownloads(settings)
        with db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS purpose_model_choices(role TEXT PRIMARY KEY, configuration_json TEXT NOT NULL)')
            con.execute("DELETE FROM purpose_model_choices WHERE role='vision_fallback'")

    def choice(self, role):
        if role not in ROLES:
            raise ValueError('MODEL_PURPOSE_UNKNOWN')
        with self.db.connect() as con:
            row = con.execute('SELECT configuration_json FROM purpose_model_choices WHERE role=?', (role,)).fetchone()
        return json.loads(row[0]) if row else {'protocol': 'preset'}

    def audio_model(self, language: str) -> str:
        role = 'transcription_' + language
        value = self.db.load_settings().get('profile_audio_models', {})
        selected = value.get(role) if isinstance(value, dict) else None
        return selected if isinstance(selected, str) and selected else role

    def namespace(self):
        config = self.choice('semantic_embedding')
        if config['protocol'] == 'preset':
            return 'embeddinggemma:latest'
        return 'purpose:' + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:32]

    def validate(self, role, value):
        if role not in ROLES or not isinstance(value, dict):
            raise ValueError('MODEL_PURPOSE_UNKNOWN')
        protocol = value.get('protocol')
        if protocol == 'preset':
            return {'protocol': 'preset'}
        if protocol not in ROLES[role][2]:
            raise ValueError('MODEL_PROTOCOL_INCOMPATIBLE')
        model = value.get('model')
        if not isinstance(model, str) or not model.strip() or len(model) > 240 or any(ord(x) < 32 for x in model):
            raise ValueError('MODEL_ID_INVALID')
        clean = {'protocol': protocol, 'model': model.strip()}
        if protocol == 'brain':
            provider = value.get('provider')
            from .brain_providers import HANDLES as PROVIDERS
            if provider not in PROVIDERS or self.brain is None:
                raise ValueError('VISION_PROVIDER_UNSUPPORTED')
            metadata = self.brain.providers.model_metadata(provider, clean['model'])
            if not metadata or clean['model'] not in metadata.get('model_ids', []):
                raise ValueError('VISION_MODEL_NOT_REGISTERED')
            if not self.brain.providers.configured(provider, metadata.get('account_id', 1)):
                raise ValueError('MODEL_CREDENTIAL_REQUIRED')
            clean['provider'] = provider
        else:
            clean['base_url'] = endpoint(str(value.get('base_url') or ''))
            if protocol == 'ollama' and not self.local(clean):
                raise ValueError('OLLAMA_USE_LOOPBACK_ENDPOINT')
        return clean

    @staticmethod
    def local(config):
        from urllib.parse import urlsplit
        return urlsplit(config.get('base_url', '')).hostname in {'localhost', '127.0.0.1', '::1'}

    def _headers(self, role, config, key=None):
        if not self.local(config) and not capability_enabled(self.settings, "external_access_enabled"):
            raise ModelFailure('external_access_disabled', '外部アクセスを有効にしてください。')
        credential = self.credentials.get(HANDLES[role] + config.get('key_slot', '_A'), '') if key is None else key
        return {'Authorization': 'Bearer ' + credential} if credential else {}

    def http(self, role, config, path, payload=None, *, data=None, content_type='application/json', key=None, log=True):
        headers = {'Content-Type': content_type, **self._headers(role, config, key)}
        if data is None and payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
        req = request.Request(config['base_url'].rstrip('/') + path, data=data, headers=headers)
        traffic_call = None
        if log:
            from .model_traffic_logs import begin_model_traffic, uses_internet
            traffic_call = begin_model_traffic(
                role=role, model=config['model'], runtime=config['protocol'],
                request_body={'path': path, 'payload': payload,
                              'body': data if payload is None else None,
                              'content_type': content_type},
                internet=uses_internet(config.get('base_url')))
        try:
            with request.build_opener(NoRedirect()).open(req, timeout=120) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                if traffic_call:
                    traffic_call.finish(status='error', response_body=value,
                                        error_code='MODEL_RESPONSE_NOT_OBJECT')
                raise ValueError('MODEL_RESPONSE_NOT_OBJECT')
            if traffic_call:
                traffic_call.finish(status='success', response_body=value)
            return value
        except error.HTTPError as exc:
            if traffic_call: traffic_call.finish(status='error', error_code='provider_rejected')
            raise ModelFailure('provider_rejected', f'Model request failed (HTTP {exc.code})') from None
        except (error.URLError, OSError):
            if traffic_call: traffic_call.finish(status='error', error_code='provider_unavailable')
            raise ModelFailure('provider_unavailable', 'Model connection failed') from None
        except BaseException as exc:
            from .generation_control import GenerationCancelled
            if traffic_call:
                traffic_call.finish(status='cancelled' if isinstance(exc, (GeneratorExit, GenerationCancelled)) else 'error',
                                    error_code=type(exc).__name__)
            raise

    def embed(self, texts, config=None, key=None):
        config = config or self.choice('semantic_embedding')
        if config['protocol'] == 'preset':
            return OllamaEmbeddingProvider(self.settings.ollama_endpoint).embed(texts, 'embeddinggemma:latest')
        if config['protocol'] == 'ollama':
            value = self.http('semantic_embedding', config, '/api/embed', {'model': config['model'], 'input': texts}, key=key)
            result = value.get('embeddings')
        else:
            value = self.http('semantic_embedding', config, '/embeddings', {'model': config['model'], 'input': texts}, key=key)
            rows = value.get('data')
            if not isinstance(rows, list) or len(rows) != len(texts) or any(not isinstance(row, dict) or type(row.get('index')) is not int for row in rows) or sorted(row['index'] for row in rows) != list(range(len(texts))):
                raise ValueError('EMBEDDING_INDEX_MISMATCH')
            result = [row.get('embedding') for row in sorted(rows, key=lambda row: row['index'])]
        vectors(result, len(texts))
        if config.get('dimension') and result and len(result[0]) != config['dimension']:
            raise ValueError('EMBEDDING_DIMENSION_CHANGED')
        return result

    def decision(self, kind, question, candidates, language, instructions, config=None, key=None):
        config = config or self.choice('typed_local_decisions')
        criteria = {str(row['tool_id']): str(row.get('criteria') or row.get('description') or row['tool_id']) for row in candidates}
        if not criteria or len(criteria) != len(candidates):
            raise ValueError('DECISION_CANDIDATES_INVALID')
        if kind == 'choice':
            questions = {'api': {'type': 'choice', 'instructions': instructions, 'criteria': criteria}}
        else:
            questions = {str(i): {'type': kind, 'instructions': instructions + '\n' + description,
                         **({'criteria': ['Does not match', 'Partially matches', 'Directly matches']} if kind == 'score' else {})}
                         for i, description in enumerate(criteria.values())}
        result = self.http('typed_local_decisions', config, '/v1/systemone',
                           {'model': config['model'], 'state': {'request': question, 'language': language}, 'questions': questions}, key=key)
        answers = result.get('answers')
        if not isinstance(answers, dict):
            raise ValueError('DECISION_ANSWERS_MISSING')
        if kind == 'choice':
            answer = answers.get('api', {})
            if not isinstance(answer, dict): raise ValueError('DECISION_ANSWER_INVALID')
            selected, distribution = answer.get('choice'), answer.get('probabilities')
            if selected not in criteria or not isinstance(distribution, dict) or set(distribution) != set(criteria):
                raise ValueError('DECISION_UNKNOWN_CHOICE')
            distribution = {k: probability(v) for k, v in distribution.items()}
            if abs(sum(distribution.values()) - 1) > .01 or distribution[selected] + .011 < max(distribution.values()):
                raise ValueError('DECISION_DISTRIBUTION_INVALID')
            return {'tool_id': selected, 'confidence': probability(answer.get('confidence')), 'raw': result}
        scores = {}
        for i, tool_id in enumerate(criteria):
            answer = answers.get(str(i), {})
            if not isinstance(answer, dict): raise ValueError('DECISION_ANSWER_INVALID')
            score = answer.get('noul' if kind == 'noul' else 'score')
            if isinstance(score, bool): raise ValueError('DECISION_PROBABILITY_INVALID')
            scores[tool_id] = probability(score if kind == 'noul' else score / 2 if isinstance(score, (int, float)) else None)
        if kind == 'score':
            return {'scores': scores, 'confidence': max(scores.values()), 'raw': result}
        selected = max(scores, key=scores.get)
        return {'tool_id': selected, 'confidence': scores[selected], 'raw': result}

    def vision(self, role, image, mime, prompt, config=None, key=None):
        config = config or self.choice(role)
        traffic_call = None
        if config['protocol'] == 'ollama':
            from .model_traffic_logs import begin_model_traffic, uses_internet
            traffic_call = begin_model_traffic(
                role=role, model=config['model'], runtime=config['protocol'],
                request_body={'image': image, 'mime_type': mime, 'prompt': prompt},
                internet=uses_internet(config.get('base_url')))
        try:
            if config['protocol'] == 'ollama':
                value = OllamaProvider(config['base_url']).vision(image, mime, prompt, config['model'])
            else:
                data_url = f'data:{mime};base64,' + base64.b64encode(image).decode()
                payload = {'model': config['model'], 'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': data_url}}]}], 'stream': False}
                if config['protocol'] == 'brain':
                    value = self._brain_vision(role, config, image, mime, prompt, payload)
                else:
                    raw = self.http(role, config, '/chat/completions', payload, key=key)
                    value = {'text': completion_text(raw)}
            if traffic_call:
                traffic_call.finish(status='success', response_body=value)
        except BaseException as exc:
            if traffic_call:
                from .generation_control import GenerationCancelled
                traffic_call.finish(status='cancelled' if isinstance(exc, (GeneratorExit, GenerationCancelled)) else 'error',
                                    error_code=getattr(exc, 'category', type(exc).__name__))
            raise
        if not isinstance(value, dict) or not isinstance(value.get('text'), str) or not value['text'].strip():
            raise ValueError('VISION_RESPONSE_INVALID')
        return {**value, 'model': config['model'], 'model_role': role, 'model_runtime': config['protocol']}

    def _brain_vision(self, role, config, image, mime, prompt, payload):
        return self.brain.providers.with_model_accounts(config['provider'], config['model'],
            lambda metadata: self._brain_vision_account(role, config, image, mime, prompt, payload, metadata))

    def _brain_vision_account(self, role, config, image, mime, prompt, payload, metadata):
        providers = self.brain.providers
        provider = config['provider']
        if not metadata or not providers.configured(provider, metadata.get('account_id', 1)):
            raise ValueError('MODEL_CREDENTIAL_REQUIRED')
        if not self.local(metadata) and not capability_enabled(self.settings, "external_access_enabled"):
            raise ModelFailure('external_access_disabled', '外部アクセスを有効にしてください。')
        from .brain_providers import call_json
        credential = providers.token(provider, metadata)
        if config['model'] not in metadata.get('model_ids', []):
            raise ValueError('VISION_MODEL_NOT_REGISTERED')
        path = ('/responses' if provider == 'openai_oauth' else
                '/models/' + config['model'] + ':generateContent' if provider == 'google' else
                '/messages' if provider == 'anthropic' else '/chat/completions')
        from .model_traffic_logs import begin_model_traffic, uses_internet
        traffic_call = begin_model_traffic(
            role=role, model=config['model'], runtime='brain:' + provider,
            request_body={'path': path, 'provider': provider,
                          'provider_account_id': metadata.get('account_id', 1),
                          'image': image, 'mime_type': mime, 'prompt': prompt},
            internet=uses_internet(metadata.get('base_url')))
        raw = None
        try:
            if provider == 'openai_oauth':
                # Reuse SIWC token refresh and Responses transport, including image
                # input. Never stringify multimodal content through the text path.
                data_url = f'data:{mime};base64,' + base64.b64encode(image).decode()
                text, usage = providers._responses_stream(metadata['base_url'] + '/responses', {
                    'model': config['model'], 'store': False, 'stream': True,
                    'input': [{'role': 'user', 'content': [
                        {'type': 'input_text', 'text': prompt},
                        {'type': 'input_image', 'image_url': data_url, 'detail': 'auto'}]}],
                }, credential)
                value = {'text': text, 'usage': usage}
            elif provider == 'google':
                raw = call_json(metadata['base_url'] + path,
                    {'contents': [{'role': 'user', 'parts': [{'text': prompt}, {'inlineData': {'mimeType': mime, 'data': base64.b64encode(image).decode()}}]}]},
                    {'x-goog-api-key': credential})
                text = ''.join(p.get('text', '') for p in raw.get('candidates', [{}])[0].get('content', {}).get('parts', []))
                value = {'text': text}
            elif provider == 'anthropic':
                raw = call_json(metadata['base_url'] + path, {'model': config['model'], 'max_tokens': 1024,
                    'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': base64.b64encode(image).decode()}}, {'type': 'text', 'text': prompt}]}]},
                    {'x-api-key': credential, 'anthropic-version': '2023-06-01'})
                text = ''.join(p.get('text', '') for p in raw.get('content', []))
                value = {'text': text}
            else:
                raw = call_json(metadata['base_url'] + path, payload, {'Authorization': 'Bearer ' + credential})
                value = {'text': completion_text(raw)}
            traffic_call.finish(status='success', response_body=raw if raw is not None else value)
            return value
        except BaseException as exc:
            from .generation_control import GenerationCancelled
            traffic_call.finish(status='cancelled' if isinstance(exc, (GeneratorExit, GenerationCancelled)) else 'error',
                                response_body=raw if raw is not None else getattr(exc, 'model_traffic_partial', None),
                                error_code=getattr(exc, 'category', type(exc).__name__))
            raise

    def transcribe(self, audio, language, config=None, key=None):
        role = 'transcription_' + language
        config = config or self.choice(role)
        if config.get('protocol') == 'preset':
            from .audio_transcription import transcribe_audio
            return transcribe_audio(audio, language, self.audio_model(language))
        raw = base64.b64decode(audio, validate=True)
        from .audio_transcription import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS
        if not raw or len(raw) > MAX_AUDIO_BYTES:
            raise ValueError('AUDIO_CLIP_TOO_LARGE')
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() not in {1, 2} or not 8000 <= wav.getframerate() <= 96000 or not 0 < wav.getnframes() / wav.getframerate() <= MAX_AUDIO_SECONDS:
                raise ValueError('AUDIO_FORMAT_UNSUPPORTED')
        boundary = 'olcr-' + uuid.uuid4().hex
        parts = []
        for name, value in [('model', config['model']), ('language', language), ('response_format', 'json')]:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(), raw, f'\r\n--{boundary}--\r\n'.encode()]
        result = self.http(role, config, '/audio/transcriptions', data=b''.join(parts), content_type='multipart/form-data; boundary=' + boundary, key=key)
        if not isinstance(result.get('text'), str):
            raise ValueError('TRANSCRIPTION_RESPONSE_INVALID')
        return {'text': result['text'], 'model': config['model']}

    def translate(self, text, source, target, config=None, key=None):
        role = 'translation'
        config = config or self.choice(role)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSLATION_CHARS:
            raise ValueError('MODEL_TRANSLATION_INPUT_INVALID')
        messages = [
            {'role': 'system', 'content':
             'Translate the user text from the requested source language to the requested target language. '
             'Return only the translation. Preserve meaning, tone, names, and formatting; do not explain.'},
            {'role': 'user', 'content':
             f'Source language: {source}\nTarget language: {target}\n\n{text}'},
        ]
        if config['protocol'] == 'preset':
            selected_model = ModelRegistry(self.settings.model_manifest_path).entry_for_role(role)['id']
            preset = self.downloads.presets()[selected_model]
            if not self.downloads.installed(preset):
                raise ValueError('MODEL_PRESET_NOT_DOWNLOADED')
            from .plamo_translation import translate as translate_plamo
            model = preset['id']
            runtime = 'plamo-translate-mlx'
        elif config['protocol'] == 'ollama':
            model = config['model']
            runtime = 'ollama'
        else:
            raw = self.http(role, config, '/chat/completions',
                            {'model': config['model'], 'messages': messages, 'stream': False}, key=key)
            value = {'text': completion_text(raw)}
            model = config['model']
            runtime = 'openai'
        if config['protocol'] in {'preset', 'ollama'}:
            from .model_traffic_logs import begin_model_traffic, uses_internet
            traffic_call = begin_model_traffic(role=role, model=model, runtime=runtime,
                                               request_body={'messages': messages, 'source': source, 'target': target},
                                               internet=(uses_internet(config.get('base_url'))
                                                         if config['protocol'] == 'ollama' else False))
            try:
                value = ({'text': translate_plamo(text, source, target, asset_path(preset['relative']))}
                         if config['protocol'] == 'preset' else
                         OllamaProvider(config['base_url']).generate(messages, config['model'], think=False))
            except BaseException as exc:
                from .generation_control import GenerationCancelled
                traffic_call.finish(status='cancelled' if isinstance(exc, (GeneratorExit, GenerationCancelled)) else 'error',
                                    error_code=getattr(exc, 'category', type(exc).__name__))
                raise
            traffic_call.finish(status='success', response_body=value)
        translated = value.get('text') if isinstance(value, dict) else None
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError('MODEL_TRANSLATION_RESPONSE_INVALID')
        return {'text': translated.strip(), 'model': model, 'model_role': role, 'model_runtime': runtime}

    def probe(self, role, config, key=None):
        if config['protocol'] == 'preset':
            if role.startswith('transcription_'):
                preset_id = self.audio_model(role[-2:])
            else:
                preset_id = ModelRegistry(self.settings.model_manifest_path).entry_for_role(role)['id']
            if not self.downloads.installed(self.downloads.presets()[preset_id]):
                raise ValueError('MODEL_PRESET_NOT_DOWNLOADED')
            if getattr(self, 'preset_probe', None):
                self.preset_probe(role)
            return {}
        if role == 'semantic_embedding':
            result = self.embed(['OLCR compatibility test', '互換性の確認'], config, key)
            return {'dimension': len(result[0])}
        if role == 'typed_local_decisions':
            for kind in ('choice', 'score', 'noul'):
                self.decision(kind, 'Compatibility test: select yes.', [{'tool_id': 'yes', 'criteria': 'Correct answer'}, {'tool_id': 'no', 'criteria': 'Incorrect answer'}], 'multilingual', 'Choose yes.', config, key)
        elif role in VISION_ROLES:
            image = vision_probe_image()
            prompt = 'Describe this image briefly.'
            if role == 'gui_grounding':
                prompt = 'Return JSON: {"elements":[],"text":[],"confidence":1,"uncertainty":[]}. Report no UI targets for this blank image.'
            result = self.vision(role, image, 'image/png', prompt, config, key)
            if role == 'gui_grounding':
                from .runtime import validate_visual_context, _extract_visual_json
                validate_visual_context(_extract_visual_json(result['text']))
        elif role == 'translation':
            self.translate('こんにちは。ありがとうございます。', 'ja', 'en', config, key)
        else:
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0' * 3200)
            self.transcribe(base64.b64encode(buffer.getvalue()).decode(), role[-2:], config, key)
        return {}

    def prepare(self, role, value, key=None):
        clean = self.validate(role, value)
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.monotonic()}
            if any(value['role'] == role for value in self.pending.values()):
                # Do not let concurrent forms overwrite the same inactive
                # Keychain slot before one of their activations commits.
                raise ValueError('MODEL_SWITCH_IN_PROGRESS')
            old = self.choice(role)
            old_key = self.credentials.get(HANDLES[role] + old.get('key_slot', '_A'), '')
            if key is None and old_key and clean.get('base_url') != old.get('base_url') and clean['protocol'] not in {'preset', 'brain'}:
                raise ValueError('MODEL_NEW_ENDPOINT_REQUIRES_NEW_KEY')
            clean['key_slot'] = ('_B' if old.get('key_slot', '_A') == '_A' else '_A') if key else old.get('key_slot', '_A')
            if clean['protocol'] == 'preset': clean.pop('key_slot', None)
            details = self.probe(role, clean, key)
            clean.update(details)
            token = uuid.uuid4().hex
            self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.monotonic()}
            self.pending[token] = {'role': role, 'config': clean, 'key': key, 'expires': time.monotonic() + 300, 'old': old}
            return {'token': token, 'configuration': clean, 'credential_handle': HANDLES[role] + clean.get('key_slot', '_A')}

    def activate(self, role, token):
        with self.lock:
            pending = self.pending.pop(token, None)
            if not pending or pending['role'] != role or pending['expires'] < time.monotonic() or self.choice(role) != pending['old']:
                raise ValueError('MODEL_PROBE_EXPIRED_RETRY')
            clean = pending['config']
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO purpose_model_choices VALUES(?,?)', (role, json.dumps(clean)))
            if pending['key'] is not None:
                self.credentials[HANDLES[role] + clean.get('key_slot', '_A')] = pending['key']
            return clean

    def cancel(self, role, token):
        with self.lock:
            pending = self.pending.get(token)
            if pending and pending['role'] == role:
                self.pending.pop(token)

    def save(self, role, value, key=None):
        prepared = self.prepare(role, value, key)
        return self.activate(role, prepared['token'])

    def catalog(self):
        registry = ModelRegistry(self.settings.model_manifest_path)
        result = []
        for role, (ja, en, protocols) in ROLES.items():
            if role.startswith('transcription_'):
                from .audio_transcription import _MODELS
                model_id = self.audio_model(role[-2:])
                if model_id.startswith('whisper_'):
                    from .model_profiles import _data
                    descriptor = _data()['models'][model_id]
                    preset = {'id': model_id, 'label': descriptor['label'], 'deployment': descriptor['repo'],
                              'runtime': descriptor['runtime'], 'size_bytes': descriptor['size_bytes']}
                else:
                    preset = {'id': role, 'label': 'LFM2.5 Audio' + (' · JP' if role.endswith('ja') else ''), 'deployment': _MODELS[role[-2:]], 'runtime': 'Liquid Audio', 'size_bytes': 3_700_000_000}
            else:
                entry = registry.entry_for_role(role)
                preset = {'id': entry['id'], 'label': entry['id'], 'deployment': entry['deployment_repo'], 'runtime': entry['runtime'], 'size_bytes': entry.get('artifact_size_bytes_upstream')}
            result.append({'role': role, 'purpose': ja, 'purpose_en': en, 'protocols': protocols,
                           'selected': self.choice(role), 'preset': preset})
        local_models = self._local_catalog
        if time.monotonic() - self._catalog_at < 30:
            return {'roles': result, 'local_models': local_models, 'vision_models': self.vision_catalog(local_models), **self.downloads.catalog()}
        local_models = []
        try:
            config = {'base_url': self.settings.ollama_endpoint}
            with request.urlopen(config['base_url'].rstrip('/') + '/api/tags', timeout=3) as response:
                tags = json.load(response).get('models', [])
            for row in tags:
                model = row.get('name')
                if not isinstance(model, str): continue
                info = self.http('general_image', config, '/api/show', {'model': model}, key='', log=False)
                capabilities = info.get('capabilities', [])
                local_models.append({'model': model, 'base_url': config['base_url'], 'capabilities': capabilities})
        except Exception: pass
        self._local_catalog = local_models
        self._catalog_at = time.monotonic()
        return {'roles': result, 'local_models': local_models, 'vision_models': self.vision_catalog(local_models), **self.downloads.catalog()}

    def vision_catalog(self, local_models):
        """Native choices reuse installed models and existing provider credentials.

        APIs often omit modality metadata. Unknown capability remains selectable
        and must pass the image probe; an explicit negative is never advertised.
        """
        choices = [{'id': 'ollama:' + row['model'], 'label': row['model'], 'provider': 'Ollama',
                    'local': True, 'vision_supported': True,
                    'configuration': {'protocol': 'ollama', 'model': row['model'], 'base_url': row['base_url']}}
                   for row in local_models if 'vision' in row.get('capabilities', [])]
        if self.brain:
            for model in self.brain.catalog()['models']:
                if not model.get('available') or not model['id'].startswith('api:') or model.get('vision_supported') is False:
                    continue
                _, provider, name = model['id'].split(':', 2)
                choices.append({'id': model['id'], 'label': model['label'], 'provider': model['provider'],
                                'local': self.local(self.brain.providers.model_metadata(provider, name) or {}), 'vision_supported': model.get('vision_supported'),
                                'configuration': {'protocol': 'brain', 'provider': provider, 'model': name}})
        return choices

    def bindings(self):
        """Effective runtime selections, not the immutable recommended manifest."""
        return {role: self.choice(role) for role in ROLES}


class PurposeEmbeddingProvider:
    """Freeze a binding per retrieval store so indexes never cross model spaces."""
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.choice('semantic_embedding')
        self.key = manager.credentials.get(HANDLES['semantic_embedding'] + self.config.get('key_slot', '_A'), '')
    def embed(self, texts, model):
        try:
            return self.manager.embed(texts, self.config, self.key)
        except (ValueError, TypeError, KeyError, IndexError, ModelFailure) as exc:
            raise EmbeddingFailure(getattr(exc, 'category', 'invalid_response'), str(exc)) from exc


class PurposeDecisionProvider:
    def __init__(self, manager, local):
        self.manager, self.local = manager, local
        self.last_trace = {}
    @property
    def loaded(self):
        return self.manager.choice('typed_local_decisions')['protocol'] != 'preset' or self.local.loaded
    def __getattr__(self, name):
        return getattr(self.local, name)
    def _call(self, kind, **kwargs):
        config = self.manager.choice('typed_local_decisions')
        if config['protocol'] == 'preset':
            return getattr(self.local, kind)(**kwargs)
        try:
            result = self.manager.decision(kind, kwargs['question'], kwargs['candidates'], kwargs.get('language', 'multilingual'), kwargs.get('instructions', 'Return a typed decision only.'), config)
            self.last_trace = {'decision_provider': config['protocol'], 'decision_model': config['model'], 'laya_decision_outcome': result.get('tool_id'), 'laya_confidence': result.get('confidence')}
            return result
        except (ValueError, TypeError, KeyError, IndexError, ModelFailure) as exc:
            raise LayaRuntimeError(getattr(exc, 'category', 'DECISION_INVALID_RESPONSE'), str(exc)) from exc
    def choice(self, **kwargs): return self._call('choice', **kwargs)
    def score(self, **kwargs): return self._call('score', **kwargs)
    def noul(self, **kwargs): return self._call('noul', **kwargs)
