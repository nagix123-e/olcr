#!/usr/bin/env python3
"""Prepare pinned offline execution resources after npm ci in agent-runtime."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.request import urlopen
from prepare_node_mcp_runtime import write_license_inventory

ROOT=Path(__file__).resolve().parents[1]
root=ROOT/'agent-runtime'
node=root/'node_modules/node/bin/node'
identity=json.loads(subprocess.check_output([str(node),'-p','JSON.stringify({version:process.version,platform:process.platform,arch:process.arch})'],text=True))
if identity['version']!='v24.18.0': raise SystemExit('AGENT_NODE_VERSION_MISMATCH')
package=json.loads((root/'node_modules/@librechat/agents/package.json').read_text())
if package['version']!='4.0.4': raise SystemExit('AGENT_PACKAGE_VERSION_MISMATCH')
# Node's npm binary package lacks the distribution notice; retain the exact upstream notice.
license_path=root/'node/LICENSE';license_path.parent.mkdir(exist_ok=True)
if not license_path.is_file():
    with urlopen('https://raw.githubusercontent.com/nodejs/node/v24.18.0/LICENSE',timeout=30) as response: license_path.write_bytes(response.read())
# Reuse the existing bundled-runtime inventory generator without copying dependencies.
with tempfile.TemporaryDirectory(prefix='olcr-agent-inventory-') as directory:
    stage=Path(directory);(stage/'node').symlink_to(root/'node',target_is_directory=True);(stage/'node_modules').symlink_to(root/'node_modules',target_is_directory=True)
    count=write_license_inventory(stage)
    inventory=json.loads((stage/'licenses/license-inventory.json').read_text());inventory['runtime']='custom-agents'
    (root/'licenses').mkdir(exist_ok=True);(root/'licenses/license-inventory.json').write_text(json.dumps(inventory,indent=2,sort_keys=True)+'\n')
manifest={'immutable':True,'engine':'@librechat/agents','engine_version':package['version'],'node':identity,'entrypoint':'runner.mjs','node_entrypoint':'node_modules/node/bin/node','package_lock_sha256':hashlib.sha256((root/'package-lock.json').read_bytes()).hexdigest(),'runner_sha256':hashlib.sha256((root/'runner.mjs').read_bytes()).hexdigest(),'license_inventory':'licenses/license-inventory.json','licensed_component_count':count}
(root/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
print(f'Prepared LibreChat {package["version"]}, Node {identity["version"]}, {count} package/license records')
