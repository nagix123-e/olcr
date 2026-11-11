"""Safe diagnostics from provider errors: never retain response messages or bodies."""
import json
import re
from .ollama import ModelFailure
from .model_availability import attach_http_failure

CODES={'invalid_json_schema','invalid_schema','unsupported_parameter','invalid_value','invalid_request_error','model_not_found','insufficient_quota','rate_limit_exceeded','invalid_api_key','authentication_error','permission_denied','RESOURCE_EXHAUSTED','INVALID_ARGUMENT','UNAUTHENTICATED','PERMISSION_DENIED','NOT_FOUND','UNAVAILABLE','INTERNAL','DEADLINE_EXCEEDED','invalid_image','invalid_image_format','invalid_base64_image','image_parse_error','image_too_small','image_too_large','unsupported_image'}
PARAMS={'text.format.schema','text.format','response_format','generationConfig.responseMimeType',
        'generationConfig.responseSchema','generationConfig.responseJsonSchema',
        'generationConfig.thinkingConfig','generationConfig.thinkingConfig.thinkingLevel',
        'generationConfig.thinkingConfig.thinkingBudget','generationConfig.thinkingConfig.includeThoughts',
        'reasoning.effort','reasoning_effort','output_config.effort','output_config.format',
        'thinking','speed','service_tier','max_tokens','max_completion_tokens','max_output_tokens',
        'generationConfig.maxOutputTokens','model','input','messages'}
SCHEMA_PARAMETER_PREFIXES=('response_format.json_schema','text.format',
                           'generationConfig.responseSchema','generationConfig.responseJsonSchema',
                           'output_config.format')
SCHEMA_ERROR_CODES={'invalid_json_schema','invalid_schema'}

def _safe_parameter(value):
    if not isinstance(value,str):return None
    if value in PARAMS:return value
    if len(value)<=160 and any(value.startswith(prefix+'.') for prefix in SCHEMA_PARAMETER_PREFIXES):
        return value
    return None

def _schema_parameter(value):
    return bool(value and (value in {'response_format','generationConfig.responseMimeType'} or any(
        value==prefix or value.startswith(prefix+'.') for prefix in SCHEMA_PARAMETER_PREFIXES)))

def _explicit_provider_parameter(error,message):
    """Read an explicitly named request field; never infer one from arbitrary body text."""
    for detail in error.get('details') or []:
        if not isinstance(detail,dict):
            continue
        violations=detail.get('fieldViolations') or detail.get('field_violations') or []
        for violation in violations:
            if isinstance(violation,dict):
                value=_safe_parameter(violation.get('field') or violation.get('fieldPath'))
                if value:
                    return value
    match=re.search(r"\b(?:at|field|parameter)\s+['\"]([^'\"]+)['\"]",message,re.I)
    return _safe_parameter(match.group(1)) if match else None

def provider_http_failure(exc,label='Provider'):
    try:raw=exc.read(32768).decode('utf-8','replace')
    except (OSError,ValueError):raw=''
    try:detail=json.loads(raw).get('error',{})
    except (ValueError,AttributeError):detail={}
    if not isinstance(detail,dict):detail={}
    code=next((detail.get(k) for k in ('code','status','type') if isinstance(detail.get(k),str) and detail.get(k) in CODES),None)
    message=detail.get('message') if isinstance(detail.get('message'),str) else ''
    param=_safe_parameter(detail.get('param')) or _explicit_provider_parameter(detail,message)
    # Keep the raw provider body out of the exception. This boolean is enough
    # for a safe, narrowly scoped JSON-format compatibility retry.
    if code in SCHEMA_ERROR_CODES:
        schema_format_rejection=True
    elif param is not None:
        schema_format_rejection=_schema_parameter(param)
    else:
        schema_format_rejection=bool(re.search(
            r'json[ -]?schema|structured output|response[_ .-]?format|'
            r'response(Json)?Schema|output_config\.format',message,re.I))
    category='provider_rejected'
    if exc.code==401:category='credential_expired'
    elif exc.code==403:category='access_denied'
    elif exc.code==404:category='model_or_endpoint_unavailable'
    elif exc.code==429:category='rate_limited'
    elif exc.code>=500:category='provider_unavailable'
    if re.search(r'concurren(?:t|cy)|too many (?:active|running|in.flight) requests|'
                 r'model.{0,40}(?:busy|already running)|maximum.{0,40}parallel', raw, re.I) \
            and exc.code in {409,429,503}:
        category='model_concurrency_limit'
    suffix=''.join(' · '+value for value in (code,param) if value)
    failure=attach_http_failure(ModelFailure(category,f'{label} request failed (HTTP {exc.code}){suffix}'),exc)
    failure.provider_code=code;failure.parameter=param
    failure.schema_format_rejection=schema_format_rejection
    return failure

def failure_diagnostics(failure):
    return {key:getattr(failure,key) for key in ('http_status','provider_code','parameter','retry_at') if getattr(failure,key,None) is not None}
