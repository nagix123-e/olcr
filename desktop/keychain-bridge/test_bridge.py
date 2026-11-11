"""macOS integration regression using only a disposable Keychain and fixture secrets.

Run with python3 desktop/keychain-bridge/test_bridge.py. Never accesses the default
Keychain. The test helpers share the production designated requirement, but their
Keychain/lock paths are available only behind KEYCHAIN_BRIDGE_TEST at compile time.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = 'OLCR Local Debug Code Signing'
REQUIREMENT = 'identifier "local.olcr.keychain-bridge" and certificate root = H"969372f8911d3a2a85ea3339df864961a663f683"'


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=90, **kwargs)


def checked(args, **kwargs):
    result = run(args, **kwargs)
    if result.returncode:
        # Never print stdout: a helper's stdout may contain credentials.
        raise AssertionError(f'{Path(str(args[0])).name} failed ({result.returncode}): {result.stderr.decode()[-1500:]}')
    return result


with tempfile.TemporaryDirectory(prefix='olcr-keychain-regression-', dir='/private/tmp') as temp:
    root = Path(temp)
    keychain = root / 'fixture.keychain-db'
    env = dict(os.environ, OLCR_TEST_KEYCHAIN=str(keychain))
    setup = root / 'keychain.swift'
    setup.write_text('''import Foundation
import Security
SecKeychainSetUserInteractionAllowed(false)
let path = CommandLine.arguments[2]
var keychain: SecKeychain?
var status: OSStatus
switch CommandLine.arguments[1] {
case "create":
 let password="fixture-only-password"
 status=password.withCString { SecKeychainCreate(path, UInt32(password.utf8.count), $0, false, nil, &keychain) }
case "lock":
 status=SecKeychainOpen(path, &keychain)
 if status == errSecSuccess { status=SecKeychainLock(keychain) }
case "delete":
 status=SecKeychainOpen(path, &keychain)
 if status == errSecSuccess, let keychain { status=SecKeychainDelete(keychain) }
default: status=errSecParam
}
if status != errSecSuccess { fputs("fixture_status=\\(status)\\n",stderr); exit(1) }
''')
    setup_binary = root / 'keychain-fixture'
    checked(['/usr/bin/swiftc', '-module-cache-path', str(root/'modules'), str(setup), '-o', str(setup_binary)])
    source = (ROOT/'desktop/keychain-bridge/main.swift').read_text()
    helpers = []
    for index in range(2):
        # Different bytes exercise an actual helper update, not a second launch
        # of the same executable. Both use the unchanged signed identity.
        version_source = root / f'bridge{index}.swift'
        version_source.write_text(source + f'\nfputs("TEST_VERSION={index}\\n", stderr)\n')
        binary = root / f'bridge{index}'
        checked(['/usr/bin/swiftc', '-D', 'KEYCHAIN_BRIDGE_TEST', '-module-cache-path', str(root/'modules'), '-O', str(version_source), '-o', str(binary)])
        checked(['/usr/bin/codesign', '--force', '--sign', IDENTITY, '--timestamp=none', '--identifier', 'local.olcr.keychain-bridge', '--requirements', '=designated => '+REQUIREMENT, str(binary)])
        helpers.append(binary)
    checked([str(setup_binary), 'create', str(keychain)])
    try:
        keys = ['web:brave','web:tavily','OLCR_BRAIN_OPENAI_OAUTH']
        def bridge(index, change=None, flags=None):
            args = [str(helpers[index]), '--allowlist-json='+json.dumps(keys), '--interaction=forbid']
            if flags: args.extend(flags)
            if change is not None: args.append('--update-stdin')
            return run(args, input=json.dumps(change).encode() if change is not None else None, env=env)
        result = bridge(0, {'key':'web:brave','value':'fixture-brave'})
        assert result.returncode == 0, result.stderr.decode()
        assert b'LEGACY_BUNDLE_MIGRATED=true' in result.stderr
        for index in [0,1,1,0]:
            result = bridge(index)
            assert result.returncode == 0, result.stderr.decode()
            assert json.loads(result.stdout)['web:brave'] == 'fixture-brave'
            assert b'LEGACY_BUNDLE_MIGRATED=false' in result.stderr
        changes=[{'key':'web:tavily','value':'fixture-tavily'}, {'key':'OLCR_BRAIN_OPENAI_OAUTH','value':'fixture-oauth'}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda pair:bridge(*pair),enumerate(changes)))
        assert all(result.returncode == 0 for result in results)
        result=bridge(1); assert set(json.loads(result.stdout)) == set(keys)
        assert b'LEGACY_BUNDLE_MIGRATED=false' in result.stderr
        result=bridge(1, {'key':'web:brave','value':None}); assert result.returncode == 0
        assert set(json.loads(result.stdout)) == set(keys[1:])
        result=bridge(0, {'key':'unknown','value':'rejected'}); assert result.returncode == 1
        # A different signed identity must be refused without showing a dialog.
        stranger=root/'stranger'; stranger.write_bytes(helpers[0].read_bytes()); stranger.chmod(0o700)
        checked(['/usr/bin/codesign','--force','--sign',IDENTITY,'--timestamp=none','--identifier','local.olcr.untrusted-test',str(stranger)])
        result=run([str(stranger),'--allowlist-json='+json.dumps(keys),'--interaction=forbid'],env=env)
        assert result.returncode == 2 and not result.stdout, f'expected authorization refusal: {result.returncode}'
        checked([str(setup_binary),'lock',str(keychain)])
        result=bridge(1)
        assert result.returncode == 2 and not result.stdout, f'expected locked refusal: {result.returncode}'
        print('PASS: same signature across two helper builds; restart reads; concurrent saves; migration marker; deletion; invalid updates; unauthorized identity and locked Keychain fail without interaction. No fixture values printed.')
    finally:
        checked([str(setup_binary),'delete',str(keychain)])
