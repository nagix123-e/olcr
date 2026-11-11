import json
import sys
import unittest
from unittest.mock import patch

from olcr_api.mcp_connections import MCPConnections
from olcr_api.mcp_manifest import MCP_MANIFEST
from olcr_api.node_mcp_runtime import launch_command, managed_browser_command
from olcr_api.serena_runtime import launch_command as serena_command

SERVER = '''import json,sys
for line in sys.stdin:
    m=json.loads(line)
    if m.get('method')=='initialize':
        result={'protocolVersion':m['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif m.get('method')=='tools/list':
        result={'tools':[{'name':n} for n in ['search_items_in_registries','list_items_in_registries','view_items_in_registries']]}
    elif m.get('method')=='notifications/initialized': continue
    else: raise RuntimeError('probe must not invoke tools')
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


class MCPConnectionTests(unittest.TestCase):
    def setUp(self):
        self.service = MCPConnections()

    def configure(self, program=SERVER):
        definition = {**MCP_MANIFEST['shadcn'], 'startup_timeout':1, 'tool_timeout':1, 'shutdown_timeout':.1}
        return patch.object(self.service, '_configuration', return_value=(
            definition, [sys.executable, '-u', '-c', program], 'coding', True, 'READY'))

    def test_inventory_never_starts_a_server_or_confuses_unchecked_with_failed(self):
        with self.configure(), patch('olcr_api.mcp_connections.MCPRuntime') as runtime:
            rows = self.service.inventory()
        runtime.assert_not_called()
        self.assertTrue(all(row['connection_status']=='NOT_CHECKED' for row in rows))

    def test_probe_checks_handshake_and_discovery_without_calling_tools(self):
        with self.configure():
            result = self.service.check('shadcn')
            self.assertEqual('CONNECTED', result['connection_status'])
            self.assertEqual(3, result['tool_count'])
            self.assertTrue(result['checked_at'])
            self.assertEqual('CONNECTED', next(row for row in self.service.inventory() if row['server']=='shadcn')['connection_status'])
        self.assertNotIn('command', json.dumps(result))

    def test_valid_handshake_does_not_hide_tools_list_failure(self):
        program = SERVER.replace("result={'tools':[{'name':n} for n in ['search_items_in_registries','list_items_in_registries','view_items_in_registries']]}",
                                 "print(json.dumps({'jsonrpc':'2.0','id':m['id'],'error':{'code':-32603,'message':'unavailable'}}),flush=True); continue")
        with self.configure(program):
            result = self.service.check('shadcn')
        self.assertEqual('FAILED', result['connection_status'])
        self.assertEqual('JSON_RPC_ERROR', result['reason'])

    def test_exited_child_can_be_checked_again_and_success_replaces_failure(self):
        with self.configure('pass'):
            self.assertEqual('FAILED', self.service.check('shadcn')['connection_status'])
        with self.configure():
            self.assertEqual('CONNECTED', self.service.check('shadcn')['connection_status'])

    def test_coding_policy_does_not_disable_registered_connection_checks(self):
        with patch('olcr_api.mcp_connections.serena_command', return_value=[sys.executable, '-c', 'pass']), \
             patch('olcr_api.mcp_connections.resolve_mcp_resources', return_value={'command':[sys.executable], 'reason':'READY'}):
            rows = {row['server']:row for row in self.service.inventory()}
        for name in ('serena', 'devdocs'):
            self.assertTrue(rows[name]['can_check'], rows[name])
            self.assertEqual('NOT_CHECKED', rows[name]['connection_status'])
            self.assertFalse(rows[name]['coding_enabled'])

    def test_missing_serena_can_be_prepared_without_host_python_fallback(self):
        definition = {**MCP_MANIFEST['serena'], 'allowed_tools':MCP_MANIFEST['shadcn']['allowed_tools']}
        with patch.object(self.service, '_configuration', side_effect=[
                (definition, None, 'coding', True, 'PYTHON_RUNTIME_MISSING'),
                (definition, [sys.executable, '-u', '-c', SERVER], 'coding', True, 'READY'),
                *[(definition, [sys.executable, '-u', '-c', SERVER], 'coding', True, 'READY')]*5]), \
             patch('olcr_api.mcp_connections.components.ensure') as prepare:
            result = self.service.check('serena')
        self.assertEqual('CONNECTED', result['connection_status'])
        prepare.assert_called_once_with('serena')

    def test_preparation_failure_is_visible_and_can_retry(self):
        from olcr_api.optional_runtimes import SetupError
        definition = MCP_MANIFEST['serena']
        with patch.object(self.service, '_configuration', return_value=(definition,None,'coding',True,'PYTHON_RUNTIME_MISSING')), \
             patch('olcr_api.mcp_connections.components.ensure', side_effect=SetupError('offline')):
            result = self.service.check('serena')
        self.assertEqual('FAILED', result['connection_status'])
        self.assertEqual('RUNTIME_SETUP_FAILED', result['reason'])
        self.assertTrue(result['can_check'])

    def test_missing_resources_are_distinct_from_disabled_and_not_checked(self):
        definition = MCP_MANIFEST['animejs']
        with patch.object(self.service, '_configuration', return_value=(definition, None, 'coding', True, 'NODE_RUNTIME_MISSING')), patch('olcr_api.mcp_connections.components.ensure'):
            result = self.service.check('animejs')
        self.assertEqual('FAILED', result['connection_status'])
        self.assertEqual('NODE_RUNTIME_MISSING', result['reason'])

    def test_unknown_server_cannot_be_used_as_an_arbitrary_command(self):
        with self.assertRaises(KeyError):
            self.service.check('/bin/sh')

    def test_connection_is_failed_when_policy_tools_are_not_advertised(self):
        with self.configure(SERVER.replace("'view_items_in_registries'", "'other'")):
            result = self.service.check('shadcn')
        self.assertEqual('FAILED', result['connection_status'])
        self.assertEqual('REQUIRED_TOOL_MISSING', result['reason'])

    def test_resource_change_invalidates_prior_connection_check(self):
        with self.configure():
            self.service.check('shadcn')
        with self.configure(SERVER+'\n'):
            row = next(row for row in self.service.inventory() if row['server']=='shadcn')
        self.assertEqual('NOT_CHECKED', row['connection_status'])
        self.assertIsNone(row['checked_at'])


class BundledMCPConnectionTests(unittest.TestCase):
    def test_actual_bundled_servers_initialize_and_list_tools(self):
        commands = {'serena': serena_command(),
                    'devdocs': launch_command('devdocs', for_connection=True), 'animejs': launch_command('animejs'), 'shadcn': launch_command('shadcn'),
                    'playwright': managed_browser_command()}
        if not all(commands.values()):
            self.skipTest('Prepared immutable Node MCP runtime required')
        service = MCPConnections()
        for name in commands:
            with self.subTest(server=name):
                row = service.check(name)
                self.assertEqual('CONNECTED', row['connection_status'], row)
                self.assertGreater(row['tool_count'], 0)
                if name == 'playwright':
                    self.assertEqual('managed_browser', row['scope'])
                    self.assertFalse(row['coding_enabled'])

if __name__ == '__main__': unittest.main()
