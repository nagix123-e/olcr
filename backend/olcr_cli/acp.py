"""ACP v1 newline-delimited JSON-RPC bridge. stdout is protocol-only."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import quote, urlsplit
import uuid

import httpx

LEGACY_DIRECTORY_REQUEST = '__OLCR_MOBILE_DIRECTORY_V1__'
LEGACY_DIRECTORY_RESPONSE = 'OLCR_PC_CONVERSATIONS_V1\n'
LEGACY_DIRECTORY_SESSION_PREFIX = 'olcr-mobile-library-'
DIRECTORY_REQUEST = '__OLCR_MOBILE_DIRECTORY_V2__'
DIRECTORY_RESPONSE = 'OLCR_PC_LIBRARY_V2\n'
DIRECTORY_SESSION_PREFIX = 'olcr-mobile-library-v3-'
RUN_OBSERVER_SESSION_PREFIX = 'olcr-mobile-run-watch-v1-'
RUN_OBSERVER_EVENT_PREFIX = 'OLCR_PC_RUN_EVENT_V1\n'


class ProtocolError(Exception):
    def __init__(self, message, code=-32602, *, retryable=False):
        self.message, self.code, self.retryable = message, code, retryable


def load_descriptor(path):
    path = Path(path)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise ProtocolError('INSECURE_HOST_DESCRIPTOR')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('base_url'), str):
        raise ProtocolError('INVALID_HOST_DESCRIPTOR')
    url = urlsplit(data['base_url'])
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'} or url.username or url.password
            or url.query or url.fragment or url.path != '/api/mobile/bridge' or data.get('api_version') != 1
            or not isinstance(data.get('token'), str) or not data['token']):
        raise ProtocolError('INVALID_HOST_DESCRIPTOR')
    return data


class HostClient:
    """Resolve the current desktop bridge for each request without replaying it."""

    def __init__(self, descriptor_path, http):
        self.descriptor_path, self.http = descriptor_path, http

    async def request(self, method, path, *, headers=None, **kwargs):
        try:
            descriptor = load_descriptor(self.descriptor_path)
        except FileNotFoundError:
            raise ProtocolError('OLCR_HOST_OFFLINE', -32603, retryable=True) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise ProtocolError('INVALID_HOST_DESCRIPTOR', -32603) from None
        return await self.http.request(method, descriptor['base_url'] + path,
            headers={**(headers or {}), 'X-OLCR-Bridge': descriptor['token']}, **kwargs)


class ACPBridge:
    def __init__(self, client, send, mode='chat'):
        self.client, self.send = client, send
        self.mode = mode
        self.observer_run_id = os.environ.get('OLCR_MOBILE_RUN_ID', '') if mode == 'observer' else ''
        self.initialized = False
        self.runs = {}
        self.permissions = {}
        self.sessions = set()
        self.watchers = set()
        self.run_feed_cursors = {}
        self.session_watchers = {}
        self.session_observer_tails = {}
        self.pending_active_run_ids = {}
        self.observed_run_ids = set()
        self.read_only_runs = {}
        self.pending_prompts = set()
        self.cancelled_sessions = set()
        self.created_conversations = {}

    async def api(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ProtocolError('OLCR_HOST_TIMEOUT', -32603, retryable=True) from None
        except httpx.HTTPError:
            raise ProtocolError('OLCR_HOST_OFFLINE', -32603, retryable=True) from None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('object required')
        except ValueError:
            if response.status_code >= 500:
                raise ProtocolError('OLCR_HOST_HTTP_' + str(response.status_code), -32603,
                                    retryable=True) from None
            raise ProtocolError('OLCR_INVALID_HOST_RESPONSE', -32603) from None
        if response.status_code >= 400:
            detail = payload.get('detail', 'OLCR_REQUEST_FAILED')
            if isinstance(detail, dict) and detail.get('code') == 'PERMISSION_CONFIRMATION_REQUIRED':
                return {'approval_required': detail}
            retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
            raise ProtocolError(str(detail), -32603, retryable=retryable)
        return payload

    async def directory_operation(self, command):
        kind = command.get('kind')
        def identifier(value):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ProtocolError('DIRECTORY_ACTION_ID_INVALID')
            return quote(value, safe='')

        if kind == 'projects':
            return await self.api('GET', '/projects')
        if kind == 'history':
            result = await self.api('GET', '/conversations/' + identifier(command.get('conversation_id')))
            rows = [item for item in result.get('messages', [])
                    if item.get('role') in {'user', 'assistant'} and isinstance(item.get('content'), str)]
            messages, budget = [], 32_000
            truncated = len(rows) > 50
            for item in reversed(rows[-50:]):
                content = item['content']
                if budget <= 0:
                    truncated = True
                    break
                excerpt = content[:min(budget, 24_000)]
                truncated = truncated or len(excerpt) < len(content)
                messages.append({'role': item['role'], 'text': excerpt})
                budget -= len(excerpt)
            return {'messages': list(reversed(messages)), 'truncated': truncated,
                    'active_run': result.get('active_run'),
                    'run_cursor': result.get('run_cursor', 0)}
        if kind == 'active_run':
            return await self.api('GET', '/conversations/' + identifier(command.get('conversation_id')) + '/active-run')
        if kind == 'youtube_search':
            query, language = command.get('query'), command.get('language', 'ja')
            if not isinstance(query, str) or not query.strip() or len(query) > 300:
                raise ProtocolError('YOUTUBE_QUERY_INVALID')
            if language not in {'ja', 'en'}:
                raise ProtocolError('YOUTUBE_LANGUAGE_INVALID')
            return await self.api('POST', '/youtube/search', json={'query': query.strip(), 'language': language})
        if kind == 'youtube_search_decision':
            approval_id = command.get('approval_id')
            query, language, approved = command.get('query'), command.get('language', 'ja'), command.get('approved')
            if not isinstance(approval_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', approval_id):
                raise ProtocolError('YOUTUBE_APPROVAL_INVALID')
            if not isinstance(query, str) or not query.strip() or len(query) > 300:
                raise ProtocolError('YOUTUBE_QUERY_INVALID')
            if language not in {'ja', 'en'} or not isinstance(approved, bool):
                raise ProtocolError('YOUTUBE_APPROVAL_INVALID')
            await self.api('POST', '/permissions/' + quote(approval_id, safe=''), json={'approved': approved})
            if not approved:
                return {'approved': False}
            return await self.api('POST', '/youtube/search', json={'query': query.strip(), 'language': language},
                                  headers={'X-OLCR-Approval': approval_id})
        if kind == 'conversations':
            offset = command.get('offset', 0)
            if not isinstance(offset, int) or offset < 0:
                raise ProtocolError('DIRECTORY_OFFSET_INVALID')
            params = {'offset': offset, 'limit': 100}
            if 'project_id' in command:
                project_id = command.get('project_id')
                if project_id is None:
                    params['projectless'] = 'true'
                elif isinstance(project_id, str) and project_id:
                    params['project_id'] = project_id
                else:
                    raise ProtocolError('DIRECTORY_PROJECT_INVALID')
            return await self.api('GET', '/conversations', params=params)
        if kind != 'action':
            raise ProtocolError('DIRECTORY_COMMAND_UNSUPPORTED')
        action = command.get('action')
        if not isinstance(action, dict):
            raise ProtocolError('DIRECTORY_ACTION_INVALID')
        operation = action.get('type')

        if operation == 'project.rename':
            project_id = identifier(action.get('project_id'))
            return await self.api('PATCH', '/projects/' + project_id, json={'name': action.get('name')})
        if operation == 'project.pin':
            project_id = identifier(action.get('project_id'))
            return await self.api('PUT', '/projects/' + project_id + '/pin', json={'pinned': action.get('pinned')})
        if operation == 'project.delete':
            project_id = identifier(action.get('project_id'))
            return await self.api('DELETE', '/projects/' + project_id)
        if operation == 'conversation.create':
            creation_key = identifier(action.get('creation_key'))
            if creation_key in self.created_conversations:
                return self.created_conversations[creation_key]
            workspace = action.get('workspace')
            project_id = action.get('project_id')
            if not isinstance(workspace, str) or not workspace:
                raise ProtocolError('PC_WORKSPACE_REQUIRED')
            if project_id is not None:
                project_id = identifier(project_id)
            body = {'cwd': workspace}
            if project_id is not None:
                body['project_id'] = project_id
            created = await self.api('POST', '/sessions', json=body)
            created_id = created.get('sessionId')
            if not isinstance(created_id, str):
                raise ProtocolError('PC_CONVERSATION_CREATE_UNCONFIRMED', -32603)
            conversation = await self.api('GET', '/conversations/' + identifier(created_id))
            self.created_conversations[creation_key] = conversation
            return conversation
        if operation == 'conversation.rename':
            conversation_id = identifier(action.get('conversation_id'))
            return await self.api('PATCH', '/conversations/' + conversation_id, json={'title': action.get('title')})
        if operation == 'conversation.pin':
            conversation_id = identifier(action.get('conversation_id'))
            return await self.api('PUT', '/conversations/' + conversation_id + '/pin',
                                  json={'pinned': action.get('pinned')})
        if operation == 'conversation.delete':
            conversation_id = identifier(action.get('conversation_id'))
            return await self.api('DELETE', '/conversations/' + conversation_id)
        raise ProtocolError('DIRECTORY_ACTION_UNSUPPORTED')

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
            return {'protocolVersion': 1, 'agentInfo': {'name': 'olcr', 'version': '0.2.0', 'title': 'OLCR'},
                'agentCapabilities': {'loadSession': True, 'promptCapabilities': {'image': False, 'audio': False, 'embeddedContext': False},
                                      'mcpCapabilities': {'http': False, 'sse': False}}, 'authMethods': [],
                '_meta': {'olcrCapabilities': capabilities}}
        if not self.initialized:
            raise ProtocolError('INITIALIZE_REQUIRED')
        if method in {'session/new', 'session/load'} and params.get('mcpServers'):
            raise ProtocolError('EXTERNAL_MCP_CONFIG_UNSUPPORTED')
        if method == 'session/new':
            if self.mode == 'observer':
                if not re.fullmatch(r'[0-9a-f]{32}', self.observer_run_id):
                    raise ProtocolError('RUN_OBSERVER_ID_INVALID')
                result = {'sessionId': RUN_OBSERVER_SESSION_PREFIX + self.observer_run_id}
            elif self.mode == 'directory':
                result = {'sessionId': DIRECTORY_SESSION_PREFIX + uuid.uuid4().hex}
            else:
                body = dict(params)
                existing_id = os.environ.get('OLCR_EXISTING_CONVERSATION_ID')
                if existing_id:
                    body['existing_conversation_id'] = existing_id
                else:
                    project_id = os.environ.get('OLCR_NEW_CONVERSATION_PROJECT_ID')
                    if project_id:
                        body['project_id'] = project_id
                result = await self.api('POST', '/sessions', json=body)
            self.sessions.add(result['sessionId'])
            if self.mode == 'chat':
                cursor = os.environ.get('OLCR_RUN_FEED_CURSOR')
                active_id = os.environ.get('OLCR_ACTIVE_RUN_ID')
                try:
                    cursor = int(cursor) if cursor is not None else None
                except ValueError:
                    cursor = None
                await self.start_run_feed(result['sessionId'], cursor=cursor, active_run_id=active_id)
            return result
        session = params.get('sessionId')
        if method == 'session/load':
            if isinstance(session, str) and session.startswith(RUN_OBSERVER_SESSION_PREFIX):
                run_id = session.removeprefix(RUN_OBSERVER_SESSION_PREFIX)
                if not re.fullmatch(r'[0-9a-f]{32}', run_id):
                    raise ProtocolError('RUN_OBSERVER_ID_INVALID')
                self.mode = 'observer'
                self.observer_run_id = run_id
                self.sessions.add(session)
                return {}
            if isinstance(session, str) and (session.startswith(DIRECTORY_SESSION_PREFIX)
                                              or session.startswith(LEGACY_DIRECTORY_SESSION_PREFIX)):
                self.mode = 'directory'
                self.sessions.add(session)
                return {}
            if self.mode == 'directory':
                raise ProtocolError('DIRECTORY_SESSION_INVALID')
            result = await self.api('GET', '/conversations/' + str(session))
            self.sessions.add(session)
            for message in result.get('messages', []):
                if message['role'] in {'user', 'assistant'}:
                    self.update(session, {'sessionUpdate': 'user_message_chunk' if message['role'] == 'user' else 'agent_message_chunk',
                                          'content': {'type': 'text', 'text': message['content']}})
            await self.start_run_feed(session, cursor=result.get('run_cursor'), active_run=result.get('active_run'))
            return {}
        if session not in self.sessions:
            raise ProtocolError('SESSION_NOT_LOADED')
        if method == 'session/cancel':
            if session in self.pending_prompts:
                self.cancelled_sessions.add(session)
            observed_id = self.read_only_runs.get(session)
            read_only_current = observed_id is not None and self.runs.get(session) == observed_id
            if not read_only_current and session not in self.runs:
                active = await self.api('GET', '/conversations/' + str(session) + '/active-run')
                if active.get('run'):
                    self.runs[session] = active['run']['id']
            if session in self.runs and not read_only_current:
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
        if self.mode in {'directory', 'observer'}:
            request = '\n'.join(block['text'] for block in blocks)
            # Happier can prepend context, including older commands in its transcript.
            # The current V2 request is the final nonempty line.
            if request.strip():
                final_line = request.rstrip().rsplit('\n', 1)[-1]
                if final_line.startswith(DIRECTORY_REQUEST + ':{'):
                    request = final_line
            try:
                if request == DIRECTORY_REQUEST or request.startswith(DIRECTORY_REQUEST + ':'):
                    request_prefix, response_prefix = DIRECTORY_REQUEST, DIRECTORY_RESPONSE
                    v2 = True
                elif request == LEGACY_DIRECTORY_REQUEST or request.startswith(LEGACY_DIRECTORY_REQUEST + ':'):
                    request_prefix, response_prefix = LEGACY_DIRECTORY_REQUEST, LEGACY_DIRECTORY_RESPONSE
                    v2 = False
                else:
                    raise ValueError
                if request == request_prefix:
                    command = {'kind': 'conversations', 'offset': 0}
                    request_id = None
                    legacy = True
                else:
                    raw_command = request[len(request_prefix) + 1:]
                    if v2 and raw_command.startswith('{'):
                        command = json.loads(raw_command)
                        if not isinstance(command, dict):
                            raise ValueError
                        request_id = command.get('request_id')
                        if not isinstance(request_id, str) or not request_id:
                            raise ValueError
                        legacy = False
                    else:
                        raw_offset, separator, raw_request_id = raw_command.partition(':')
                        command = {'kind': 'conversations', 'offset': int(raw_offset)}
                        request_id = raw_request_id if separator and raw_request_id else None
                        legacy = True
            except ValueError:
                raise ProtocolError('DIRECTORY_OFFSET_INVALID') from None
            try:
                if self.mode == 'observer':
                    if (command.get('kind') != 'run_observer'
                            or command.get('run_id') != self.observer_run_id):
                        raise ProtocolError('RUN_OBSERVER_REQUEST_INVALID')
                    after = command.get('after', 0)
                    if not isinstance(after, int) or after < 0:
                        raise ProtocolError('RUN_OBSERVER_CURSOR_INVALID')
                    result = await self.observe_pc_run(session, self.observer_run_id, after)
                else:
                    result = await self.directory_operation(command)
                if legacy:
                    payload = result | {'offset': command.get('offset', 0)}
                    if request_id:
                        payload['request_id'] = request_id
                else:
                    payload = {'request_id': request_id, 'kind': command.get('kind'), 'result': result}
            except ProtocolError as exc:
                payload = {'request_id': request_id, 'kind': command.get('kind'), 'error': exc.message}
            self.update(session, {'sessionUpdate': 'agent_message_chunk', 'content': {
                'type': 'text', 'text': response_prefix + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}})
            return {'stopReason': 'end_turn'}
        if session in self.pending_prompts:
            raise ProtocolError('SESSION_PROMPT_ALREADY_RUNNING')
        self.pending_prompts.add(session)
        try:
            return await self.prompt(session, params, blocks)
        finally:
            self.pending_prompts.discard(session)
            self.cancelled_sessions.discard(session)

    async def observe_pc_run(self, session, run_id, after=0):
        """Stream durable PC-run events inside a dedicated relay prompt turn."""
        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ProtocolError('RUN_OBSERVER_ID_INVALID')
        if session in self.pending_prompts:
            raise ProtocolError('SESSION_PROMPT_ALREADY_RUNNING')
        cursor = after
        self.pending_prompts.add(session)
        self.read_only_runs[session] = run_id
        try:
            while session in self.sessions and session not in self.cancelled_sessions:
                data = await self.api('GET', '/runs/' + quote(run_id, safe=''), params={'after': cursor})
                rows = data.get('events') if isinstance(data.get('events'), list) else []
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get('seq'), int) or row['seq'] <= cursor:
                        continue
                    envelope = {'seq': row['seq'], 'event': row.get('event')}
                    self.update(session, {'sessionUpdate': 'agent_message_chunk', 'content': {
                        'type': 'text', 'text': RUN_OBSERVER_EVENT_PREFIX +
                        json.dumps(envelope, ensure_ascii=False, separators=(',', ':')) + '\n'}})
                    cursor = row['seq']
                run = data.get('run') if isinstance(data.get('run'), dict) else {}
                if run.get('state') != 'running' and len(rows) < 256:
                    return {'run': run, 'cursor': cursor}
                await asyncio.sleep(.2)
            return {'run': {'id': run_id, 'state': 'interrupted'}, 'cursor': cursor}
        finally:
            if self.read_only_runs.get(session) == run_id:
                self.read_only_runs.pop(session, None)
            self.pending_prompts.discard(session)
            self.cancelled_sessions.discard(session)

    async def prompt(self, session, params, blocks):
        body = {'conversation_id': session, 'message': '\n'.join(block['text'] for block in blocks),
                'request_id': params.get('_meta', {}).get('olcrRequestId') or uuid.uuid4().hex}
        run = await self.start_run_when_available(session, body)
        if run is None:
            return {'stopReason': 'cancelled'}
        if 'approval_required' in run:
            detail = run['approval_required']
            if not await self.request_permission(session, detail):
                return {'stopReason': 'cancelled' if session in self.cancelled_sessions else 'refusal'}
            run = await self.start_run_when_available(session, body, {'X-OLCR-Approval': detail['approval_id']})
            if run is None:
                return {'stopReason': 'cancelled'}
        self.runs[session] = run['id']
        if session in self.cancelled_sessions:
            await self.api('POST', '/runs/' + run['id'] + '/cancel')
        return await self.observe(session, run['id'])

    async def start_run_when_available(self, session, body, headers=None):
        """Keep an ACP prompt queued until the shared OLCR conversation is free."""
        path = '/conversations/' + quote(session, safe='') + '/active-run'
        while session not in self.cancelled_sessions:
            active = await self.api('GET', path)
            if not active.get('run'):
                try:
                    return await self.api('POST', '/runs', json=body, headers=headers)
                except ProtocolError as exc:
                    # Another PC or mobile runner may win the race after the
                    # active-run check. Preserve this prompt and wait for its turn.
                    if exc.message != 'CONVERSATION_BUSY_USE_NEXT_TURN_QUEUE':
                        raise
            await asyncio.sleep(.5)
        return None

    async def _resume(self, session, run_id):
        try:
            await self.observe(session, run_id)
        except ProtocolError as exc:
            self.update(session, {'sessionUpdate':'agent_message_chunk', 'content':{'type':'text','text':exc.message}})

    async def start_run_feed(self, session, *, cursor=None, active_run=None, active_run_id=None):
        task = self.session_watchers.get(session)
        if task and not task.done():
            return
        if cursor is None:
            snapshot = await self.api('GET', '/conversations/' + quote(session, safe=''))
            cursor = snapshot.get('run_cursor', 0)
            active_run = snapshot.get('active_run')
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        self.run_feed_cursors[session] = cursor
        if isinstance(active_run, dict) and isinstance(active_run.get('id'), str):
            self.attach_run(session, active_run)
        elif isinstance(active_run_id, str) and active_run_id:
            self.pending_active_run_ids[session] = active_run_id
            try:
                result = await self.api('GET', '/runs/' + quote(active_run_id, safe=''))
                if isinstance(result.get('run'), dict):
                    self.attach_run(session, result['run'])
                    self.pending_active_run_ids.pop(session, None)
            except ProtocolError:
                pass
        task = asyncio.create_task(self.watch_run_feed(session))
        self.session_watchers[session] = task

    def attach_run(self, session, run):
        run_id = run.get('id')
        if not isinstance(run_id, str) or run_id in self.observed_run_ids:
            return
        # The in-flight prompt already observes its own run; attaching a second
        # reader would duplicate every streamed chunk.
        if run.get('source') == 'mobile' and session in self.pending_prompts:
            self.observed_run_ids.add(run_id)
            return
        self.observed_run_ids.add(run_id)
        previous = self.session_observer_tails.get(session)
        task = asyncio.create_task(self._observe_after(session, run_id, previous))
        self.session_observer_tails[session] = task
        self.watchers.add(task)
        def finished(completed):
            self.watchers.discard(completed)
            if self.session_observer_tails.get(session) is completed:
                self.session_observer_tails.pop(session, None)
        task.add_done_callback(finished)

    async def _observe_after(self, session, run_id, previous):
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        self.runs[session] = run_id
        self.read_only_runs[session] = run_id
        await self._resume_observed(session, run_id)

    async def _resume_observed(self, session, run_id):
        try:
            await self.observe(session, run_id)
        except ProtocolError as exc:
            self.update(session, {'sessionUpdate':'agent_message_chunk', 'content':{'type':'text','text':exc.message}})

    async def watch_run_feed(self, session):
        delay = .5
        path = '/conversations/' + quote(session, safe='') + '/runs'
        while session in self.sessions:
            try:
                pending_id = self.pending_active_run_ids.get(session)
                if pending_id:
                    try:
                        pending = await self.api('GET', '/runs/' + quote(pending_id, safe=''))
                        if isinstance(pending.get('run'), dict):
                            self.attach_run(session, pending['run'])
                            self.pending_active_run_ids.pop(session, None)
                    except ProtocolError:
                        pass
                result = await self.api('GET', path, params={'after': self.run_feed_cursors.get(session, 0)})
                rows = result.get('runs') if isinstance(result.get('runs'), list) else []
                cursor = result.get('cursor')
                if isinstance(cursor, int) and cursor >= self.run_feed_cursors.get(session, 0):
                    self.run_feed_cursors[session] = cursor
                for run in rows:
                    if isinstance(run, dict):
                        self.attach_run(session, run)
                delay = .5 if rows else min(2.0, delay * 1.25)
            except ProtocolError:
                delay = min(8.0, delay * 2)
            await asyncio.sleep(delay)

    async def observe(self, session, run_id):
        cursor = 0
        tools = set()
        stop = 'cancelled' if session in self.cancelled_sessions else 'end_turn'
        delay = .5
        try:
            while True:
                try:
                    data = await self.api('GET', '/runs/' + run_id, params={'after': cursor})
                    delay = .5
                except ProtocolError as exc:
                    if not exc.retryable or session not in self.sessions:
                        raise
                    await asyncio.sleep(delay)
                    delay = min(8.0, delay * 2)
                    continue
                for row in data['events']:
                    if row['seq'] <= cursor:
                        continue
                    event = row['event']
                    kind = event.get('type')
                    if kind == 'user_message':
                        self.update(session, {'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': event.get('text') or ''}})
                    elif kind in {'chunk', 'reasoning_delta'}:
                        self.update(session, {'sessionUpdate': 'agent_message_chunk' if kind == 'chunk' else 'agent_thought_chunk',
                            'content': {'type': 'text', 'text': event.get('text') or event.get('delta') or (event.get('reasoning') or {}).get('text') or ''}})
                    elif kind == 'task_update':
                        task = event['task']
                        state = task.get('status')
                        status = 'completed' if state == 'COMPLETED' else 'failed' if state in {'FAILED','BLOCKED','CANCELLED','INTERRUPTED'} else 'pending' if state in {'WAITING_FOR_USER','RESUMABLE'} else 'in_progress'
                        self.update(session, {'sessionUpdate': 'tool_call' if str(task['id']) not in tools else 'tool_call_update',
                            'toolCallId': str(task['id']), 'title': task.get('activity') or ('Coding Orchestrator · ' + str(state)),
                            'kind': 'other', 'status': status, '_meta': {'olcrTask': task}})
                        tools.add(str(task['id']))
                    elif kind == 'error':
                        message = event.get('message') or event.get('code') or 'OLCR_EXECUTION_ERROR'
                        raise ProtocolError(str(message)[:1000], -32603)
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
            if self.runs.get(session) == run_id:
                self.runs.pop(session, None)
            if self.read_only_runs.get(session) == run_id:
                self.read_only_runs.pop(session, None)

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


async def serve(path, mode='chat'):
    def send(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        bridge = ACPBridge(HostClient(path, client), send, mode)
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
    parser.add_argument('--mode', choices=('chat', 'directory', 'observer'))
    parser.add_argument('--workspace', help=argparse.SUPPRESS)  # Accepted for older paired wrappers.
    args = parser.parse_args()
    mode = args.mode or os.environ.get('OLCR_MOBILE_MODE', 'chat')
    if mode not in {'chat', 'directory', 'observer'}:
        parser.error('invalid OLCR mobile mode')
    try:
        asyncio.run(serve(args.host_file, mode))
    except (OSError, ProtocolError, ValueError):
        print('OLCR ACP: local host unavailable or descriptor invalid; start OLCR first.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
