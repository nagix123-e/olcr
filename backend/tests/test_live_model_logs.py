import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.live_model_logs import record_model_log,register_model_log_routes
from olcr_api.model_progress import observe_model_progress,model_progress

class LiveLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'logs.db');self.db.initialize();self.db.create_conversation('test',1,'chat',None)
        self.db.add_message('chat','user','request',1,'turn')
        self.app=FastAPI();register_model_log_routes(self.app,lambda:self.db);self.client=TestClient(self.app)
    def test_progress_is_a_snapshot_without_erasing_lifecycle_or_reasoning(self):
        record_model_log(self.db,'chat',{'invocation_id':'a','state':'request_started','model':'local'})
        record_model_log(self.db,'chat',{'invocation_id':'a','state':'reasoning','text':'received thought'})
        for i in range(1100):
            record_model_log(self.db,'chat',{'invocation_id':'a','state':'worker_progress','event':'ollama_generation_progress','output_chars':i})
        events=self.client.get('/api/conversations/chat/model-logs').json()['events']
        self.assertEqual(3,len(events))
        self.assertEqual('request_started',events[0]['state'])
        self.assertEqual('received thought',events[1]['text'])
        self.assertEqual(1099,events[2]['output_chars'])

    def test_reasoning_appends_per_invocation_and_host_fallbacks_are_separate(self):
        with observe_model_progress(lambda data:record_model_log(self.db,'chat',data)):
            model_progress(state='request_started',model='first',runtime='ollama')
            model_progress(state='reasoning',text='raw ',kind='trace',complete=False)
            model_progress(state='reasoning',text='trace',kind='trace',complete=False)
            model_progress(state='request_failed',model='first',failure='timeout')
            model_progress(state='fallback',model='second',reason='timeout')
            model_progress(state='request_started',model='second',runtime='google')
            model_progress(state='reasoning',text='summary',kind='summary',complete=False)
            model_progress(state='reasoning',text='summary final',kind='summary',snapshot=True,complete=True)
        events=self.client.get('/api/conversations/chat/model-logs').json()['events']
        traces=[e for e in events if e['state']=='reasoning']
        self.assertEqual(['raw trace','summary final'],[e['text'] for e in traces]);self.assertNotEqual(traces[0]['invocation_id'],traces[1]['invocation_id'])
        self.assertIn('fallback',[e['state'] for e in events])
        self.db.add_message('chat','user','next',99999999999,'turn2')
        self.assertEqual([],self.client.get('/api/conversations/chat/model-logs').json()['events'])
    def test_reasoning_is_queryable_before_stream_response_finishes(self):
        received=threading.Event();release=threading.Event();self.addCleanup(release.set)
        @self.app.middleware('http')
        async def observe(request,call_next):
            with observe_model_progress(lambda data:record_model_log(self.db,'chat',data)):
                return await call_next(request)
        @self.app.get('/stream')
        def stream():
            def events():
                model_progress(state='request_started',model='local',runtime='ollama')
                model_progress(state='reasoning',text='arrived now',kind='trace',complete=False)
                received.set();release.wait(5)
                yield 'finished'
            return StreamingResponse(events())
        result=[];worker=threading.Thread(target=lambda:result.append(self.client.get('/stream')));worker.start()
        try:
            self.assertTrue(received.wait(3))
            response=self.client.get('/api/conversations/chat/model-logs')
            self.assertEqual('arrived now',response.json()['events'][-1]['text']);self.assertEqual([],result)
        finally:release.set();worker.join(5)
    def test_mlx_worker_forwards_only_received_thought_channel_and_not_answer(self):
        from olcr_api import mlx_worker
        path=Path(self.tmp.name)/'worker.jsonl'
        with patch.dict('os.environ',{'OLCR_MLX_LIVE_PATH':str(path)}),patch.object(mlx_worker,'_last_live_reasoning',''):
            mlx_worker._live_reasoning('<think>one')
            mlx_worker._live_reasoning('<think>one two</think>final answer',complete=True)
        events=[json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual('one two',''.join(e['text'] for e in events));self.assertNotIn('final answer',path.read_text())

    def test_ollama_nonstream_answer_still_forwards_received_thinking_live(self):
        from olcr_api.ollama import OllamaProvider
        received=[]
        class Response:
            def __iter__(self):
                yield json.dumps({'model':'local','message':{'thinking':'raw '},'done':False}).encode()
                assert received and received[-1]['text']=='raw '
                yield json.dumps({'model':'local','message':{'thinking':'trace','content':'answer'},'done':False}).encode()
                yield json.dumps({'model':'local','message':{},'done':True,'eval_count':3}).encode()
            def close(self):pass
        with patch('olcr_api.ollama.open_provider_response',return_value=Response()) as opened,observe_model_progress(received.append):
            model_progress(state='request_started',model='local',runtime='ollama')
            from olcr_api.ollama import OllamaProvider
            answer=OllamaProvider('http://127.0.0.1:11434').generate([{'role':'user','content':'request'}],'local',think=True)
        self.assertTrue(json.loads(opened.call_args.args[0].data)['stream'])
        self.assertEqual('answer',answer['text']);self.assertEqual('raw trace',answer['reasoning']['text'])

    def test_mlx_parent_forwards_worker_log_before_process_finishes(self):
        import subprocess
        from olcr_api.config import Settings
        from olcr_api.model_provider import RoleAwareModelProvider
        provider=RoleAwareModelProvider(Settings(db_path=str(Path(self.tmp.name)/'settings.db')))
        received=[];arrived=threading.Event()
        def observe(data):
            received.append(data)
            if data.get('text')=='live thought':arrived.set()
        def run(command,**kwargs):
            path=Path(kwargs['env']['OLCR_MLX_LIVE_PATH'])
            path.write_text(json.dumps({'state':'reasoning','text':'live thought','kind':'trace'})+'\n')
            self.assertTrue(arrived.wait(2),'worker log must reach the parent before completion')
            return subprocess.CompletedProcess(command,0,json.dumps({'text':'answer'}),'')
        with observe_model_progress(observe),patch.object(provider,'_mlx_python',return_value=Path('/fake/python')),patch('olcr_api.model_provider.subprocess.run',side_effect=run):
            model_progress(state='request_started',model='MLX model',runtime='mlx')
            result=provider._run_mlx('ornith',{})
        self.assertEqual('answer',result['text']);self.assertTrue(arrived.is_set())
