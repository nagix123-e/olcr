"""ACP v1 newline-delimited JSON-RPC bridge. stdout is protocol-only."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit
import uuid

import httpx


class ProtocolError(Exception):
    def __init__(self, message, code=-32602):
        self.message, self.code = message, code


def load_descriptor(path):
    path = Path(path)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise ProtocolError('INSECURE_HOST_DESCRIPTOR')
    data = json.loads(path.read_text())
    url = urlsplit(data['base_url'])
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'} or url.username or url.password
            or url.query or url.fragment or url.path != '/api/mobile/bridge' or data.get('api_version') != 1):
        raise ProtocolError('INVALID_HOST_DESCRIPTOR')
    return data


class ACPBridge:
    def __init__(self, client, send):
        self.client, self.send = client, send
        self.initialized = False
        self.runs = {}
        self.permissions = {}
        self.sessions = set()
        self.watchers = set()
        self.pending_prompts = set()
        self.cancelled_sessions = set()

    async def api(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ProtocolError('OLCR_HOST_TIMEOUT', -32603) from None
        except httpx.HTTPError:
            raise ProtocolError('OLCR_HOST_OFFLINE', -32603) from None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('object required')
        except ValueError:
            raise ProtocolError('OLCR_INVALID_HOST_RESPONSE', -32603) from None
        if response.status_code >= 400:
            detail = payload.get('detail', 'OLCR_REQUEST_FAILED')
            if isinstance(detail, dict) and detail.get('code') == 'PERMISSION_CONFIRMATION_REQUIRED':
                return {'approval_required': detail}
            raise ProtocolError(str(detail), -32603)
        return payload

    def update(self, session, update):
        self.send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': session, 'update': update}})

    async def request_permission(self, session, detail):
        identifier = 'olcr-permission-' + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.permissions[identifier] = (session, future)
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': 'session/request_permission', 'params': {
            'sessionId': session, 'toolCall': {'toolCallId': identifier, 'title': detail['summary'], 'kind': 'other', 'status': 'pending'},
            'options': [{'optionId': 'allow', 'name': 'この指示の実行を許可', 'kind': 'allow_once'},
                        {'optionId': 'reject', 'name': '拒否', 'kind': 'reject_once'}]}})
        try:
            outcome = await asyncio.wait_for(future, 290)
            allowed = outcome.get('outcome', {}).get('optionId') == 'allow'
            await self.api('POST', '/permissions/' + detail['approval_id'], json={'approved': allowed})
            return allowed
        except asyncio.TimeoutError:
            raise ProtocolError('PERMISSION_RESPONSE_TIMEOUT', -32603) from None
        finally:
            self.permissions.pop(identifier, None)

    async def dispatch(self, method, params):
        if method == 'initialize':
            if params.get('protocolVersion') != 1:
                raise ProtocolError('ONLY_ACP_V1_SUPPORTED')
            capabilities = await self.api('GET', '/capabilities')
            self.initialized = True
            return {'protocolVersion': 1, 'agentInfo': {'name': 'olcr', 'version': '0.1.0', 'title': 'OLCR'},
                'agentCapabilities': {'loadSession': True, 'promptCapabilities': {'image': False, 'audio': False, 'embeddedContext': False},
                                      'mcpCapabilities': {'http': False, 'sse': False}}, 'authMethods': [],
                '_meta': {'olcrCapabilities': capabilities}}
        if not self.initialized:
            raise ProtocolError('INITIALIZE_REQUIRED')
        if method == 'session/new':
            result = await self.api('POST', '/sessions', json=params)
            self.sessions.add(result['sessionId'])
            return result
        session = params.get('sessionId')
        if method == 'session/load':
            if params.get('mcpServers'):
                raise ProtocolError('EXTERNAL_MCP_CONFIG_UNSUPPORTED')
            result = await self.api('GET', '/conversations/' + str(session))
            self.sessions.add(session)
            for message in result.get('messages', []):
                if message['role'] in {'user', 'assistant'}:
                    self.update(session, {'sessionUpdate': 'user_message_chunk' if message['role'] == 'user' else 'agent_message_chunk',
                                          'content': {'type': 'text', 'text': message['content']}})
            active = result.get('active_run')
            if active:
                self.runs[session] = active['id']
                task = asyncio.create_task(self._resume(session, active['id']))
                self.watchers.add(task)
                task.add_done_callback(self.watchers.discard)
            return {}
        if session not in self.sessions:
            raise ProtocolError('SESSION_NOT_LOADED')
        if method == 'session/cancel':
            if session in self.pending_prompts:
                self.cancelled_sessions.add(session)
            if session not in self.runs:
                active = await self.api('GET', '/conversations/' + str(session) + '/active-run')
                if active.get('run'):
                    self.runs[session] = active['run']['id']
            if session in self.runs:
                await self.api('POST', '/runs/' + self.runs[session] + '/cancel')
            for pending_session, future in list(self.permissions.values()):
                if pending_session == session and not future.done():
                    future.set_result({'outcome': {'outcome': 'cancelled'}})
            return None
        if method != 'session/prompt':
            raise ProtocolError('METHOD_NOT_SUPPORTED: ' + method, -32601)
        blocks = params.get('prompt', [])
        if not blocks or any(block.get('type') != 'text' for block in blocks):
            raise ProtocolError('ONLY_TEXT_PROMPTS_SUPPORTED')
        if session in self.pending_prompts:
            raise ProtocolError('SESSION_PROMPT_ALREADY_RUNNING')
        self.pending_prompts.add(session)
        try:
            return await self.prompt(session, params, blocks)
        finally:
            self.pending_prompts.discard(session)
            self.cancelled_sessions.discard(session)

    async def prompt(self, session, params, blocks):
        body = {'conversation_id': session, 'message': '\n'.join(block['text'] for block in blocks),
                'request_id': params.get('_meta', {}).get('olcrRequestId') or uuid.uuid4().hex}
        run = await self.api('POST', '/runs', json=body)
        if 'approval_required' in run:
            detail = run['approval_required']
            if not await self.request_permission(session, detail):
                return {'stopReason': 'cancelled' if session in self.cancelled_sessions else 'refusal'}
            run = await self.api('POST', '/runs', json=body, headers={'X-OLCR-Approval': detail['approval_id']})
        self.runs[session] = run['id']
        if session in self.cancelled_sessions:
            await self.api('POST', '/runs/' + run['id'] + '/cancel')
        return await self.observe(session, run['id'])

    async def _resume(self, session, run_id):
        try:
            await self.observe(session, run_id)
        except ProtocolError as exc:
            self.update(session, {'sessionUpdate':'agent_message_chunk', 'content':{'type':'text','text':exc.message}})

    async def observe(self, session, run_id):
        cursor = 0
        tools = set()
        stop = 'cancelled' if session in self.cancelled_sessions else 'end_turn'
        try:
            while True:
                data = await self.api('GET', '/runs/' + run_id, params={'after': cursor})
                for row in data['events']:
                    if row['seq'] <= cursor:
                        continue
                    event = row['event']
                    kind = event.get('type')
                    if kind in {'chunk', 'reasoning_delta'}:
                        self.update(session, {'sessionUpdate': 'agent_message_chunk' if kind == 'chunk' else 'agent_thought_chunk',
                            'content': {'type': 'text', 'text': event.get('text') or event.get('delta') or (event.get('reasoning') or {}).get('text') or ''}})
                    elif kind == 'task_update':
                        task = event['task']
                        state = task.get('status')
                        status = 'completed' if state == 'COMPLETED' else 'failed' if state in {'FAILED','BLOCKED'} else 'pending' if state in {'WAITING_FOR_USER','RESUMABLE'} else 'in_progress'
                        self.update(session, {'sessionUpdate': 'tool_call' if str(task['id']) not in tools else 'tool_call_update',
                            'toolCallId': str(task['id']), 'title': 'Coding Orchestrator · ' + str(state),
                            'kind': 'other', 'status': status, '_meta': {'olcrTask': task}})
                        tools.add(str(task['id']))
                    elif kind == 'error':
                        raise ProtocolError(event.get('code') or 'OLCR_EXECUTION_ERROR', -32603)
                    elif kind == 'cancelled':
                        stop = 'cancelled'
                    elif kind not in {'meta', 'done'}:
                        self.send({'jsonrpc': '2.0', 'method': '_olcr/event',
                                   'params': {'sessionId': session, 'event': event, 'seq': row['seq']}})
                    cursor = row['seq']
                if data['run']['state'] != 'running' and len(data['events']) < 256:
                    if data['run']['state'] in {'failed', 'interrupted'}:
                        raise ProtocolError('OLCR_RUN_' + data['run']['state'].upper(), -32603)
                    return {'stopReason': stop}
                await asyncio.sleep(.15)
        finally:
            self.runs.pop(session, None)

    async def handle(self, message):
        identifier = message.get('id')
        if 'method' not in message:
            entry = self.permissions.get(identifier)
            if entry and not entry[1].done():
                entry[1].set_result(message.get('result', {}))
            return
        try:
            result = await self.dispatch(message['method'], message.get('params') or {})
            if identifier is not None:
                self.send({'jsonrpc': '2.0', 'id': identifier, 'result': result})
        except (ProtocolError, ValueError, KeyError, TypeError) as exc:
            if identifier is not None:
                self.send({'jsonrpc': '2.0', 'id': identifier, 'error': {
                    'code': getattr(exc, 'code', -32602), 'message': getattr(exc, 'message', 'INVALID_REQUEST')}})


async def serve(path):
    descriptor = load_descriptor(path)
    def send(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    async with httpx.AsyncClient(base_url=descriptor['base_url'] + '/', timeout=20,
            headers={'X-OLCR-Bridge': descriptor['token']}, trust_env=False) as client:
        # A leading slash in httpx resolves against the base path, as used below.
        bridge = ACPBridge(client, send)
        tasks = set()
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, 1_048_577)
            if not line:
                break
            try:
                if len(line) > 1_048_576:
                    raise ValueError('frame too large')
                message = json.loads(line)
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('invalid request')
                task = asyncio.create_task(bridge.handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            except ValueError:
                send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'INVALID_JSON_RPC_FRAME'}})
        # Closing the adapter must not cancel PC-owned runs.
        tasks.update(bridge.watchers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    from olcr_api.config import default_db_path
    parser = argparse.ArgumentParser(description='OLCR ACP v1 adapter for Happier Custom ACP')
    parser.add_argument('--host-file', default=str(Path(default_db_path()).parent / 'mobile-host.json'))
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.host_file))
    except (OSError, ProtocolError, ValueError):
        print('OLCR ACP: local host unavailable or descriptor invalid; start OLCR first.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
