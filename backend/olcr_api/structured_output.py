"""Provider/model-aware response-format selection for Brain API calls.

Capabilities are deliberately conservative: an unknown OpenAI-compatible
endpoint is not assumed to implement OpenAI's optional structured-output
extensions. Catalog-provided model capabilities can override the built-in
first-party rules.
"""
from __future__ import annotations

from enum import StrEnum
import json
import re
from urllib.parse import urlsplit


class OutputMode(StrEnum):
    JSON_SCHEMA = 'json_schema'
    JSON_OBJECT = 'json_object'
    PROMPT_JSON = 'prompt_json'


def _official_endpoint(provider: str, base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or '').lower()
    expected = {
        'openai': {'api.openai.com'},
        'google': {'generativelanguage.googleapis.com'},
        'anthropic': {'api.anthropic.com'},
        'openai_oauth': {'api.openai.com'},
    }
    return host in expected.get(provider, set())


def _schema_keywords_supported(schema: object, allowed: set[str]) -> bool:
    if not isinstance(schema, dict):
        return True
    if any(key not in allowed for key in schema):
        return False
    for key in ('properties', '$defs'):
        children = schema.get(key)
        if isinstance(children, dict) and any(
                not _schema_keywords_supported(child, allowed) for child in children.values()):
            return False
    for key in ('items', 'additionalProperties', 'not', 'if', 'then', 'else'):
        child = schema.get(key)
        if isinstance(child, dict) and not _schema_keywords_supported(child, allowed):
            return False
    for key in ('prefixItems', 'allOf', 'anyOf', 'oneOf'):
        children = schema.get(key)
        if isinstance(children, list) and any(
                not _schema_keywords_supported(child, allowed) for child in children):
            return False
    return True


def _google_schema_subset(schema: dict) -> bool:
    # Gemini's generateContent responseSchema accepts a documented subset of
    # JSON Schema. Unknown keywords use prompted JSON and local parsing instead
    # of deliberately making a request that the server will reject.
    supported = {
        '$id', '$defs', '$ref', '$anchor', 'type', 'format', 'title',
        'description', 'enum', 'items', 'prefixItems', 'minItems',
        'maxItems', 'minimum', 'maximum', 'anyOf', 'oneOf', 'properties',
        'additionalProperties', 'required', 'propertyOrdering',
    }
    return _schema_keywords_supported(schema, supported)


def _anthropic_schema_subset(schema: dict) -> bool:
    # These schema constructs are explicitly unsupported by Anthropic's
    # structured-output grammar and would otherwise produce a predictable 400.
    unsupported = {'minimum', 'maximum', 'multipleOf', 'minLength', 'maxLength'}

    def inspect(value: object) -> bool:
        if isinstance(value, dict):
            if unsupported.intersection(value):
                return False
            if 'minItems' in value:
                min_items = value['minItems']
                if type(min_items) is not int or min_items not in (0, 1):
                    return False
            if 'additionalProperties' in value and value['additionalProperties'] is not False:
                return False
            if isinstance(value.get('$ref'), str) and '://' in value['$ref']:
                return False
            return all(inspect(child) for child in value.values())
        if isinstance(value, list):
            return all(inspect(child) for child in value)
        return True

    return inspect(schema)


def openai_schema_supported(schema):
    """Keep optional fields/dynamic maps in the local contract, not strict wire schema."""
    if isinstance(schema,list):return all(openai_schema_supported(v) for v in schema)
    if not isinstance(schema,dict):return True
    if schema.get('type')=='object' or 'properties' in schema:
        if schema.get('additionalProperties') is not False:return False
        if set(schema.get('required',[]))!=set(schema.get('properties',{})):return False
    return all(openai_schema_supported(v) for k,v in schema.items() if k not in {'properties','$defs'}) and all(openai_schema_supported(v) for k in ('properties','$defs') for v in schema.get(k,{}).values())


def resolve_output_mode(provider: str, model: str, base_url: str,
                        declared: str | None = None,
                        schema: dict | None = None) -> OutputMode:
    """Resolve the wire format without probing a model with a known-bad call.

    `declared` is reserved for per-model capability metadata supplied by a
    provider catalog. Unknown/custom endpoints fall back to prompted JSON,
    which avoids sending optional protocol extensions they may not implement.
    """
    if declared:
        try:
            mode = OutputMode(declared)
            if mode is OutputMode.JSON_SCHEMA and isinstance(schema, dict):
                if provider == 'google' and not _google_schema_subset(schema):
                    return OutputMode.PROMPT_JSON
                if provider == 'anthropic' and not _anthropic_schema_subset(schema):
                    return OutputMode.PROMPT_JSON
            return mode
        except ValueError:
            pass

    model_id = model.casefold().split(':', 1)[0]
    official = _official_endpoint(provider, base_url)
    if provider == 'google' and official and model_id.startswith('gemini-'):
        return (OutputMode.JSON_SCHEMA if not isinstance(schema, dict) or _google_schema_subset(schema)
                else OutputMode.PROMPT_JSON)
    if provider == 'anthropic' and official and re.search(
            r'(?:opus|sonnet|haiku)-(?:4-[5-9]|5(?:\.\d+)?)(?:[-.]|$)|'
            r'(?:fable|mythos)-5(?:\.\d+)?(?:[-.]|$)', model_id):
        return (OutputMode.JSON_SCHEMA if not isinstance(schema, dict) or _anthropic_schema_subset(schema)
                else OutputMode.PROMPT_JSON)
    if provider in {'openai', 'openai_oauth'} and official:
        if re.match(r'(?:gpt-(?:4o|4\.1|5|6)|o[3-9])(?:[-.]|$)', model_id):
            return OutputMode.JSON_SCHEMA
        if re.match(r'(?:gpt-3\.5|gpt-4)(?:[-.]|$)', model_id):
            return OutputMode.JSON_OBJECT
    return OutputMode.PROMPT_JSON


def schema_instruction(schema: dict) -> str:
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
    return ('Return exactly one JSON object that conforms to this JSON Schema. '
            'Do not include Markdown or text outside the JSON object. Schema: ' + schema_text)


def prompted_json_payload(payload: dict, schema: dict, style: str) -> dict:
    """Remove protocol schema fields and provide the same contract in text."""
    result = json.loads(json.dumps(payload))
    instruction = schema_instruction(schema)
    if style == 'openai':
        result.pop('response_format', None)
        result.setdefault('messages', []).append({'role': 'user', 'content': instruction})
    elif style == 'google':
        generation = result.setdefault('generationConfig', {})
        generation.pop('responseMimeType', None)
        generation.pop('responseJsonSchema', None)
        generation.pop('responseSchema', None)
        if not generation:
            result.pop('generationConfig', None)
        result.setdefault('contents', []).append({'role': 'user', 'parts': [{'text': instruction}]})
    else:
        output_config = result.get('output_config')
        if isinstance(output_config, dict):
            output_config.pop('format', None)
            if not output_config:
                result.pop('output_config', None)
        result.setdefault('messages', []).append({'role': 'user', 'content': instruction})
    return result
