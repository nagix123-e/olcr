"""Opt-in Infermedica transport. Credentials and patient data never enter logs."""
from __future__ import annotations

import os
import logging
from contextvars import ContextVar

import httpx

_health_request = ContextVar('olcr_health_request', default=False)


class _PrivateTransportLog(logging.Filter):
    def filter(self, record):
        return not _health_request.get()


# httpx otherwise includes the search phrase in its INFO-level request URL.
for _logger in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy'):
    logging.getLogger(_logger).addFilter(_PrivateTransportLog())


class SymptomProviderError(Exception):
    pass


class InfermedicaProvider:
    def __init__(self, app_id: str, app_key: str, *, transport=None):
        self._app_id, self._app_key = app_id, app_key
        self._transport = transport

    def request(self, endpoint: str, payload: dict, *, interview_id: str, language: str):
        if endpoint not in {'search', 'diagnosis', 'triage', 'recommend_specialist'}:
            raise SymptomProviderError('Unsupported operation')
        headers = {'App-Id': self._app_id, 'App-Key': self._app_key,
                   'Model': 'infermedica-ja' if language == 'ja' else 'infermedica-en',
                   'Interview-Id': interview_id}
        privacy_token = _health_request.set(True)
        try:
            with httpx.Client(transport=self._transport, timeout=15, trust_env=False,
                              follow_redirects=False) as client:
                url = 'https://api.infermedica.com/v3/' + endpoint
                response = (client.get(url, params=payload, headers=headers) if endpoint == 'search'
                            else client.post(url, json=payload, headers=headers))
                response.raise_for_status()
                if len(response.content) > 512 * 1024:
                    raise SymptomProviderError('Invalid provider response')
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise SymptomProviderError('Symptom provider unavailable') from None
        finally:
            _health_request.reset(privacy_token)


def configured_provider():
    app_id = os.environ.get('OLCR_INFERMEDICA_APP_ID', '').strip()
    app_key = os.environ.get('OLCR_INFERMEDICA_APP_KEY', '').strip()
    return InfermedicaProvider(app_id, app_key) if app_id and app_key else None
