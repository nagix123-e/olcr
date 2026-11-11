import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

from olcr_api.mcp_runtime import MCPRuntime

# An actual subprocess, not a mock of the transport under test.
SERVER = '''import json, os, sys, time
initialized = False
seen = []
def reply(m, result):
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    method = m.get('method')
    if method == 'initialize':
        reply(m, {'protocolVersion':m['params']['protocolVersion'], 'capabilities':{'tools':{}}, 'serverInfo':{'name':'fixture','version':'1'}})
    elif method == 'notifications/initialized':
        initialized = True
    elif method == 'tools/list':
        assert initialized
        reply(m, {'tools':[{'name':'echo','inputSchema':{'type':'object'}}]})
    elif method == 'tools/call':
        seen.append(m['id'])
        reply(m, {'arguments':m['params']['arguments'], 'ids':seen, 'secret_present':'OLCR_FAKE_SECRET' in os.environ})
'''


class MCPRuntimeTests(unittest.TestCase):
    def runtime(self, program=SERVER, **options):
        rt = MCPRuntime([sys.executable, '-u', '-c', program], ['echo'],
                        shutdown_timeout=.15, **options)
        self.addCleanup(rt.close)
        return rt

    def connect(self, rt):
        self.assertEqual('STARTING', rt.start())
        self.assertEqual('AVAILABLE', rt.initialize()['status'])
        self.assertEqual('READY', rt.state)

    def test_lifecycle_initialized_notification_and_unique_request_ids(self):
        rt = self.runtime()
        rt.start()
        self.assertEqual('BLOCKED', rt.tools_list()['status'])
        self.assertEqual('AVAILABLE', rt.initialize()['status'])
        self.assertEqual('AVAILABLE', rt.tools_list()['status'])
        first = rt.call('echo', {'text':'日本語'})['response']
        second = rt.call('echo')['response']
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual('日本語', first['result']['arguments']['text'])
        self.assertEqual(2, len(set(second['result']['ids'])))
        rt.close()
        self.assertEqual('STOPPED', rt.state)
        self.assertFalse(rt.last_diagnostic['initialized'])

    def test_notification_and_wrong_id_are_not_a_response(self):
        program = SERVER.replace("seen.append(m['id'])", """print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'level':'info'}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','id':-1,'result':{'wrong':True}}), flush=True)
        seen.append(m['id'])""")
        rt = self.runtime(program)
        self.connect(rt)
        result = rt.call('echo', {'expected':True})
        self.assertEqual('AVAILABLE', result['status'])
        self.assertEqual({'expected':True}, result['response']['result']['arguments'])
        self.assertGreater(rt.last_diagnostic['notifications'], 0)

    def test_server_ping_and_unsupported_request_do_not_deadlock_initialize(self):
        prefix = """print(json.dumps({'jsonrpc':'2.0','id':'server-ping','method':'ping'}),flush=True)
        ping = json.loads(sys.stdin.readline())
        assert ping['id']=='server-ping' and ping['result']=={}
        print(json.dumps({'jsonrpc':'2.0','id':'server-sampling','method':'sampling/createMessage'}),flush=True)
        rejected = json.loads(sys.stdin.readline())
        assert rejected['id']=='server-sampling' and rejected['error']['code']==-32601
        """
        program = SERVER.replace("reply(m, {'protocolVersion'", prefix + "reply(m, {'protocolVersion'", 1)
        self.connect(self.runtime(program))

    def test_tool_jsonrpc_error_is_not_success_and_connection_remains_usable(self):
        program = SERVER.replace("seen.append(m['id'])", """if m['params']['arguments'].get('fail'):
            print(json.dumps({'jsonrpc':'2.0','id':m['id'],'error':{'code':-32602,'message':'invalid arguments'}}),flush=True)
            continue
        seen.append(m['id'])""")
        rt = self.runtime(program)
        self.connect(rt)
        result = rt.call('echo', {'fail':True})
        self.assertEqual('FAILED', result['status'])
        self.assertEqual(-32602, result['response']['error']['code'])
        self.assertEqual('AVAILABLE', rt.call('echo')['status'])

    def test_timeout_reaps_reader_and_child_before_explicit_reconnect(self):
        program = SERVER.replace("seen.append(m['id'])", "time.sleep(1)\n        seen.append(m['id'])")
        rt = self.runtime(program, timeout=.05)
        self.connect(rt)
        child, reader = rt.process, rt._stdout_thread
        result = rt.call('echo')
        self.assertEqual('REQUEST_TIMEOUT', result['error_code'])
        self.assertEqual('FAILED', rt.state)
        self.assertIsNotNone(child.poll())
        self.assertFalse(reader.is_alive())
        self.assertIsNone(rt.process)
        self.assertEqual('FAILED', rt.call('echo')['status'])
        rt.command = [sys.executable, '-u', '-c', SERVER]
        self.connect(rt)
        self.assertEqual('AVAILABLE', rt.call('echo')['status'])

    def test_initialize_timeout_retains_actual_cause(self):
        rt = self.runtime('import time; time.sleep(20)', startup_timeout=.03)
        self.assertEqual('REQUEST_TIMEOUT', rt.initialize()['error_code'])
        self.assertEqual('FAILED', rt.state)
        self.assertIsNone(rt.process)

    def test_unsupported_negotiated_version_closes_connection(self):
        program = SERVER.replace("m['params']['protocolVersion']", "'2099-01-01'")
        rt = self.runtime(program)
        result = rt.initialize()
        self.assertEqual('UNSUPPORTED_PROTOCOL_VERSION', result['error_code'])
        self.assertIsNone(rt.process)

    def test_invalid_initialize_result_does_not_set_ready(self):
        rt = self.runtime(SERVER.replace("'capabilities':{'tools':{}}", "'bad_capabilities':{}"))
        self.assertEqual('INVALID_INITIALIZE_RESPONSE', rt.initialize()['error_code'])
        self.assertEqual('FAILED', rt.state)

    def test_malformed_stdout_is_a_transport_error(self):
        rt = self.runtime("print('not json',flush=True); import time; time.sleep(5)")
        self.assertEqual('INVALID_MESSAGE', rt.initialize()['error_code'])
        self.assertEqual('FAILED', rt.state)

    def test_missing_command_and_exited_child_report_failure(self):
        rt = MCPRuntime(['/no-such-olcr-mcp'], [])
        self.addCleanup(rt.close)
        self.assertEqual('PROCESS_START_FAILED', rt.initialize()['error_code'])
        rt = self.runtime('pass')
        self.assertEqual('PROCESS_EXITED', rt.initialize()['error_code'])

    def test_environment_and_tool_allowlist_are_enforced_at_process_boundary(self):
        with patch.dict(os.environ, {'OLCR_FAKE_SECRET':'never inherit'}):
            rt = self.runtime()
            self.connect(rt)
            self.assertFalse(rt.call('echo')['response']['result']['secret_present'])
            self.assertEqual('BLOCKED', rt.call('not-allowed')['status'])
            self.assertNotIn('never inherit', json.dumps(rt.last_diagnostic))

    def test_child_stderr_is_drained_without_retaining_output(self):
        rt = self.runtime("import sys; sys.stderr.write('x'*300000); sys.stderr.flush()\n" + SERVER)
        self.connect(rt)
        self.assertEqual('AVAILABLE', rt.tools_list()['status'])
        self.assertNotIn('stderr', rt.last_diagnostic)

    def test_paginated_tools_are_all_discovered(self):
        program = SERVER.replace("reply(m, {'tools':[{'name':'echo','inputSchema':{'type':'object'}}]})", """reply(m, {'tools':[{'name':'last'}]} if m['params'].get('cursor') else {'tools':[{'name':'first'}], 'nextCursor':'page2'})""")
        rt = self.runtime(program)
        self.connect(rt)
        self.assertEqual(['first','last'], [x['name'] for x in rt.tools_list()['response']['result']['tools']])

    def test_repeating_pagination_cursor_is_not_an_infinite_loop(self):
        program = SERVER.replace("reply(m, {'tools':[{'name':'echo','inputSchema':{'type':'object'}}]})", "reply(m, {'tools':[], 'nextCursor':'again'})")
        rt = self.runtime(program)
        self.connect(rt)
        self.assertEqual('INVALID_TOOLS_PAGINATION', rt.tools_list()['error_code'])

    def test_pagination_cannot_reset_the_request_time_budget(self):
        program = SERVER.replace("reply(m, {'tools':[{'name':'echo','inputSchema':{'type':'object'}}]})", "time.sleep(.08); reply(m, {'tools':[], 'nextCursor':str(m['id'])})")
        rt = self.runtime(program, timeout=.12, startup_timeout=2)
        self.connect(rt)
        self.assertEqual('REQUEST_TIMEOUT', rt.tools_list()['error_code'])
        self.assertIsNone(rt.process)

    def test_concurrent_callers_cannot_consume_each_others_responses(self):
        rt = self.runtime()
        self.connect(rt)
        results = {}
        def call(i):
            results[i] = rt.call('echo', {'caller':i})['response']['result']['arguments']['caller']
        threads = [threading.Thread(target=call, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual({i:i for i in range(8)}, results)

if __name__ == '__main__': unittest.main()
