import asyncio
import base64
import json

import httpx
import pytest
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import VerifyKey


def test_open_acp_bridge_uses_new_pc_endpoint_after_restart(tmp_path):
    from olcr_cli.acp import ACPBridge, HostClient, ProtocolError

    descriptor = tmp_path / 'mobile-host.json'

    def start_host(port, token):
        candidate = tmp_path / 'replacement'
        candidate.write_text(json.dumps({'api_version': 1,
            'base_url': f'http://127.0.0.1:{port}/api/mobile/bridge', 'token': token}))
        candidate.chmod(0o600)
        candidate.replace(descriptor)

    def host(request):
        assert request.url.path == '/api/mobile/bridge/projects'
        expected = {12345: 'before', 12346: 'after'}[request.url.port]
        assert request.headers['X-OLCR-Bridge'] == expected
        return httpx.Response(200, json={'projects': [{'id': expected}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(host)) as http:
            bridge = ACPBridge(HostClient(descriptor, http), lambda _: None, 'directory')
            start_host(12345, 'before')
            assert await bridge.directory_operation({'kind': 'projects'}) == {'projects': [{'id': 'before'}]}
            descriptor.unlink()
            with pytest.raises(ProtocolError, match='OLCR_HOST_OFFLINE'):
                await bridge.directory_operation({'kind': 'projects'})
            start_host(12346, 'after')
            assert await bridge.directory_operation({'kind': 'projects'}) == {'projects': [{'id': 'after'}]}

    asyncio.run(scenario())


def test_directory_accepts_final_command_with_happier_preamble():
    from olcr_cli.acp import ACPBridge, DIRECTORY_REQUEST, DIRECTORY_RESPONSE, ProtocolError

    async def scenario():
        command = DIRECTORY_REQUEST + ':' + json.dumps({'kind': 'projects', 'request_id': 'read'})
        old_command = DIRECTORY_REQUEST + ':' + json.dumps({'kind': 'action',
            'action': {'type': 'project.delete', 'project_id': 'old'}, 'request_id': 'old'})
        output, calls = [], []

        def host(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/conversations/existing':
                return httpx.Response(200, json={'messages': [
                    {'role': 'user', 'content': 'A saved question'},
                    {'role': 'tool', 'content': 'Internal result'},
                    {'role': 'assistant', 'content': 'A saved answer'}]})
            return httpx.Response(200, json={'projects': []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(host),
                                     base_url='http://127.0.0.1') as http:
            bridge = ACPBridge(http, output.append, 'directory')
            bridge.initialized = True
            session = (await bridge.dispatch('session/new', {}))['sessionId']
            prompt = lambda text: {'sessionId': session, 'prompt': [{'type': 'text', 'text': text}]}
            await bridge.dispatch('session/prompt', prompt('Happier context\n' + old_command + '\n\n' + command))
            assert calls == [('GET', '/projects')]
            response = output[-1]['params']['update']['content']['text']
            assert json.loads(response.removeprefix(DIRECTORY_RESPONSE))['request_id'] == 'read'
            with pytest.raises(ProtocolError):
                await bridge.dispatch('session/prompt', prompt(old_command + '\nOrdinary text'))
            assert calls == [('GET', '/projects')]
            history = await bridge.directory_operation({'kind': 'history', 'conversation_id': 'existing'})
            assert calls[-1] == ('GET', '/conversations/existing')
            assert history == {'messages': [
                {'role': 'user', 'text': 'A saved question'},
                {'role': 'assistant', 'text': 'A saved answer'}], 'truncated': False}

    asyncio.run(scenario())


def test_pairing_uses_signed_identity_and_encrypted_account_link():
    from olcr_api.mobile_pairing import HappierPairing, PairingError

    async def scenario():
        phone = PrivateKey.generate()
        public = base64.b64encode(bytes(phone.public_key)).decode()
        sent = []
        secrets = {}
        def relay(request):
            body = json.loads(request.content or '{}')
            sent.append((request.url.path, body))
            if request.url.path == '/v1/auth':
                VerifyKey(base64.b64decode(body['publicKey'])).verify(
                    base64.b64decode(body['challenge']), base64.b64decode(body['signature']))
                return httpx.Response(200, json={'token': 'test-token'})
            if request.url.path.endswith('/start'):
                assert 'secretHash' in body and 'secret' not in body
                return httpx.Response(200, json={'pairId': 'pair-1', 'expiresAt': '2099-01-01T00:00:00Z'})
            if request.url.path.endswith('/status'):
                return httpx.Response(200, json={'state': 'requested', 'confirmCode': '123 456',
                    'requestedPublicKey': public, 'requestedDeviceLabel': 'My phone',
                    'supportsCredentialVariants': True})
            return httpx.Response(200, json={'success': True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(relay)) as client:
            service = HappierPairing('https://relay.example', 'profile', client=client,
                read_secret=secrets.get, save_secret=lambda key, value: secrets.__setitem__(key, value))
            session = await service.start()
            assert session['url'].startswith('happier:///pair?')
            with pytest.raises(PairingError):
                await service.approve('wrong')
            assert not any(path.endswith('/response') for path, _ in sent)
            await service.approve('123 456')
            answer = next(body for path, body in sent if path.endswith('/response'))
            bundle = base64.b64decode(answer['response'])
            plaintext = Box(phone, PublicKey(bundle[:32])).decrypt(bundle[56:], bundle[32:56])
            assert plaintext == b'HPAC\0' + base64.b64decode(next(iter(secrets.values())))
            with pytest.raises(PairingError):
                await service.approve('123 456')
    asyncio.run(scenario())


def test_durable_runs_survive_observer_disconnect_and_deduplicate(tmp_path):
    from olcr_api.mobile_runs import MobileRuns

    async def scenario():
        started = 0
        async def source(payload, generation):
            nonlocal started
            started += 1
            yield {'type': 'chunk', 'text': 'first'}
            await asyncio.sleep(.03)
            yield {'type': 'done'}
        runs = MobileRuns(str(tmp_path / 'runs.db'), source)
        body = {'conversation_id': 'conversation', 'message': 'hello', 'request_id': 'one'}
        first = runs.start(body)
        assert runs.start(body)['id'] == first['id']
        with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
            runs.start(body | {'message': 'different'})
        await asyncio.sleep(.08)  # no observer is connected while the PC executes
        events = runs.events(first['id'], 0)
        assert [e['seq'] for e in events] == [1, 2]
        assert runs.events(first['id'], 1) == events[1:]
        assert runs.get(first['id'])['state'] == 'completed'
        assert started == 1
        restored = MobileRuns(str(tmp_path / 'runs.db'), source)
        assert restored.events(first['id'], 0) == events
    asyncio.run(scenario())


def test_run_restart_is_interrupted_not_resubmitted(tmp_path):
    from olcr_api.mobile_runs import MobileRuns
    async def source(payload, generation):
        await asyncio.sleep(100)
        yield {'type': 'done'}
    async def scenario():
        path = str(tmp_path / 'runs.db')
        runs = MobileRuns(path, source)
        run = runs.start({'conversation_id': 'c', 'message': 'write', 'request_id': 'r'})
        restored = MobileRuns(path, source)
        assert restored.get(run['id'])['state'] == 'interrupted'
        assert restored.start({'conversation_id': 'c', 'message': 'write', 'request_id': 'r'})['id'] == run['id']
        await runs.close()
    asyncio.run(scenario())


def test_bridge_requires_local_credential_and_authorized_workspace(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from types import SimpleNamespace
    from olcr_api.db import Database
    from olcr_api.mobile_api import BridgeAccess, install_mobile_api
    from olcr_cli.acp import load_descriptor
    monkeypatch.setenv('OLCR_GUI_SESSION_TOKEN', 'desktop-secret')
    db = Database(str(tmp_path / 'olcr.db'))
    db.initialize()
    access = BridgeAccess(tmp_path, 12345)
    assert load_descriptor(access.path)['token'] == access.token
    async def source(body, generation):
        yield {'type': 'done'}
    app = FastAPI()
    install_mobile_api(app, db=db, settings=lambda: SimpleNamespace(allowed_roots=[str(tmp_path)]),
                       access=access, source=source, approve_request=lambda *args: {})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 4000)),
                                     base_url='http://127.0.0.1') as client:
            assert (await client.get('/api/mobile/bridge/capabilities')).status_code == 401
            headers = {'X-OLCR-Bridge': access.token}
            assert (await client.get('/api/mobile/bridge/capabilities', headers=headers)).status_code == 200
            assert (await client.get('/api/mobile/bridge/capabilities', headers=headers | {'Origin':'https://evil.example'})).status_code == 401
            bad = await client.post('/api/mobile/bridge/sessions', headers=headers, json={'cwd': '/'})
            assert bad.status_code == 403
            created = await client.post('/api/mobile/bridge/sessions', headers=headers, json={'cwd': str(tmp_path), 'mcpServers': []})
            assert created.status_code == 200
            identifier = created.json()['sessionId']
            from olcr_api.working_folders import WorkingFolders
            assert WorkingFolders(db).get(identifier)['root'] == str(tmp_path)
            assert (await client.post('/api/mobile/pairing', headers=headers, json={'consent': True})).status_code == 401
    asyncio.run(scenario())


def test_acp_v1_stream_and_permission_without_form_login():
    from olcr_cli.acp import ACPBridge, ProtocolError
    async def scenario():
        output = []
        sent_prompt = []
        def relay(request):
            path = request.url.path
            if path.endswith('/capabilities'):
                return httpx.Response(200, json={})
            if path.endswith('/sessions'):
                return httpx.Response(200, json={'sessionId':'conversation'})
            if path.endswith('/runs'):
                sent_prompt.append(json.loads(request.content))
                if request.headers.get('X-OLCR-Approval') != 'grant':
                    return httpx.Response(409, json={'detail':{'code':'PERMISSION_CONFIRMATION_REQUIRED',
                        'approval_id':'grant', 'summary':'Execute this prompt?'}})
                return httpx.Response(200, json={'id':'run'})
            if '/runs/' in path:
                return httpx.Response(200, json={'run':{'state':'completed'},'events':[
                    {'seq':1,'event':{'type':'chunk','text':'hello'}},{'seq':2,'event':{'type':'done'}}]})
            return httpx.Response(200, json={'approved':True})
        def send(value):
            output.append(value)
            if value.get('method') == 'session/request_permission':
                asyncio.create_task(bridge.handle({'jsonrpc':'2.0','id':value['id'],
                    'result':{'outcome':{'outcome':'selected','optionId':'allow'}}}))
        async with httpx.AsyncClient(base_url='http://127.0.0.1/api/mobile/bridge/', transport=httpx.MockTransport(relay)) as client:
            bridge = ACPBridge(client, send)
            with pytest.raises(ProtocolError, match='INITIALIZE_REQUIRED'):
                await bridge.dispatch('session/new', {})
            initialized = await bridge.dispatch('initialize', {'protocolVersion':1})
            assert initialized['authMethods'] == []
            await bridge.dispatch('session/new', {'cwd':'/authorized','mcpServers':[]})
            result = await bridge.dispatch('session/prompt', {'sessionId':'conversation', 'prompt':[{'type':'text','text':'hello'}]})
            assert result == {'stopReason':'end_turn'}
            assert sent_prompt[0] == sent_prompt[1]  # one idempotency key over permission retry
            assert output[-1]['params']['update']['content']['text'] == 'hello'
            with pytest.raises(ProtocolError, match='ONLY_TEXT_PROMPTS_SUPPORTED'):
                await bridge.dispatch('session/prompt', {'sessionId':'conversation','prompt':[{'type':'image'}]})
    asyncio.run(scenario())


def test_common_host_lock_prevents_live_descriptor_overwrite(tmp_path):
    from olcr_api.mobile_api import BridgeAccess
    first = BridgeAccess(tmp_path, 12345)
    before = first.path.read_text()
    with pytest.raises(RuntimeError, match='PROFILE_ALREADY_OWNED'):
        BridgeAccess(tmp_path, 54321)
    assert first.path.read_text() == before
    first.close()
    second = BridgeAccess(tmp_path, 54321)
    second.close()


def test_coding_handoff_waits_for_task_and_cancels_real_worker():
    from olcr_api.mobile_runs import follow_coding_task
    from olcr_api.generation_control import Generation
    async def scenario():
        generation = Generation()
        states = iter(['QUEUED', 'RUNNING', 'COMPLETED'])
        events = [event async for event in follow_coding_task('task',
            lambda _: {'id':'task','status':next(states)}, lambda _: None, generation, poll_seconds=0)]
        assert [event['type'] for event in events] == ['task_update'] * 3 + ['done']
        generation.cancel()
        paused = []
        cancelled = [event async for event in follow_coding_task('task', lambda _: {}, paused.append, generation)]
        assert paused == ['task'] and cancelled == [{'type':'cancelled'}]
    asyncio.run(scenario())


def test_conversation_deletion_purges_mobile_event_copies(tmp_path):
    from olcr_api.db import Database
    from olcr_api.mobile_runs import MobileRuns
    async def scenario():
        db = Database(str(tmp_path / 'olcr.db'))
        db.initialize()
        db.create_conversation('test', 1, 'conversation', None)
        async def source(body, generation):
            yield {'type':'chunk','text':'private content'}
            yield {'type':'done'}
        runs = MobileRuns(db.path, source)
        run = runs.start({'conversation_id':'conversation','request_id':'id','message':'test'})
        await asyncio.sleep(.02)
        with db.connect() as conn:
            conn.execute('DELETE FROM conversations WHERE id=?', ('conversation',))
            assert conn.execute('SELECT COUNT(*) FROM mobile_run_events').fetchone()[0] == 0
        with pytest.raises(KeyError):
            runs.get(run['id'])
    asyncio.run(scenario())


def test_acp_load_reattaches_active_run_and_nested_reasoning():
    from olcr_cli.acp import ACPBridge
    async def scenario():
        output = []
        def host(request):
            if '/runs/' in request.url.path:
                return httpx.Response(200,json={'run':{'state':'completed'},'events':[
                    {'seq':1,'event':{'type':'reasoning_delta','reasoning':{'text':'thinking'}}},
                    {'seq':2,'event':{'type':'chunk','text':'resumed answer'}}]})
            return httpx.Response(200,json={'messages':[], 'active_run':{'id':'active'}})
        async with httpx.AsyncClient(base_url='http://127.0.0.1/api/mobile/bridge/', transport=httpx.MockTransport(host)) as client:
            bridge = ACPBridge(client, output.append)
            bridge.initialized = True
            await bridge.dispatch('session/load', {'sessionId':'conversation'})
            await asyncio.gather(*bridge.watchers)
            assert [event['params']['update']['content']['text'] for event in output] == ['thinking','resumed answer']
    asyncio.run(scenario())


def test_acp_cancel_during_run_creation_is_not_lost():
    from olcr_cli.acp import ACPBridge
    async def scenario():
        created, release = asyncio.Event(), asyncio.Event()
        cancelled = []
        async def host(request):
            if request.url.path.endswith('/runs'):
                created.set()
                await release.wait()
                return httpx.Response(200,json={'id':'late-run'})
            if request.url.path.endswith('/cancel'):
                cancelled.append(True)
                return httpx.Response(200,json={})
            if request.url.path.endswith('/active-run'):
                return httpx.Response(200,json={'run':None})
            return httpx.Response(200,json={'run':{'state':'cancelled'},'events':[]})
        async with httpx.AsyncClient(base_url='http://127.0.0.1/api/mobile/bridge/',transport=httpx.MockTransport(host)) as client:
            bridge=ACPBridge(client,lambda _:None)
            bridge.initialized=True
            bridge.sessions.add('c')
            prompt=asyncio.create_task(bridge.dispatch('session/prompt',{'sessionId':'c','prompt':[{'type':'text','text':'work'}]}))
            await created.wait()
            await bridge.dispatch('session/cancel',{'sessionId':'c'})
            release.set()
            assert await prompt == {'stopReason':'cancelled'}
            assert cancelled == [True]
    asyncio.run(scenario())


def test_happier_adapter_registration_preserves_encrypted_catalog_on_conflict():
    from nacl.secret import SecretBox
    from olcr_api.mobile_daemon import account_settings_box, register_adapter

    class Relay:
        seed = bytes(range(32))
        def __init__(self):
            self.writes = []
        async def _request(self, method, path, **kwargs):
            assert path == '/v2/account/settings'
            if method == 'GET':
                original = {'theme':'light', 'acpCatalogSettingsV1':{'v':2, 'backends':[{'id':'existing'}]}}
                ciphertext = base64.b64encode(bytes(SecretBox(self.seed).encrypt(json.dumps(original).encode()))).decode()
                return {'content':{'t':'encrypted','c':ciphertext}, 'version':len(self.writes)}
            self.writes.append(kwargs['json'])
            return {'success':len(self.writes)>1,'error':'version-mismatch'}

    async def scenario():
        relay = Relay()
        await register_adapter(relay, {'id':'olcr','command':'/private/example/olcr-acp'})
        assert [item['expectedVersion'] for item in relay.writes] == [0,1]
        data = base64.b64decode(relay.writes[-1]['content']['c'])
        assert data[:2] == b'\xa1\x01'
        settings = json.loads(account_settings_box(relay.seed).decrypt(data[2:]))
        assert settings['theme'] == 'light'
        assert [item['id'] for item in settings['acpCatalogSettingsV1']['backends']] == ['existing','olcr']

    asyncio.run(scenario())
