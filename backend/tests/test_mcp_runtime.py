import os, sys, tempfile, textwrap, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from olcr_api.mcp_runtime import MCPRuntime

class MCPRuntimeTests(unittest.TestCase):
    def test_lifecycle_and_initialize_gate(self):
        rt=MCPRuntime([sys.executable,"-u","-c","import sys; [sys.stdout.write(l) or sys.stdout.flush() for l in sys.stdin]"],["x"],env_allowlist=[])
        self.assertEqual("STARTING",rt.start()); self.assertEqual("BLOCKED",rt.tools_list()["status"])
        rt.close(); self.assertEqual("STOPPED",rt.state)
    def test_ready_requires_a_valid_initialize_response(self):
        program="""import json, sys
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'protocolVersion':'2024-11-05'}}), flush=True)
    elif message.get('method') == 'tools/list':
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'tools':[]}}), flush=True)
"""
        rt=MCPRuntime([sys.executable,"-u","-c",program],["x"],env_allowlist=[])
        self.assertEqual("STARTING",rt.start())
        self.assertEqual("AVAILABLE",rt.initialize()["status"])
        self.assertEqual("READY",rt.state)
        self.assertEqual("AVAILABLE",rt.tools_list()["status"])
        rt.close()
    def test_unknown_tool_blocked(self):
        rt=MCPRuntime([],['known']); self.assertEqual("BLOCKED",rt.call('unknown')["status"])
    def test_environment_is_allowlisted(self):
        os.environ["OLCR_FAKE_SECRET"]="secret"
        rt=MCPRuntime([sys.executable,"-c","import time; time.sleep(1)"],[],env_allowlist=[])
        rt.start(); self.assertIsNotNone(rt.process); rt.close()
        self.assertNotIn("OLCR_FAKE_SECRET", rt.last_diagnostic.get("environment", {}))

if __name__ == "__main__": unittest.main()
