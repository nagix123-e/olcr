import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from olcr_api.apple_mail import AppleMailConnector

class AppleMailPermissionTests(unittest.TestCase):
 def test_status_distinguishes_denied_not_running_and_probe_errors(self):
  for permission,code,expected in [('not_determined',-1744,'permission_required'),('denied',-1743,'permission_denied'),('not_running',-600,'not_running'),('error',-50,'permission_error')]:
   with self.subTest(permission=permission):
    connector=AppleMailConnector(runner=lambda request:{'available':True,'authorized':False,'permission':permission,'automation_status':code})
    self.assertEqual(connector.status()['state'],expected)
 def test_authorized_and_unavailable_keep_existing_contract(self):
  self.assertEqual(AppleMailConnector(runner=lambda r:{'available':True,'authorized':True,'permission':'authorized'}).status()['state'],'connected')
  self.assertEqual(AppleMailConnector(runner=lambda r:{'available':False,'authorized':False,'permission':'unavailable'}).status()['state'],'unavailable')

@unittest.skipUnless(sys.platform=='darwin' and shutil.which('xcrun'),'macOS Swift compiler required')
class NativeMailPermissionTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory()
  source=Path(__file__).resolve().parents[2]/'desktop/src-tauri/macos-mail-helper.swift'
  prefix=source.read_text().split('\ndo {\n    guard let line',1)[0]
  harness='''
let scenario=CommandLine.arguments[1]
do {
 if scenario == "request" {
  var requested:[Bool]=[]
  let result=try requestMailAuthorization(available:true,checker:{ ask in requested.append(ask);return noErr })
  output(["requested":requested,"authorized":result["authorized"] ?? false])
 } else if scenario == "denied" {
  _ = try requestMailAuthorization(available:true,checker:{ _ in OSStatus(-1743) })
 } else if scenario == "mapping" {
  output(["states":[OSStatus(0),OSStatus(-1744),OSStatus(-1743),OSStatus(-600),OSStatus(-50)].map { automationStatus(available:true,permission:$0)["permission"] as? String ?? "" }])
 } else if scenario == "unavailable" {
  _ = try requestMailAuthorization(available:false,checker:{_ in fatalError("must not request unavailable target")})
 }
} catch let error as Failure { output(["error":error.code]) } catch { output(["error":"unexpected"]) }
'''
  main=Path(cls.tmp.name)/'main.swift';main.write_text(prefix+harness)
  cls.binary=Path(cls.tmp.name)/'mail-tests'
  result=subprocess.run(['xcrun','swiftc','-module-cache-path','/tmp/olcr-mail-test-module-cache','-framework','AppKit','-framework','Carbon',str(main),'-o',str(cls.binary)],capture_output=True,text=True)
  if result.returncode:raise AssertionError('Native permission harness failed to compile:\n'+result.stderr)
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 def native(self,name):return json.loads(subprocess.check_output([str(self.binary),name],text=True))
 def test_connect_explicitly_requests_user_consent(self):
  self.assertEqual(self.native('request'),{'requested':[True],'authorized':True})
 def test_denial_and_unavailable_are_specific(self):
  self.assertEqual(self.native('denied')['error'],'APPLE_MAIL_AUTOMATION_DENIED')
  self.assertEqual(self.native('unavailable')['error'],'APPLE_MAIL_UNAVAILABLE')
 def test_status_mapping_keeps_os_failure_distinct(self):
  self.assertEqual(self.native('mapping')['states'],['authorized','not_determined','denied','not_running','error'])
