import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import olcr_api.app as api


class MCPConnectionAPITests(unittest.TestCase):
    def test_serena_environment_can_be_prepared_from_settings(self):
        with patch.object(api.optional_components, 'prepare', return_value={'name':'serena','state':'installing'}):
            response = TestClient(api.app).post('/api/components/serena/prepare')
        self.assertEqual(200, response.status_code)
        self.assertEqual('serena', response.json()['name'])

    def test_inventory_has_no_connection_side_effect(self):
        with patch.object(api.mcp_connections, 'inventory', return_value=[{'server':'shadcn', 'connection_status':'NOT_CHECKED'}]), \
             patch.object(api.mcp_connections, 'check') as check:
            response = TestClient(api.app).get('/api/mcp/connections')
        self.assertEqual(200, response.status_code)
        self.assertEqual('NOT_CHECKED', response.json()['servers'][0]['connection_status'])
        check.assert_not_called()

    def test_explicit_check_dispatches_without_a_model_or_task(self):
        with patch.object(api.mcp_connections, 'check', return_value={'server':'shadcn', 'connection_status':'CONNECTED'}) as check:
            response = TestClient(api.app).post('/api/mcp/connections/shadcn/check')
        self.assertEqual(200, response.status_code)
        self.assertEqual('CONNECTED', response.json()['connection_status'])
        check.assert_called_once_with('shadcn')

    def test_unknown_server_is_a_404(self):
        response = TestClient(api.app).post('/api/mcp/connections/not-registered/check')
        self.assertEqual(404, response.status_code)

    def test_telemetry_does_not_infer_unavailable_from_no_task_usage(self):
        text = api._append_mcp_telemetry('done', {})
        self.assertIn('SHADCN_MCP_AVAILABLE=NOT_CHECKED', text)
        self.assertIn('MCP_AVAILABLE=shadcn:NOT_CHECKED', text)
        self.assertIn('SHADCN_MCP_USED=NO', text)

    def test_failed_handshake_is_not_available(self):
        text = api._append_mcp_telemetry('done', {'mcp_evidence':[
            {'mcp_name':'shadcn', 'tool_name':'initialize', 'status':'FAILED'}]})
        self.assertIn('SHADCN_MCP_AVAILABLE=NO', text)
        self.assertIn('MCP_AVAILABLE=shadcn:NO', text)

    def test_tool_failure_does_not_erase_successful_connection(self):
        text = api._append_mcp_telemetry('done', {'mcp_evidence':[
            {'mcp_name':'shadcn', 'tool_name':'initialize', 'status':'PASS'},
            {'mcp_name':'shadcn', 'tool_name':'tools/list', 'status':'PASS'},
            {'mcp_name':'shadcn', 'tool_name':'search_items_in_registries', 'status':'FAILED'}]})
        self.assertIn('SHADCN_MCP_AVAILABLE=YES', text)
        self.assertIn('MCP_AVAILABLE=shadcn:YES', text)
        self.assertIn('SHADCN_MCP_USED=NO', text)

    def test_new_handshake_cannot_reuse_old_discovery_or_tool_usage(self):
        text = api._append_mcp_telemetry('done', {'mcp_evidence':[
            {'mcp_name':'shadcn', 'tool_name':'initialize', 'status':'PASS'},
            {'mcp_name':'shadcn', 'tool_name':'tools/list', 'status':'PASS'},
            {'mcp_name':'shadcn', 'tool_name':'search_items_in_registries', 'status':'PASS'},
            {'mcp_name':'shadcn', 'tool_name':'initialize', 'status':'PASS'}]})
        self.assertIn('SHADCN_MCP_AVAILABLE=NOT_CHECKED', text)
        self.assertIn('SHADCN_MCP_USED=YES', text)
