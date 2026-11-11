"""Compatible text providers. Secrets remain in memory or native Keychain."""
from __future__ import annotations
from .permissions import capability_enabled
from .generation_control import check_cancelled, interrupt_response, GenerationCancelled, open_provider_response
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, urlencode, parse_qs, quote
from urllib import request, error
from .ollama import ModelFailure, provider_timeout_seconds as model_timeout_seconds, model_concurrency_failure
from .structured_output import OutputMode, prompted_json_payload, resolve_output_mode, schema_instruction

HANDLES = {p: 'OLCR_BRAIN_' + p.upper() for p in ('openai','google','anthropic','nvidia_nim','huggingface','ollama_cloud','openai_oauth')}
MAX_PROVIDER_ACCOUNTS = 3


def quick_profile(provider: str, model: str, base_url: str) -> str | None:
    """Documented fast modes on first-party endpoints only (2026-10-28)."""
    base = base_url.rstrip('/')
    if provider in {'openai', 'openai_oauth'} and base == DEFAULT_BASE_URLS['openai']:
        if re.fullmatch(r'(?:gpt-6\.1-(?:sol|terra)|gpt-6-(?:luna|sol|astra)|gpt-5\.6(?:-sol)?)(?:-\d{4}-\d{2}-\d{2})?', model):
            return 'openai_fast'
    if provider == 'anthropic' and base == DEFAULT_BASE_URLS['anthropic']:
        if re.fullmatch(r'claude-opus-(?:5-5|5|4-8)(?:-\d{8})?', model):
            return 'anthropic_fast' if model.startswith('claude-opus-5-5') else 'anthropic_fast_beta'
    return None


def account_key(provider: str, account_id: int = 1) -> str:
    if provider not in HANDLES or type(account_id) is not int or not 1 <= account_id <= MAX_PROVIDER_ACCOUNTS:
        raise ValueError('Choose an account slot from 1 to 3 for a supported Brain provider')
    return provider if account_id == 1 else f'{provider}:account:{account_id}'

def credential_handle(provider: str, account_id: int = 1) -> str:
    account_key(provider, account_id)
    return HANDLES[provider] + (f'_{account_id}' if account_id != 1 else '')

LABELS = {'openai':'OpenAI compatible API','google':'Google compatible API',
          'anthropic':'Anthropic compatible API','nvidia_nim':'NVIDIA NIM',
          'huggingface':'Hugging Face',
          'ollama_cloud':'Ollama Cloud',
          'openai_oauth':'OpenAI Codex · ChatGPT plan'}
DEFAULT_BASE_URLS = {
    'openai': 'https://api.openai.com/v1',
    'google': 'https://generativelanguage.googleapis.com/v1beta',
    'anthropic': 'https://api.anthropic.com/v1',
    'nvidia_nim': 'https://integrate.api.nvidia.com/v1',
    'huggingface': 'https://router.huggingface.co/v1',
    'ollama_cloud': 'https://ollama.com/v1',
}
CHATGPT_AUTHORIZATION_URL = 'https://auth.openai.com/api/accounts/authorize'
CHATGPT_TOKEN_URL = 'https://auth.openai.com/api/accounts/oauth/token'
CHATGPT_ISSUER = 'https://auth.openai.com'
CHATGPT_RESOURCE = 'https://api.openai.com/v1'
CHATGPT_SCOPE = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
CHATGPT_JWKS_URL = 'https://auth.openai.com/.well-known/jwks.json'
# The SIWC /v1/models catalog can omit models that are still accepted by the
# account's Responses endpoint. These two were verified with completed
# Responses for the active ChatGPT-plan account; keep them selectable when
# absent from the catalog, while letting inference remain the entitlement check.
CHATGPT_VERIFIED_CATALOG_FALLBACKS = {
    'gpt-6-luna': 'GPT-6 Luna',
    'gpt-6.1-sol': 'GPT-6.1 Sol',
}

def vision_capability(model: dict) -> bool | None:
    """Use provider declarations; do not infer image support from model names."""
    for key in ('supports_vision', 'supports_image_input'):
        if isinstance(model.get(key), bool):
            return model[key]
    capabilities = model.get('capabilities')
    if isinstance(capabilities, dict):
        for key in ('vision', 'image_input'):
            if isinstance(capabilities.get(key), bool):
                return capabilities[key]
    modalities = model.get('input_modalities')
    if not isinstance(modalities, list):
        architecture = model.get('architecture')
        modalities = architecture.get('input_modalities') if isinstance(architecture, dict) else None
    if isinstance(modalities, list):
        return 'image' in modalities
    return None

def reasoning_profile(provider: str, model: str, base_url: str | None = None) -> dict:
    """Provider/model-advertised reasoning stages and request parameter mapping.

    Unknown models deliberately have no stages; callers omit reasoning controls
    instead of guessing a parameter the provider may reject.
    """
    normalized = model.casefold()
    if provider=='google' and re.fullmatch(r'(?:models/)?gemini-2\.5-flash(?:-lite)?(?:-preview-\d{2}-\d{2})?',normalized):
        return {'effort_levels':['off','auto','on'],'effort_default':'auto','effort_transport':'google_thinking_toggle'}
    if provider=='anthropic' and re.fullmatch(r'claude-(?:3-7-sonnet|sonnet-4|opus-4|opus-4-1|haiku-4-5)(?:-\d{8})?',normalized):
        return {'effort_levels':['off','auto','on'],'effort_default':'auto','effort_transport':'anthropic_thinking_toggle'}
    if provider in {'openai', 'openai_oauth'}:
        if provider == 'openai' and (urlsplit(base_url or '').hostname or '').lower() != 'api.openai.com':
            return {'effort_levels': [], 'effort_default': None, 'effort_transport': None}
        transport = 'responses' if provider == 'openai_oauth' else 'openai_chat'
        if normalized.startswith(('gpt-6', 'gpt-5.2', 'gpt-5.3', 'gpt-5.4')):
            levels = ['minimal', 'low', 'medium', 'high', 'xhigh']
            return {'effort_levels': levels, 'effort_default': 'medium', 'effort_transport': transport}
        if normalized.startswith(('gpt-5.1', 'gpt-5.1-codex')):
            levels = ['none', 'low', 'medium', 'high']
            return {'effort_levels': levels, 'effort_default': 'medium', 'effort_transport': transport}
        if normalized.startswith(('o1', 'o3', 'o4')) or normalized.startswith('gpt-5'):
            levels = ['low', 'medium', 'high']
            if 'codex-max' in normalized:
                levels.append('xhigh')
            return {'effort_levels': levels, 'effort_default': 'medium', 'effort_transport': transport}
    elif provider == 'google':
        # Gemini 3 exposes thinkingLevel; supported 2.5 Flash models use the
        # binary thinkingBudget profile above instead of named depth stages.
        if normalized.startswith(('gemini-3.8-flash', 'gemini-3.7-flash')):
            return {'effort_levels': ['low', 'medium', 'high'], 'effort_default': 'medium',
                    'effort_transport': 'google_thinking_level'}
        if normalized.startswith(('gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-3-flash')):
            return {'effort_levels': ['minimal', 'low', 'medium', 'high'], 'effort_default': 'medium',
                    'effort_transport': 'google_thinking_level'}
        if normalized.startswith(('gemini-3.1-pro', 'gemini-3-pro')):
            return {'effort_levels': ['low', 'medium', 'high'], 'effort_default': 'high',
                    'effort_transport': 'google_thinking_level'}
        if normalized.startswith(('gemini-3.1-flash-lite-image', 'gemini-3.1-flash-lite')):
            return {'effort_levels': ['minimal', 'low', 'medium', 'high'], 'effort_default': 'minimal',
                    'effort_transport': 'google_thinking_level'}
    elif provider == 'anthropic':
        if any(family in normalized for family in ('opus-5', 'sonnet-5', 'mythos-5', 'fable-5')):
            return {'effort_levels': ['low', 'medium', 'high', 'xhigh', 'max'],
                    'effort_default': 'high', 'effort_transport': 'anthropic_output_effort'}
        if any(family in normalized for family in ('opus-4-6', 'sonnet-4-6', 'opus-4-7', 'sonnet-4-7', 'opus-4-8')):
            return {'effort_levels': ['low', 'medium', 'high'], 'effort_default': 'high',
                    'effort_transport': 'anthropic_adaptive_effort'}
    elif provider == 'nvidia_nim':
        if 'mistral-small-4' in normalized:
            return {'effort_levels': ['none', 'high'], 'effort_default': 'high',
                    'effort_transport': 'openai_compatible'}
        if 'glm-5.3' in normalized:
            return {'effort_levels': ['low', 'high', 'max'], 'effort_default': 'max',
                    'effort_transport': 'openai_compatible'}
        if 'nemotron-3-super' in normalized:
            return {'effort_levels': ['none', 'low', 'high'], 'effort_default': 'high',
                    'effort_transport': 'openai_compatible'}
    return {'effort_levels': [], 'effort_default': None, 'effort_transport': None}

def _openai_message_text(content):
    """Normalize text returned by OpenAI-compatible chat endpoints."""
    if isinstance(content,str):return content
    if isinstance(content,list):
        return ''.join(part.get('text','') for part in content
                       if isinstance(part,dict) and part.get('type') in {'text','output_text'}
                       and isinstance(part.get('text'),str))
    return content

def _anthropic_messages(messages):
    """Translate internal chat roles to the Messages API's user/assistant roles."""
    translated=[]
    for message in messages:
        source_role=message.get('role','user')
        if source_role in {'system','developer'}:continue
        role=source_role if source_role in {'user','assistant'} else 'user'
        content=message.get('content','')
        if source_role not in {'user','assistant'}:
            content=f"[{message.get('name') or source_role}]\n{content}"
        translated.append({'role':role,'content':content})
    return translated

def _optional_parameter_group(parameter: str | None) -> str | None:
    aliases={
        'reasoning.effort':'reasoning','reasoning_effort':'reasoning',
        'generationConfig.thinkingConfig.thinkingLevel':'google_thinking_level',
        'generationConfig.thinkingConfig.thinkingBudget':'google_thinking_budget',
        'generationConfig.thinkingConfig.includeThoughts':'google_include_thoughts',
        'generationConfig.thinkingConfig':'google_thinking_config',
        'output_config.effort':'anthropic_effort','thinking':'anthropic_thinking',
        'speed':'anthropic_speed','service_tier':'service_tier',
        'max_tokens':'output_limit','max_completion_tokens':'output_limit',
        'max_output_tokens':'output_limit','generationConfig.maxOutputTokens':'output_limit',
    }
    return aliases.get(parameter)

def _remove_optional_parameter(payload: dict, headers: dict, group: str) -> bool:
    """Remove only a documented optional control rejected by the endpoint."""
    changed=False
    if group=='reasoning':
        changed=payload.pop('reasoning',None) is not None or changed
        changed=payload.pop('reasoning_effort',None) is not None or changed
    elif group=='output_limit':
        for key in ('max_completion_tokens','max_output_tokens'):
            changed=payload.pop(key,None) is not None or changed
        # Messages requires max_tokens, so removing it cannot recover a call.
        if 'anthropic-version' not in headers:
            changed=payload.pop('max_tokens',None) is not None or changed
        config=payload.get('generationConfig')
        if isinstance(config,dict):
            changed=config.pop('maxOutputTokens',None) is not None or changed
            if not config:payload.pop('generationConfig',None)
    elif group.startswith('google_'):
        config=payload.get('generationConfig')
        thinking=config.get('thinkingConfig') if isinstance(config,dict) else None
        if isinstance(thinking,dict):
            key={'google_thinking_level':'thinkingLevel','google_thinking_budget':'thinkingBudget',
                 'google_include_thoughts':'includeThoughts','google_thinking_config':None}[group]
            if key is None:
                changed=config.pop('thinkingConfig',None) is not None or changed
            else:
                changed=thinking.pop(key,None) is not None or changed
                if not thinking:
                    config.pop('thinkingConfig',None)
            if not config:
                payload.pop('generationConfig',None)
    elif group=='anthropic_effort':
        output=payload.get('output_config')
        if isinstance(output,dict):
            changed=output.pop('effort',None) is not None
            if not output:payload.pop('output_config',None)
    elif group=='anthropic_thinking':
        changed=payload.pop('thinking',None) is not None
    elif group=='anthropic_speed':
        payload.pop('speed',None)
        headers.pop('anthropic-beta',None)
        changed=True
    elif group=='service_tier':
        payload.pop('service_tier',None)
        changed=True
    return changed

def _schema_format_profile_key(provider: str, account_id: int, base: str,
                               model: str, style: str, output_mode: OutputMode) -> tuple:
    """Scope learned structured-output fallbacks to one exact API profile."""
    return (provider, account_id, base, model, style, output_mode.value)

def reported_tokens(provider: str, response: dict) -> tuple:
    """Only provider-reported values; reasoning/cache subtotals aren't estimates."""
    valid=lambda value:isinstance(value,int) and not isinstance(value,bool) and value>=0
    usage=response.get('usageMetadata' if provider=='google' else 'usage')
    usage=usage if isinstance(usage,dict) else {}
    if provider=='google':
        prompt,output,total=usage.get('promptTokenCount'),usage.get('candidatesTokenCount'),usage.get('totalTokenCount')
        if valid(prompt): prompt+=sum(usage[key] for key in ('toolUsePromptTokenCount',) if valid(usage.get(key)))
        if valid(output): output+=sum(usage[key] for key in ('thoughtsTokenCount',) if valid(usage.get(key)))
    else:
        prompt=usage.get('prompt_tokens',usage.get('input_tokens'))
        output=usage.get('completion_tokens',usage.get('output_tokens'))
        total=usage.get('total_tokens')
        if provider=='anthropic' and valid(prompt):
            prompt+=sum(usage[key] for key in ('cache_creation_input_tokens','cache_read_input_tokens') if valid(usage.get(key)))
    prompt=prompt if valid(prompt) else None
    output=output if valid(output) else None
    total=total if valid(total) else prompt+output if prompt is not None and output is not None else None
    return prompt,output,total

def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1','localhost','::1'})):
        raise ValueError('Use an HTTPS endpoint, or a local loopback endpoint')
    return value.rstrip('/')

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        # Never forward a credential to a redirected origin.
        return None

def call_json(url: str, payload: dict, headers: dict, *, form=False) -> dict:
    check_cancelled()
    data = urlencode(payload).encode() if form else json.dumps(payload).encode()
    req = request.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json', **headers})
    try:
        with open_provider_response(req, timeout=model_timeout_seconds(120), handlers=(NoRedirect(),)) as response, interrupt_response(response):
            result = json.load(response)
            if not isinstance(result, dict): raise ValueError()
            # Keep only standard rate-limit headers; never persist arbitrary
            # response headers that could contain identifiers or secrets.
            result['_olcr_rate_limits'] = rate_limit_snapshot(response.headers)
        return result
    except error.HTTPError as exc:
        from .provider_errors import provider_http_failure
        raise provider_http_failure(exc) from None
    except (error.URLError, OSError, ValueError):
        raise ModelFailure('provider_unavailable', 'Brain provider returned no usable response') from None

def rate_limit_snapshot(headers) -> dict:
    """Extract server-reported rate windows, not account-wide plan balances."""
    allowed = {'x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
               'x-ratelimit-reset-requests', 'x-ratelimit-limit-tokens',
               'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-tokens',
               'retry-after', 'x-goog-quota-limit', 'x-goog-quota-remaining'}
    return {str(key).lower(): str(value)[:80] for key, value in headers.items()
            if str(key).lower() in allowed and str(value).strip()}

def get_json(url: str, headers: dict | None = None, *, provider_label: str = 'ChatGPT') -> dict:
    req = request.Request(url, headers=headers or {})
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict): raise ValueError()
        return result
    except error.HTTPError as exc:
        raise ModelFailure('provider_rejected', f'{provider_label} rejected model discovery (HTTP {exc.code})') from None
    except (error.URLError, OSError, ValueError):
        raise ModelFailure('provider_unavailable', f'{provider_label} returned no usable model list') from None

def decode_chatgpt_id_token(token: str, client_id: str, nonce: str | None) -> dict:
    """Verify the SIWC ID token using OpenAI's published signing keys."""
    try:
        import jwt
        header = jwt.get_unverified_header(token)
        jwks = get_json(CHATGPT_JWKS_URL)
        jwk = next(key for key in jwks.get('keys', []) if key.get('kid') == header.get('kid'))
        signing_key = jwt.PyJWK.from_dict(jwk)
        if signing_key.algorithm_name not in {'RS256','ES256'}:
            raise ValueError('unsupported signing algorithm')
        claims = jwt.decode(token, signing_key.key, algorithms=[signing_key.algorithm_name],
                            audience=client_id, issuer=CHATGPT_ISSUER, leeway=5,
                            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
        if nonce is not None and not secrets.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError('nonce mismatch')
        return claims
    except Exception:
        raise ValueError('ChatGPT sign-in could not be verified. Please try again.') from None

class BrainProviders:
    def __init__(self, db, settings=None, usage_recorder=None):
        self.usage_recorder = usage_recorder
        self.db = db
        from .model_availability import AvailabilityStore
        self.availability_store=AvailabilityStore(db)
        self.settings = settings
        self.credentials = {}
        self.pending_credentials = {}
        self.oauth_attempts = {}
        self.model_catalog_errors = {}
        self.model_catalog_error_categories = {}
        # A provider/model/account/endpoint can reject structured output while
        # still accepting the same task as prompted JSON. Remember both the
        # exact schema and the successful mode fallback so new internal
        # contracts do not each pay for the same predictable HTTP rejection.
        self.schema_compat_fallbacks = set()
        self.schema_format_compat_fallbacks = set()
        self.schema_preflight_notices = set()
        self.parameter_compat_fallbacks = set()
        self.lock = threading.RLock()
        self.model_catalog_at = {}
        self.restore_credentials({credential_handle(provider, slot): os.environ.get(credential_handle(provider, slot), '')
                                  for provider in HANDLES for slot in range(1, MAX_PROVIDER_ACCOUNTS+1)})

    def restore_credentials(self, values: dict[str, str]) -> int:
        restored = 0
        with self.lock:
            for provider in HANDLES:
                for slot in range(1, MAX_PROVIDER_ACCOUNTS+1):
                    key = account_key(provider, slot)
                    raw = values.get(credential_handle(provider, slot))
                    if not isinstance(raw, str) or not raw.strip() or len(raw) > 16_384:
                        continue
                    credential = raw.strip()
                    if provider == 'openai_oauth':
                        try: credential = json.loads(credential)
                        except ValueError: continue
                        if not isinstance(credential, dict) or not all(isinstance(credential.get(k), str) and credential[k] for k in ('access_token','client_id','id_token')):
                            continue
                        if 'chatgpt.tokens.use.direct' not in str(credential.get('scope','')).split():
                            continue
                        current = self.credentials.get(key)
                        if key in self.pending_credentials:
                            continue
                        if isinstance(current, dict):
                            old_expiry, new_expiry = current.get('expires_at', 0), credential.get('expires_at', 0)
                            if isinstance(old_expiry, (int,float)) and isinstance(new_expiry, (int,float)) and old_expiry > new_expiry:
                                continue
                    if self.credentials.get(key) == credential:
                        continue
                    if key in self.credentials:self.availability_store.clear(provider,slot)
                    self.credentials[key] = credential
                    self.model_catalog_at.pop(key, None)
                    self.model_catalog_errors.pop(key, None)
                    self.model_catalog_error_categories.pop(key, None)
                    restored += 1
        return restored

    def account_order(self, provider: str) -> list[int]:
        account_key(provider)
        with self.db.connect() as con:
            row = con.execute('SELECT metadata_json FROM brain_providers WHERE id=?', (provider+':account_order',)).fetchone()
        order = json.loads(row[0]).get('account_order') if row else None
        return order if isinstance(order, list) and sorted(order) == [1,2,3] else [1,2,3]

    def save_account_order(self, provider: str, order: list[int]) -> dict:
        account_key(provider)
        if not isinstance(order, list) or any(type(slot) is not int for slot in order) or sorted(order) != [1,2,3]:
            raise ValueError('Account order must contain slots 1, 2 and 3 exactly once')
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',
                        (provider+':account_order', json.dumps({'account_order':order})))
        return {'account_order':order}

    def _credential_host_id(self, account_id: int = 1) -> str | None:
        credential = self.credentials.get(account_key('openai_oauth', account_id))
        host = credential.get('host_id') if isinstance(credential, dict) else None
        if isinstance(host, str) and host.startswith('urn:uuid:'):
            try:
                uuid.UUID(host[len('urn:uuid:'):])
                return host
            except ValueError:
                pass
        return None

    def metadata(self, provider: str, account_id: int = 1) -> dict | None:
        key = account_key(provider, account_id)
        with self.db.connect() as con:
            row = con.execute('SELECT metadata_json FROM brain_providers WHERE id=?',(key,)).fetchone()
        if row:
            metadata={**json.loads(row[0]), 'account_id':account_id}
            if provider == 'openai_oauth':
                # Migrate the former generic OAuth settings to the official SIWC endpoints.
                current=(metadata.get('base_url') in {CHATGPT_RESOURCE, 'https://chatgpt.com/backend-api/codex'}
                         and metadata.get('authorization_url')==CHATGPT_AUTHORIZATION_URL
                         and metadata.get('token_url')==CHATGPT_TOKEN_URL)
                return {'id':provider,'account_id':account_id,'label':LABELS[provider],'auth_type':'oauth','base_url':CHATGPT_RESOURCE,
                        'authorization_url':CHATGPT_AUTHORIZATION_URL,'token_url':CHATGPT_TOKEN_URL,
                        'scope':CHATGPT_SCOPE,'model_ids':metadata.get('model_ids',[]) if current else [],
                        'model_labels':metadata.get('model_labels',{}) if current else {},
                        'host_id':metadata.get('host_id') or self._credential_host_id(account_id)}
            return metadata
        if provider == 'openai_oauth':
            return {'id': provider, 'account_id':account_id, 'label': LABELS[provider], 'auth_type': 'oauth',
                    'base_url': CHATGPT_RESOURCE, 'authorization_url': CHATGPT_AUTHORIZATION_URL,
                    'token_url': CHATGPT_TOKEN_URL, 'scope': CHATGPT_SCOPE, 'model_ids': [],
                    'host_id':self._credential_host_id(account_id)}
        if provider in DEFAULT_BASE_URLS and self.configured(provider, account_id):
            # Older OLCR versions stored API keys in Keychain before persisting
            # provider metadata/model IDs. Reconstruct first-party defaults so
            # existing credentials remain useful after the model settings change.
            return {'id': provider, 'account_id':account_id, 'label': LABELS[provider], 'auth_type': 'api_key',
                    'base_url': DEFAULT_BASE_URLS[provider], 'model_ids': [], 'model_labels': {}}
        return None

    def configured(self, provider: str, account_id: int = 1) -> bool:
        credential = self.credentials.get(account_key(provider, account_id))
        if provider == 'openai_oauth':
            return (isinstance(credential,dict)
                    and all(isinstance(credential.get(k), str) and credential[k] for k in ('access_token','client_id','id_token'))
                    and 'chatgpt.tokens.use.direct' in str(credential.get('scope','')).split())
        return bool(credential)

    def register(self, provider: str, auth_type: str, values: dict, account_id: int = 1) -> dict:
        with self.lock:
            return self._register(provider, auth_type, values, account_id)

    def _register(self, provider: str, auth_type: str, values: dict, account_id: int) -> dict:
        account_key(provider, account_id)
        if provider not in HANDLES or auth_type != ('oauth' if provider == 'openai_oauth' else 'api_key'):
            raise ValueError('Unsupported Brain provider')
        if provider == 'openai_oauth':
            raise ValueError('Use Continue with ChatGPT to connect this provider')
        ids = [m.strip() for m in values.get('model_ids','').split(',') if m.strip()]
        if ((not ids and provider not in {'nvidia_nim','huggingface','ollama_cloud'}) or len(ids)>64
                or any(len(m)>160 or re.search(r'\s|[?#]',m)
                       or re.search(r'embed|rerank|whisper|tts|audio|ui[-_]?venus|fastvlm',m,re.I) for m in ids)):
            raise ValueError('Provide general language model IDs (embedding, audio and perception models are excluded)')
        metadata = {'id':provider,'account_id':account_id,'label':LABELS[provider],'auth_type':auth_type,
                    'base_url':endpoint(values.get('base_url','')),'model_ids':list(dict.fromkeys(ids)),
                    'account_name':values.get('account_name','').strip()[:80]}
        if auth_type == 'oauth':
            if not values.get('client_id','').strip():raise ValueError('OAuth client ID is required')
            metadata.update(client_id=values['client_id'].strip(),authorization_url=endpoint(values.get('authorization_url','')),
                            token_url=endpoint(values.get('token_url','')),scope=values.get('scope',''))
        else:
            key = values.get('api_key','').strip()
            if key:
                if len(key)>4096:raise ValueError('Credential is too long')
                self.credentials[account_key(provider, account_id)]=key
            if not self.configured(provider, account_id):raise ValueError('API key is required')
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',(account_key(provider, account_id),json.dumps(metadata)))
        self.model_catalog_at.pop(account_key(provider, account_id), None)
        self.availability_store.clear(provider,account_id)
        return {**metadata,'configured':self.configured(provider, account_id)}

    def catalog(self) -> tuple[list, list]:
        models, providers = {}, []
        for provider in HANDLES:
            accounts = []
            order = self.account_order(provider)
            for rank, slot in enumerate(order, 1):
                account_models, rows = self._account_catalog(provider, slot)
                row = rows[0] if rows else {'id':provider, 'account_id':slot, 'label':LABELS[provider],
                    'auth_type':'oauth' if provider == 'openai_oauth' else 'api_key', 'configured':False}
                row['rank'] = rank
                accounts.append(row)
                for model in account_models:
                    current = models.get(model['id'])
                    if current is None or (model['available'] and not current['available']) or (model['available']==current['available'] and {'available':2,'unknown':1,'unavailable':0}.get(model.get('availability',{}).get('state'),1)>{'available':2,'unknown':1,'unavailable':0}.get(current.get('availability',{}).get('state'),1)):
                        models[model['id']] = model
            primary = next((row for row in accounts if row['configured']), accounts[0])
            providers.append({**primary, 'id':provider, 'accounts':accounts, 'account_order':order,
                              'credential_pending':any(row.get('credential_pending') for row in accounts)})
        return list(models.values()), providers

    def _account_catalog(self, provider: str, account_id: int) -> tuple[list, list]:
        models,providers=[],[]
        metadata=self.metadata(provider, account_id)
        key=account_key(provider, account_id)
        if provider == 'openai_oauth' and metadata:
            metadata = self._chatgpt_catalog(metadata)
        elif provider in DEFAULT_BASE_URLS and metadata and self.configured(provider, account_id):
            metadata = self._api_key_catalog(provider, metadata)
        if not metadata:return [], []
        connected=self.configured(provider, account_id)
        category = self.model_catalog_error_categories.get(key)
        reauth = category in {'credential_expired', 'credential_required'}
        row={**metadata,'configured':connected,'credential_pending':key in self.pending_credentials,
             'connection_state':'reauth_required' if reauth else 'connected' if connected else 'disconnected'}
        if reauth:
            row['availability_message']='ChatGPTの接続期限が切れました。再接続してください。'
        if connected and not reauth and not metadata.get('model_ids'):
            if self.settings is not None and not capability_enabled(self.settings, "external_access_enabled"):
                row['availability_message']='外部アクセスをONにすると、接続済みプロバイダーのモデルを読み込めます。'
            elif key in self.model_catalog_errors:
                row['availability_message']=self.model_catalog_errors[key]
            else:
                row['availability_message']='モデル一覧をまだ取得できていません。接続設定を確認してください。'
        if provider=='openai_oauth' and connected:
            row['account_hint']=self.credentials[key].get('email','')
        providers.append(row)
        for model in metadata['model_ids']:
            label=metadata.get('model_labels',{}).get(model,model)
            usage_url = {'google': 'https://aistudio.google.com/usage',
                         'nvidia_nim': 'https://build.nvidia.com/',
                         'ollama_cloud': 'https://ollama.com/settings/usage',
                         'openai_oauth': 'https://chatgpt.com/',
                         'openai': 'https://platform.openai.com/usage',
                         'anthropic': 'https://console.anthropic.com/'}.get(provider, 'https://huggingface.co/settings/billing')
            state=self.account_availability(provider,model,metadata)
            models.append({'id':f'api:{provider}:{model}','label':label,'provider':LABELS[provider],
                           'usage_url':usage_url,
                           'vision_supported': metadata.get('model_vision', {}).get(model),
                           'available':connected and not reauth,'availability':state,'kind':'language','local':False,
                           **reasoning_profile(provider, model, metadata['base_url']),
                           'quick_supported':bool(quick_profile(provider, model, metadata['base_url']))})
        return models,providers

    def _api_key_catalog(self, provider: str, metadata: dict) -> dict:
        """Discover usable language models for restored API keys without setup re-entry."""
        account_id = metadata.get('account_id', 1)
        key = account_key(provider, account_id)
        is_first_party = metadata.get('base_url', '').rstrip('/') == DEFAULT_BASE_URLS.get(provider)
        if metadata.get('model_ids') and not is_first_party:
            return metadata
        if self.settings is not None and not capability_enabled(self.settings, "external_access_enabled"):
            return metadata
        if time.monotonic() - self.model_catalog_at.get(key, 0.0) < 300:
            return metadata
        token = self.credentials.get(key)
        if not token:
            return metadata
        base = metadata['base_url'].rstrip('/')
        try:
            if provider == 'google':
                response = get_json(base + '/models', {'x-goog-api-key': token}, provider_label='Gemini')
                items = response.get('models', [])
                visible = [item for item in items if isinstance(item, dict)
                           and 'generateContent' in item.get('supportedGenerationMethods', [])
                           and isinstance(item.get('name'), str)
                           and not re.search(r'embed|audio|tts|aqa|image|transcri|veo', item['name'], re.I)]
                model_ids = [item['name'].removeprefix('models/') for item in visible]
                labels = {item['name'].removeprefix('models/'): item.get('displayName') or item['name']
                          for item in visible}
            elif provider in {'openai', 'nvidia_nim', 'huggingface', 'ollama_cloud'}:
                response = get_json(base + '/models', {'Authorization': 'Bearer ' + token},
                                    provider_label=LABELS.get(provider, 'OpenAI'))
                items = response.get('data', [])
                visible = [item for item in items if isinstance(item, dict) and isinstance(item.get('id'), str)
                           and not re.search(r'embed|audio|tts|whisper|rerank|moderation', item['id'], re.I)]
                model_ids = [item['id'] for item in visible]
                labels = {item['id']: item.get('name') or item['id'] for item in visible}
            else:
                response = get_json(base + '/models', {
                    'x-api-key': token, 'anthropic-version': '2023-06-01'}, provider_label='Anthropic')
                items = response.get('data', [])
                visible = [item for item in items if isinstance(item, dict) and isinstance(item.get('id'), str)]
                model_ids = [item['id'] for item in visible]
                labels = {item['id']: item.get('display_name') or item['id'] for item in visible}
            model_ids = list(dict.fromkeys(model_ids))[:256]
            metadata['model_ids'] = model_ids
            metadata['model_labels'] = {model: str(labels.get(model, model))[:200] for model in model_ids}
            metadata['model_vision'] = {str(item.get('id') or item.get('name', '').removeprefix('models/')): vision_capability(item) for item in visible}
            output_modes = {}
            for item in visible:
                model_id = str(item.get('id') or item.get('name', '').removeprefix('models/'))
                if model_id not in model_ids:
                    continue
                declared = item.get('supports_structured_output')
                if not isinstance(declared, bool) and provider == 'huggingface':
                    mapping = item.get('inferenceProviderMapping')
                    if isinstance(mapping, dict):
                        mapped = [value for value in mapping.values() if isinstance(value, dict)]
                        flags = [value.get('supports_structured_output') for value in mapped
                                 if isinstance(value.get('supports_structured_output'), bool)]
                        # The router may pick any mapped provider, so native
                        # schema is safe only if every advertised route says yes.
                        if mapped and len(flags) == len(mapped):
                            declared = all(flags)
                if isinstance(declared, bool):
                    output_modes[model_id] = 'json_schema' if declared else 'prompt_json'
            # Replace the discovery snapshot so stale capability assertions
            # cannot outlive a provider's refreshed model catalog.
            metadata['model_output_modes'] = output_modes
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',
                            (key, json.dumps(metadata)))
            self.model_catalog_at[key] = time.monotonic()
            self.model_catalog_errors.pop(key, None)
        except (ModelFailure, KeyError, TypeError, AttributeError):
            # A temporary catalog failure must not erase a restored credential or
            # any existing conversation model selection.
            self.model_catalog_at[key] = time.monotonic() - 270
            self.model_catalog_errors[key] = (
                'モデル一覧の取得に失敗しました。APIキー、APIの有効化、ネットワーク設定を確認してください。')
        return metadata

    def token(self, provider: str, metadata: dict) -> str:
        account_id = metadata.get('account_id', 1)
        key = account_key(provider, account_id)
        with self.lock:
            if not self.configured(provider, account_id):raise ModelFailure('credential_required','Configure the selected Brain provider first')
            if metadata.get('base_url'):
                current=self.metadata(provider, account_id)
                if not current or current.get('base_url') != metadata['base_url']:
                    raise ModelFailure('configuration', 'Provider account changed during this request; retry with its current settings')
            credential=self.credentials[key]
            if provider!='openai_oauth':return credential
            should_refresh=credential.get('expires_at',float('inf')) < time.time()+30
            if should_refresh:
                if not credential.get('refresh_token'):raise ModelFailure('credential_expired','ChatGPTの接続期限が切れました。設定から再接続してください。')
                try:
                    result=call_json(metadata['token_url'],{'grant_type':'refresh_token','refresh_token':credential['refresh_token'],
                                    'client_id':credential['client_id'],'resource':CHATGPT_RESOURCE},{},form=True)
                except ModelFailure as exc:
                    if exc.category=='provider_rejected':
                        raise ModelFailure('credential_expired','ChatGPTの接続期限が切れました。設定から再接続してください。') from None
                    raise
                if not result.get('access_token'):raise ModelFailure('credential_expired','ChatGPTの接続期限が切れました。設定から再接続してください。')
                credential={**credential,**result,'expires_at':time.time()+int(result.get('expires_in',3600))}
                self.credentials[key]=credential
                self.pending_credentials[key]=credential
            return credential['access_token']

    def model_accounts(self, provider: str, model: str) -> list[dict]:
        """A request snapshots priority; credentials and endpoints stay bound to stable slots."""
        accounts = []
        for slot in self.account_order(provider):
            if self.configured(provider, slot):
                metadata = self.metadata(provider, slot)
                if metadata and model in metadata.get('model_ids', []):
                    accounts.append(metadata)
        return accounts

    def account_availability(self,provider,model,metadata,external_enabled=None):
        from .model_availability import availability
        account=metadata.get('account_id',1);base=metadata.get('base_url','')
        enabled=capability_enabled(self.settings,'external_access_enabled') if external_enabled is None and self.settings is not None else external_enabled
        if enabled is False and urlsplit(base).hostname not in {'localhost','127.0.0.1','::1'}:return availability('unavailable','external_access_disabled')
        if not self.configured(provider,account):return availability('unavailable','credential_required')
        if model not in metadata.get('model_ids',[]):return availability('unavailable','model_not_configured')
        if provider=='openai_oauth':
            credential=self.credentials.get(account_key(provider,account),{})
            if credential.get('expires_at',float('inf'))<time.time()+30 and not credential.get('refresh_token'):return availability('unavailable','reauth_required')
        return self.availability_store.get(provider,account,base,model)

    def model_availability(self,provider,model,external_enabled=None):
        from .model_availability import availability
        states=[]
        for slot in self.account_order(provider):
            metadata=self.metadata(provider,slot)
            if metadata and model in metadata.get('model_ids',[]):states.append(self.account_availability(provider,model,metadata,external_enabled))
        if not states:return availability('unavailable','model_not_configured')
        usable=[state for state in states if state['state']!='unavailable']
        if usable:return next((state for state in usable if state['state']=='available'),usable[0])
        temporary=[state for state in states if state.get('retry_at') is not None]
        return min(temporary,key=lambda state:state['retry_at']) if temporary else states[0]

    def model_metadata(self, provider: str, model: str) -> dict | None:
        return next(iter(self.model_accounts(provider, model)), None)

    def generate(self, provider: str, model: str, messages: list, schema, external_enabled: bool,
                 effort: str | None = None, quick: bool = False,
                 role: str = 'normal_chat', max_tokens: int | None = None) -> dict:
        return self.with_model_accounts(provider, model, lambda metadata: self._generate_account(
            provider, model, messages, schema, external_enabled, effort=effort,
            account_id=metadata.get('account_id', 1), role=role,
            **({'max_tokens':max_tokens} if type(max_tokens) is int and max_tokens>0 else {}),
            **({'quick':True} if quick else {})),external_enabled=external_enabled)

    def with_model_accounts(self, provider: str, model: str, operation, *, external_enabled=None) -> dict:
        attempts, first_account, last_failure = [], None, None
        metadata_rows=[self.metadata(provider,slot) for slot in self.account_order(provider)]
        for metadata in metadata_rows:
            if not metadata or model not in metadata.get('model_ids',[]):continue
            slot=metadata.get('account_id', 1)
            state=self.account_availability(provider,model,metadata,external_enabled)
            if state['state']=='unavailable':
                from .model_progress import model_progress
                model_progress(state='account_skipped',model=model,account=slot,reason=state['reason'],retry_at=state.get('retry_at'))
                if state['reason']=='external_access_disabled':raise ModelFailure('authorization','External access must be enabled for the selected cloud Brain')
                attempts.append({'account_id':slot,'status':'skipped','category':state['reason']})
                last_failure=ModelFailure('preflight_unavailable','Provider account is unavailable before invocation')
                continue
            if first_account is None: first_account = slot
            from .model_progress import model_progress
            model_progress(state='account_attempt',model=model,provider=provider,account=slot)
            try:
                result = operation(metadata)
                if not isinstance(result, dict) or not isinstance(result.get('text'), str) or not result['text'].strip():
                    raise ModelFailure('invalid_response', 'Brain provider returned no text')
                self.availability_store.success(provider,slot,metadata['base_url'],model,result.get('provider_rate_limits',{}))
                result['provider_account_id'] = slot
                result['provider_account_attempts'] = [*attempts, {'account_id':slot, 'status':'success'}]
                if attempts:
                    result['account_fallback_from'] = first_account
                    # The existing host notice also covers a recovered account
                    # without changing the canonical model identity.
                    result['fallback_from'] = f'api:{provider}:{model}'
                    result['fallback_model_id'] = f'api:{provider}:{model}'
                return result
            except ModelFailure as exc:
                if exc.category in {'authorization','external_access_disabled'}: raise
                self.availability_store.failure(provider,slot,metadata['base_url'],model,exc)
                from .provider_errors import failure_diagnostics
                model_progress(state='account_failed',model=model,provider=provider,account=slot,failure=exc.category,**failure_diagnostics(exc))
                last_failure = exc
                attempts.append({'account_id':slot, 'status':'failed', 'category':exc.category})
                # Output truncation is determined by the model/output budget,
                # not by the API account. Retrying identical content through
                # every credential repeats the same incomplete generation.
                if exc.category=='output_limit':
                    raise
            except (KeyError, TypeError, AttributeError, IndexError):
                last_failure=ModelFailure('invalid_response', 'Brain provider returned an invalid response')
                attempts.append({'account_id':slot, 'status':'failed', 'category':'invalid_response'})
            except (OSError, TimeoutError, ValueError):
                last_failure=ModelFailure('provider_unavailable', 'Brain provider returned no usable response')
                self.availability_store.failure(provider,slot,metadata['base_url'],model,last_failure)
                attempts.append({'account_id':slot, 'status':'failed', 'category':'provider_unavailable'})
        if not attempts:
            raise ModelFailure('configuration', 'No connected provider account has this model configured')
        if len(attempts) == 1 and last_failure is not None:
            raise last_failure
        exhausted=ModelFailure('provider_accounts_exhausted', 'All configured accounts for the selected model are unavailable')
        categories={item.get('category') for item in attempts
                    if item.get('status') in {'failed','skipped'} and item.get('category')}
        if len(categories)==1:
            exhausted.fallback_reason=next(iter(categories))
        raise exhausted

    def _generate_account(self, provider: str, model: str, messages: list, schema, external_enabled: bool,
                          effort: str | None = None, account_id: int = 1, quick: bool = False,
                          role: str = 'normal_chat', max_tokens: int | None = None) -> dict:
        metadata=self.metadata(provider, account_id)
        if not metadata or model not in metadata['model_ids']:raise ModelFailure('configuration','Brain provider model is not configured')
        profile = reasoning_profile(provider, model, metadata['base_url'])
        fast = quick_profile(provider, model, metadata['base_url'])
        if effort is not None and effort not in profile['effort_levels']:
            if profile['effort_levels']:
                raise ModelFailure('configuration', 'The selected effort is not supported by this model')
            # Compatible endpoints often implement only the common chat schema.
            # Do not fail the whole request because a model-name heuristic lacks
            # enough evidence to safely send a reasoning-control extension.
            effort = None
        toggle=profile['effort_transport'] in {'google_thinking_toggle','anthropic_thinking_toggle'}
        toggle_decision=None
        if toggle:
            from .thinking import decide_thinking
            text=next((str(m.get('content','')) for m in reversed(messages) if m.get('role')=='user'),'')
            toggle_decision=decide_thinking(effort or 'auto',text)
        if not external_enabled and urlsplit(metadata['base_url']).hostname not in {'localhost','127.0.0.1','::1'}:
            raise ModelFailure('authorization','External access must be enabled for the selected cloud Brain')
        token=self.token(provider,metadata);base=metadata['base_url'];started=time.monotonic()
        output_budget=max_tokens if type(max_tokens) is int and max_tokens>0 else None
        effective_thinking=None
        if (provider=='anthropic' and toggle_decision and toggle_decision['enabled']
                and output_budget is not None and output_budget<=1024):
            # Manual Claude thinking requires at least 1024 tokens and a total
            # output cap strictly above that. Keep the caller's cap authoritative.
            toggle_decision={**toggle_decision,'enabled':False,
                'reason':'output_budget_too_small_for_thinking'}
            effective_thinking=toggle_decision
        output_mode=resolve_output_mode(provider, model, base,
            (metadata.get('model_output_modes') or {}).get(model), schema if isinstance(schema,dict) else None)
        from .structured_output import openai_schema_supported
        if provider in {'openai','openai_oauth'} and isinstance(schema,dict) and output_mode is OutputMode.JSON_SCHEMA and not openai_schema_supported(schema):
            output_mode=OutputMode.PROMPT_JSON
            style='responses' if provider=='openai_oauth' else 'openai'
            notice_key=_schema_format_profile_key(provider,account_id,base,model,style,OutputMode.JSON_SCHEMA)
            with self.lock:
                announce=(notice_key not in self.schema_preflight_notices
                    and notice_key not in self.schema_format_compat_fallbacks)
                self.schema_preflight_notices.add(notice_key)
            if announce:
                from .model_progress import model_progress
                model_progress(state='schema_compatibility',model=model,
                    reason='schema_unsupported_by_native_mode')
        system='\n\n'.join(str(m.get('content','')) for m in messages if m.get('role')=='system')
        provider_system='\n\n'.join(str(m.get('content','')) for m in messages
                                     if m.get('role') in {'system','developer'})
        from .thinking import received_reasoning
        reasoning = None

        def tracked_call(operation, *, stream=False, request_body=None, endpoint_path=None):
            event_id=uuid.uuid4().hex;created_at=time.time();model_id=f'api:{provider}:{model}'
            from .model_traffic_logs import begin_model_traffic, uses_internet
            traffic_call=begin_model_traffic(
                role=role,model=model,runtime='brain:'+provider,
                request_body={'path':endpoint_path,'provider':provider,
                              'provider_account_id':account_id,'payload':request_body},
                internet=uses_internet(metadata.get('base_url')))
            if self.usage_recorder:self.usage_recorder(event_id,model_id,'pending',created_at=created_at)
            try:
                result=operation()
                if not stream and isinstance(result,dict):
                    if provider=='google':
                        limited=(result.get('candidates') or [{}])[0].get('finishReason')=='MAX_TOKENS'
                    elif provider=='anthropic':
                        limited=result.get('stop_reason') in {'max_tokens','model_context_window_exceeded'}
                    else:
                        limited=(result.get('choices') or [{}])[0].get('finish_reason')=='length'
                    if limited:
                        failure=ModelFailure('output_limit','Brain provider exhausted its output budget before completing the response')
                        failure.model_traffic_partial=result
                        raise failure
            except BaseException as exc:
                partial=getattr(exc,'model_traffic_partial',None)
                traffic_call.finish(status='cancelled' if isinstance(exc,(GeneratorExit,GenerationCancelled)) else 'error',
                                    response_body=partial,
                                    error_code=getattr(exc,'category',type(exc).__name__))
                if self.usage_recorder:
                    counts=reported_tokens(provider,partial if isinstance(partial,dict) else {})
                    self.usage_recorder(event_id,model_id,'cancelled' if isinstance(exc, GenerationCancelled) else 'error',
                                        *counts,created_at=created_at)
                raise
            response_body=({'text':result[0],'metadata':result[1]} if stream and isinstance(result,tuple) and len(result)==2 else result)
            traffic_call.finish(status='success',response_body=response_body)
            response={'usage':result[1]} if stream else result
            counts=reported_tokens(provider,response)
            rate=(result[1] if stream else result).get('_olcr_rate_limits',{})
            if self.usage_recorder:self.usage_recorder(event_id,model_id,'success',*counts,provider_rate_limits=rate,created_at=created_at)
            return result

        def apply_quick(payload, headers, disabled_parameters=frozenset()):
            if fast == 'openai_fast' and 'service_tier' not in disabled_parameters:
                payload['service_tier']='fast' if quick else 'default'
            elif quick and fast and fast.startswith('anthropic_fast') and 'anthropic_speed' not in disabled_parameters:
                payload['speed']='fast'
                if fast == 'anthropic_fast_beta': headers['anthropic-beta']='fast-mode-2026-02-01'

        def model_json(url, payload, headers, disabled_parameters=frozenset()):
            wire_payload=json.loads(json.dumps(payload))
            wire_headers=dict(headers)
            apply_quick(wire_payload, wire_headers, disabled_parameters)
            return tracked_call(lambda:call_json(url,wire_payload,wire_headers),
                                request_body=wire_payload,endpoint_path=urlsplit(url).path)

        def call_with_compatibility(url: str, payload: dict, headers: dict, *, style: str) -> dict:
            """Negotiate only explicitly rejected optional protocol fields.

            Schema errors fall back to prompted JSON; an unsupported optional
            control is removed by its exact provider-reported parameter path.
            Both successful downgrades are cached for this model/account.
            """
            schema_key = None
            if isinstance(schema, dict):
                schema_digest = hashlib.sha256(json.dumps(
                    schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                schema_key = (provider, account_id, base, model, style, schema_digest)
            schema_can_fallback=schema_key is not None and output_mode in {OutputMode.JSON_SCHEMA,OutputMode.JSON_OBJECT}
            cache_prefix=(provider,account_id,base,model)
            schema_profile_key=_schema_format_profile_key(provider,account_id,base,model,style,output_mode)
            with self.lock:
                disabled={key[4] for key in self.parameter_compat_fallbacks if key[:4]==cache_prefix}
                prompted=output_mode is OutputMode.PROMPT_JSON or (
                    schema_can_fallback and (schema_key in self.schema_compat_fallbacks
                        or schema_profile_key in self.schema_format_compat_fallbacks))
            current_payload=json.loads(json.dumps(payload))
            current_headers=dict(headers)
            for group in disabled:
                _remove_optional_parameter(current_payload,current_headers,group)
            if prompted and schema_can_fallback and output_mode is not OutputMode.PROMPT_JSON:
                current_payload=prompted_json_payload(current_payload,schema,style)
            pending_parameters=set()
            pending_schema_fallback=False
            last_failure=None
            for _ in range(6):
                try:
                    result=model_json(url,current_payload,current_headers,disabled)
                except ModelFailure as failure:
                    last_failure=failure
                    status=getattr(failure,'http_status',None)
                    rejected=(failure.category=='provider_rejected' and status in {400,422})
                    if (rejected and schema_can_fallback and not prompted
                            and getattr(failure,'schema_format_rejection',False)):
                        from .model_progress import model_progress
                        from .provider_errors import failure_diagnostics
                        model_progress(state='schema_compatibility',model=model,
                            reason='provider_rejected_schema',**failure_diagnostics(failure))
                        current_payload=prompted_json_payload(current_payload,schema,style)
                        prompted=True
                        pending_schema_fallback=True
                        continue
                    group=_optional_parameter_group(getattr(failure,'parameter',None)) if rejected else None
                    if group and group not in disabled:
                        next_headers=dict(current_headers)
                        if _remove_optional_parameter(current_payload,next_headers,group):
                            current_headers=next_headers
                            disabled.add(group)
                            pending_parameters.add(group)
                            continue
                    raise
                else:
                    with self.lock:
                        if pending_schema_fallback and schema_key is not None:
                            self.schema_compat_fallbacks.add(schema_key)
                            self.schema_format_compat_fallbacks.add(schema_profile_key)
                        self.parameter_compat_fallbacks.update((*cache_prefix,group) for group in pending_parameters)
                    return result
            if last_failure is not None:
                raise last_failure
            raise ModelFailure('provider_rejected','Provider compatibility negotiation did not produce a response')

        if provider == 'openai_oauth':
            payload={'model':model,'input':[{'role':m.get('role','user'),'content':str(m.get('content',''))}
                                           for m in messages if m.get('role') in {'user','assistant','developer'}],
                     'store':False,'stream':True}
            if output_budget is not None:payload['max_output_tokens']=output_budget
            if effort is not None: payload['reasoning']={'effort':effort}
            if system: payload['instructions']=system
            if isinstance(schema,dict):
                if output_mode is OutputMode.JSON_SCHEMA:
                    payload['text']={'format':{'type':'json_schema','name':'olcr_response','schema':schema,'strict':True}}
                else:
                    payload['input'].append({'role':'user','content':schema_instruction(schema)})
                    if output_mode is OutputMode.JSON_OBJECT:
                        payload['text']={'format':{'type':'json_object'}}
            headers={'Authorization':'Bearer '+token}
            apply_quick(payload, headers)
            schema_key=(provider,account_id,base,model,'responses',json.dumps(schema,sort_keys=True)) if isinstance(schema,dict) else None
            schema_profile_key=_schema_format_profile_key(provider,account_id,base,model,'responses',output_mode)
            with self.lock:
                prompted=bool(schema_key and (schema_key in self.schema_compat_fallbacks
                    or schema_profile_key in self.schema_format_compat_fallbacks))
            def prompted_payload():
                candidate=json.loads(json.dumps(payload));candidate.pop('text',None)
                candidate['input'].append({'role':'user','content':schema_instruction(schema)})
                return candidate
            if prompted:payload=prompted_payload()
            cache_prefix=(provider,account_id,base,model)
            with self.lock:
                disabled={key[4] for key in self.parameter_compat_fallbacks if key[:4]==cache_prefix}
            for group in disabled:
                _remove_optional_parameter(payload,headers,group)
            pending_parameters=set()
            pending_schema_fallback=False
            last_failure=None
            for _ in range(6):
                wire_payload=json.loads(json.dumps(payload));wire_headers=dict(headers)
                apply_quick(wire_payload,wire_headers,disabled)
                try:
                    content,usage=tracked_call(lambda:self._responses_stream(
                        base+'/responses',wire_payload,token,wire_headers),stream=True,
                        request_body=wire_payload,endpoint_path='/responses')
                except ModelFailure as failure:
                    last_failure=failure
                    rejected=(failure.category=='provider_rejected' and getattr(failure,'http_status',None) in {400,422})
                    if rejected and schema_key and not prompted and getattr(failure,'schema_format_rejection',False):
                        from .model_progress import model_progress
                        from .provider_errors import failure_diagnostics
                        model_progress(state='schema_compatibility',model=model,
                            reason='provider_rejected_schema',**failure_diagnostics(failure))
                        payload=prompted_payload();prompted=True;pending_schema_fallback=True
                        continue
                    group=_optional_parameter_group(getattr(failure,'parameter',None)) if rejected else None
                    if group and group not in disabled and _remove_optional_parameter(payload,headers,group):
                        disabled.add(group);pending_parameters.add(group)
                        continue
                    raise
                else:
                    with self.lock:
                        if pending_schema_fallback and schema_key:
                            self.schema_compat_fallbacks.add(schema_key)
                            self.schema_format_compat_fallbacks.add(schema_profile_key)
                        self.parameter_compat_fallbacks.update((*cache_prefix,group) for group in pending_parameters)
                    break
            else:
                if last_failure is not None:raise last_failure
                raise ModelFailure('provider_rejected','Provider compatibility negotiation did not produce a response')
            reasoning = usage.pop('_olcr_reasoning', None)
            rate_limits=usage.pop('_olcr_rate_limits', {})
            prompt_tokens,completion_tokens,total_tokens=reported_tokens(provider,{'usage':usage})
        elif provider in {'openai', 'nvidia_nim', 'huggingface', 'ollama_cloud'}:
            payload={'model':model,'messages':messages,'stream':False}
            if output_budget is not None:
                first_party=(provider=='openai' and (urlsplit(base).hostname or '').lower()=='api.openai.com')
                reasoning_model=bool(re.match(r'(?:gpt-[56]|o[1-9]\d*)(?:[-.]|$)',model.casefold()))
                payload['max_completion_tokens' if first_party and reasoning_model else 'max_tokens']=output_budget
            if effort is not None: payload['reasoning_effort']=effort
            if isinstance(schema,dict):
                if output_mode is OutputMode.JSON_SCHEMA:
                    payload['response_format']={'type':'json_schema','json_schema':{'name':'olcr_response','schema':schema,'strict':True}}
                else:
                    payload=prompted_json_payload(payload,schema,'openai')
                    if output_mode is OutputMode.JSON_OBJECT:
                        payload['response_format']={'type':'json_object'}
            result=call_with_compatibility(base+'/chat/completions',payload,{'Authorization':'Bearer '+token},style='openai')
            rate_limits=result.pop('_olcr_rate_limits', {})
            content=_openai_message_text((result.get('choices') or [{}])[0].get('message',{}).get('content'))
            reasoning = received_reasoning((result.get('choices') or [{}])[0].get('message',{}).get('reasoning_content'))
            prompt_tokens,completion_tokens,total_tokens=reported_tokens(provider,result)
        elif provider=='google':
            payload={'contents':[{'role':'model' if m.get('role')=='assistant' else 'user','parts':[{'text':str(m.get('content',''))}]} for m in messages if m.get('role') not in {'system','developer'}]}
            if provider_system:payload['systemInstruction']={'parts':[{'text':provider_system}]}
            if isinstance(schema,dict):
                if output_mode is OutputMode.JSON_SCHEMA:
                    payload['generationConfig']={'responseMimeType':'application/json','responseJsonSchema':schema}
                else:
                    payload=prompted_json_payload(payload,schema,'google')
                    if output_mode is OutputMode.JSON_OBJECT:
                        payload.setdefault('generationConfig',{})['responseMimeType']='application/json'
            if effort is not None:
                if not toggle:payload.setdefault('generationConfig', {}).setdefault('thinkingConfig', {})['thinkingLevel']=effort
            if toggle:
                payload.setdefault('generationConfig',{})['thinkingConfig']={'thinkingBudget':1024 if toggle_decision['enabled'] else 0,'includeThoughts':bool(toggle_decision['enabled'])}
            if output_budget is not None:payload.setdefault('generationConfig',{})['maxOutputTokens']=output_budget
            result=call_with_compatibility(base+'/models/'+quote(model.removeprefix('models/'),safe='')+':generateContent',payload,{'x-goog-api-key':token},style='google')
            rate_limits=result.pop('_olcr_rate_limits', {})
            parts=(result.get('candidates') or [{}])[0].get('content',{}).get('parts',[])
            content=''.join(part.get('text','') for part in parts if not part.get('thought'))
            reasoning = received_reasoning(''.join(part.get('text','') for part in parts if part.get('thought')), kind='summary')
            prompt_tokens,completion_tokens,total_tokens=reported_tokens(provider,result)
        else:
            payload={'model':model,'max_tokens':output_budget or 4096,'messages':_anthropic_messages(messages)}
            if provider_system:payload['system']=provider_system
            if isinstance(schema,dict):
                if output_mode is OutputMode.JSON_SCHEMA:
                    payload['output_config']={'format':{'type':'json_schema','schema':schema}}
                else:
                    payload=prompted_json_payload(payload,schema,'anthropic')
            if toggle:
                payload['thinking']=({'type':'enabled','budget_tokens':1024} if toggle_decision['enabled'] else {'type':'disabled'})
            elif effort is not None:
                payload.setdefault('output_config', {})['effort']=effort
                if profile['effort_transport'] == 'anthropic_adaptive_effort':
                    payload['thinking']={'type':'adaptive'}
            result=call_with_compatibility(base+'/messages',payload,{'x-api-key':token,'anthropic-version':'2023-06-01'},style='anthropic')
            rate_limits=result.pop('_olcr_rate_limits', {})
            content=''.join(part.get('text','') for part in result.get('content',[]) if part.get('type')=='text')
            reasoning = received_reasoning(''.join(part.get('thinking','') for part in result.get('content',[]) if part.get('type')=='thinking'), kind='summary')
            prompt_tokens,completion_tokens,total_tokens=reported_tokens(provider,result)
        if not isinstance(content,str) or not content.strip():raise ModelFailure('invalid_response','Brain provider returned no text')
        return {'text':content,'model':model,'done':True,'model_runtime':provider,'prompt_tokens':prompt_tokens,
                'reasoning': reasoning,
                'completion_tokens':completion_tokens,'latency_ms':(time.monotonic()-started)*1000,
                'provider_rate_limits':rate_limits,'total_tokens':total_tokens,
                'brain_model_id':f'api:{provider}:{model}','usage_recorded':self.usage_recorder is not None,
                **({'thinking_decision':effective_thinking} if effective_thinking is not None else {})}

    def _chatgpt_catalog(self, metadata: dict) -> dict:
        account_id=metadata.get('account_id', 1)
        key=account_key('openai_oauth', account_id)
        credential=self.credentials.get(key)
        if not self.configured('openai_oauth', account_id):
            metadata['model_ids']=[];metadata['model_labels']={}
            return metadata
        if self.settings is not None and not capability_enabled(self.settings, "external_access_enabled"):
            return metadata
        if time.monotonic()-self.model_catalog_at.get(key, 0.0) < 300:
            return metadata
        try:
            token=self.token('openai_oauth',metadata)
            # Sign in with ChatGPT's direct plan-usage flow uses the public
            # Responses API. The private ChatGPT Codex backend is not a
            # supported endpoint for third-party SIWC clients and returns 401.
            response=get_json(CHATGPT_RESOURCE+'/models',
                              {'Authorization':'Bearer '+token},provider_label='ChatGPT')
            visible=[item for item in response.get('models',[]) if isinstance(item,dict)
                     and item.get('visibility')=='list' and isinstance(item.get('slug'),str)]
            metadata['model_ids']=[str(item['slug']) for item in visible]
            metadata['model_labels']={str(item['slug']):str(item.get('display_name') or item['slug']) for item in visible}
            metadata['model_vision']={str(item['slug']):vision_capability(item) for item in visible}
            # A live SIWC account accepted both models through POST /v1/responses
            # even though GET /v1/models omitted them. Do not discard that
            # verified usable path; normal request errors still surface directly.
            for slug,label in CHATGPT_VERIFIED_CATALOG_FALLBACKS.items():
                if slug not in metadata['model_ids']:
                    metadata['model_ids'].append(slug)
                    metadata['model_labels'][slug]=label
            metadata['base_url']=CHATGPT_RESOURCE
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',(key,json.dumps(metadata)))
            self.model_catalog_at[key]=time.monotonic()
            self.model_catalog_errors.pop(key, None)
            self.model_catalog_error_categories.pop(key, None)
        except ModelFailure as exc:
            # Keep the local model catalog usable if OpenAI is temporarily unavailable.
            self.model_catalog_at[key]=time.monotonic()-270
            # ModelFailure messages from this boundary contain only a safe category
            # and (for HTTP rejection) status code. Keep that actionable distinction
            # instead of telling users to re-authenticate for every catalog failure.
            self.model_catalog_errors[key]=str(exc)
            self.model_catalog_error_categories[key]=exc.category
        except (KeyError, TypeError):
            self.model_catalog_at[key]=time.monotonic()-270
            self.model_catalog_errors[key]=(
                'ChatGPTのモデル一覧を読み取れませんでした。設定を確認してください。')
        return metadata

    def _responses_stream(self, url: str, payload: dict, token: str, extra_headers: dict | None = None) -> tuple[str, dict]:
        check_cancelled()
        req=request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',
                         'Accept':'text/event-stream','Authorization':'Bearer '+token,**(extra_headers or {})})
        pieces=[];usage={};completed=False
        # Passively collect summaries already delivered by the provider. Do not
        # enable summaries or request another completion to produce a log.
        summary_parts = {}
        summary_truncated = False
        final_reasoning = None
        try:
            with open_provider_response(req, timeout=model_timeout_seconds(120), handlers=(NoRedirect(),)) as response, interrupt_response(response):
                rate_limits=rate_limit_snapshot(response.headers)
                for raw in response:
                    model_timeout_seconds(120)
                    line=raw.decode('utf-8','replace').strip()
                    if not line.startswith('data:'): continue
                    data=line[5:].strip()
                    if not data or data=='[DONE]': continue
                    event=json.loads(data);kind=event.get('type')
                    if kind=='response.output_text.delta': pieces.append(str(event.get('delta','')))
                    elif kind in {'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done'}:
                        from .thinking import MAX_REASONING_CHARS
                        key = (str(event.get('item_id', '')), event.get('summary_index', 0))
                        previous = summary_parts.get(key, '')
                        candidate = ((previous + str(event.get('delta', '')))
                                     if kind.endswith('.delta') else str(event.get('text', previous)))
                        if kind.endswith('.delta'):
                            from .model_progress import model_progress
                            model_progress(state='reasoning',text=str(event.get('delta','')),kind='summary',complete=False)
                        remaining = MAX_REASONING_CHARS - sum(map(len, summary_parts.values())) + len(previous)
                        summary_truncated = summary_truncated or len(candidate) > remaining
                        if key in summary_parts or (remaining > 0 and len(summary_parts) < 128):
                            summary_parts[key] = candidate[:remaining]
                    elif kind=='response.completed':
                        completed=True;usage=event.get('response',{}).get('usage',{}) or {}
                        final_summaries = [part.get('text', '')
                            for item in event.get('response', {}).get('output', [])
                            if item.get('type') == 'reasoning'
                            for part in item.get('summary', [])
                            if part.get('type') == 'summary_text' and isinstance(part.get('text'), str)]
                        from .thinking import received_reasoning
                        final_reasoning = received_reasoning('\n\n'.join(final_summaries), kind='summary')
                    elif kind=='response.incomplete':
                        response_value=event.get('response',{})
                        usage=response_value.get('usage',{}) or {}
                        if (response_value.get('incomplete_details') or {}).get('reason')=='max_output_tokens':
                            raise ModelFailure('output_limit','ChatGPT exhausted its output budget before completing the response')
                        raise ModelFailure('provider_rejected','ChatGPT did not complete the response')
                    elif kind in {'response.failed','error'}:
                        raise ModelFailure('provider_rejected','ChatGPT did not complete the response')
            if not completed: raise ModelFailure('invalid_response','ChatGPT response stream ended early')
            usage['_olcr_rate_limits']=rate_limits
            from .thinking import received_reasoning
            usage['_olcr_reasoning'] = final_reasoning or received_reasoning('\n\n'.join(summary_parts.values()), kind='summary')
            if usage['_olcr_reasoning']:
                usage['_olcr_reasoning']['truncated'] |= summary_truncated
            return ''.join(pieces),usage
        except ModelFailure as exc:
            try: exc.model_traffic_partial={'text':''.join(pieces),'usage':usage}
            except (AttributeError,TypeError): pass
            raise
        except error.HTTPError as exc:
            from .provider_errors import provider_http_failure
            failure=provider_http_failure(exc,'ChatGPT')
            failure.model_traffic_partial={'text':''.join(pieces),'usage':usage}
            raise failure from None
        except (error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            failure=ModelFailure('provider_unavailable','ChatGPT returned no usable response')
            failure.model_traffic_partial={'text':''.join(pieces),'usage':usage}
            raise failure from exc
        except BaseException as exc:
            try: exc.model_traffic_partial={'text':''.join(pieces),'usage':usage}
            except (AttributeError,TypeError): pass
            raise

    def start_oauth(self, account_id: int = 1) -> dict:
        provider='openai_oauth';key=account_key(provider, account_id);metadata=self.metadata(provider, account_id)
        if not metadata:raise ValueError('ChatGPT sign-in is unavailable')
        attempt_id=secrets.token_urlsafe(32);state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
        nonce=secrets.token_urlsafe(32)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        owner=self
        attempt={'status':'pending','deadline':time.monotonic()+300,'account_slot':account_id}
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                parsed=urlsplit(self.path);params=parse_qs(parsed.query)
                if parsed.path!='/auth/callback' or not secrets.compare_digest(params.get('state',[''])[0],state):
                    self.send_error(400);return
                try:
                    code=params.get('code',[''])[0]
                    if not code or params.get('error'):raise ValueError()
                    registered=owner.credentials.get(key,{})
                    callback_client_id=params.get('client_id',[''])[0]
                    registered_client_id=registered.get('client_id')
                    if callback_client_id and registered_client_id and callback_client_id!=registered_client_id:
                        raise ValueError('ChatGPT account registration did not match. Please retry.')
                    client_id=callback_client_id or registered_client_id
                    if not isinstance(client_id,str) or not client_id:raise ValueError()
                    result=call_json(CHATGPT_TOKEN_URL,{'grant_type':'authorization_code','code':code,'redirect_uri':redirect,'client_id':client_id,
                                     'code_verifier':verifier,'resource':CHATGPT_RESOURCE},{},form=True)
                    if not isinstance(result.get('access_token'),str) or not isinstance(result.get('id_token'),str):raise ValueError()
                    claims=decode_chatgpt_id_token(result['id_token'],client_id,nonce)
                    granted=set(str(result.get('scope') or params.get('scope',[''])[0]).split())
                    if 'chatgpt.tokens.use.direct' not in granted:raise ValueError('ChatGPTプランの利用権限が許可されませんでした。')
                    if registered.get('subject') and claims['sub']!=registered['subject']:
                        raise ValueError('接続済みのChatGPTアカウントと一致しません。')
                    credential={key:result[key] for key in ('access_token','refresh_token','token_type','id_token','scope') if key in result}
                    credential['scope']=str(result.get('scope') or ' '.join(sorted(granted)))
                    auth_claim=claims.get('https://api.openai.com/auth',{})
                    account_id=auth_claim.get('chatgpt_account_id') if isinstance(auth_claim,dict) else None
                    account_id=account_id or result.get('account_id') or result.get('chatgpt_account_id')
                    credential.update(client_id=client_id,subject=claims['sub'],email=claims.get('email',''),
                                      host_id=metadata['host_id'])
                    if isinstance(account_id,str) and account_id:credential['account_id']=account_id
                    credential['expires_at']=time.time()+int(result.get('expires_in',3600))
                    with owner.lock:
                        owner.availability_store.clear(provider,attempt['account_slot'])
                        owner.credentials[key]=credential
                        owner.pending_credentials[key]=credential
                        owner.model_catalog_at[key]=0.0
                        owner.model_catalog_errors.pop(key, None)
                        owner.model_catalog_error_categories.pop(key, None)
                        attempt.update(status='complete',credential=credential)
                    body=b'OLCR sign-in complete. You can close this window.'
                except Exception as exc:
                    attempt.update(status='failed',message=str(exc) if isinstance(exc,ValueError) and str(exc) else 'ChatGPT sign-in could not be completed. Please retry from OLCR.')
                    body=b'OLCR sign-in could not be completed. Return to OLCR and retry.'
                self.send_response(200);self.send_header('Content-Type','text/plain');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
        server=HTTPServer(('127.0.0.1',0),Callback);server.timeout=1
        redirect=f'http://127.0.0.1:{server.server_port}/auth/callback'
        def receive():
            try:
                while attempt['status']=='pending' and time.monotonic()<attempt['deadline']:server.handle_request()
                if attempt['status']=='pending':attempt['status']='expired'
            finally:server.server_close()
        with self.lock:
            self.oauth_attempts={k:a for k,a in self.oauth_attempts.items() if a['deadline']>time.monotonic()}
            if len(self.oauth_attempts)>=4 or any(a.get('account_slot',1)==account_id and a['status']=='pending' for a in self.oauth_attempts.values()):
                server.server_close();raise ValueError('An OAuth sign-in is already pending for this account')
            self.oauth_attempts[attempt_id]=attempt
        if not metadata.get('host_id'):
            metadata['host_id']='urn:uuid:'+str(uuid.uuid4())
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',(key,json.dumps(metadata)))
        registered=self.credentials.get(key,{})
        client_id=registered.get('client_id') or 'dynamic_agent_client'
        parameters={'response_type':'code','client_id':client_id,'redirect_uri':redirect,'scope':CHATGPT_SCOPE,
                    'resource':CHATGPT_RESOURCE,'ext_agent_host_id':metadata['host_id'],'state':state,'nonce':nonce,
                    'code_challenge':challenge,'code_challenge_method':'S256'}
        if client_id=='dynamic_agent_client':parameters['agent_name_hint']='OLCR'
        elif registered.get('id_token'):parameters['id_token_hint']=registered['id_token']
        query=urlencode(parameters)
        threading.Thread(target=receive,daemon=True).start()
        return {'attempt_id':attempt_id,'authorization_url':CHATGPT_AUTHORIZATION_URL+'?'+query}

    def poll_oauth(self, attempt_id: str) -> dict:
        with self.lock:
            attempt=self.oauth_attempts.get(attempt_id)
            if not attempt:return {'status':'expired'}
            result={'status':attempt['status']}
            if attempt['status']=='complete':
                attempt.pop('credential',None)
                self.oauth_attempts.pop(attempt_id,None)
            elif attempt['status']=='failed':
                result['message']=attempt.get('message','ChatGPT sign-in could not be completed. Please retry from OLCR.')
                self.oauth_attempts.pop(attempt_id,None)
            return result

    def pending_credential(self) -> dict:
        with self.lock:
            for slot in self.account_order('openai_oauth'):
                key = account_key('openai_oauth', slot)
                credential = self.pending_credentials.get(key)
                if not credential: continue
                serialized=json.dumps(credential,sort_keys=True)
                version_input = serialized if slot == 1 else key + ':' + serialized
                return {'credential':serialized, 'account_id':slot, 'handle':credential_handle('openai_oauth',slot),
                        'version':hashlib.sha256(version_input.encode()).hexdigest()}
            return {'credential':None}

    def acknowledge_credential(self, version: str):
        with self.lock:
            # Resolve by version across slots: an independently completed sign-in
            # must not make an earlier successful Keychain save unacknowledgeable.
            for slot in self.account_order('openai_oauth'):
                key=account_key('openai_oauth',slot)
                credential=self.pending_credentials.get(key)
                if not credential: continue
                serialized=json.dumps(credential,sort_keys=True)
                value=serialized if slot==1 else key+':'+serialized
                if hashlib.sha256(value.encode()).hexdigest()==version:
                    self.pending_credentials.pop(key,None)
                    return True
            return False
