import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from olcr_api.db import Database


class BrowserFormTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'forms.db');self.db.initialize()
        self.settings=SimpleNamespace(permission_level='automatic',external_access_enabled=True)
        self.identity={'session_id':'session-one','process_id':123,'url':'https://forms.example/contact','origin':'https://forms.example','document_id':100.0}
        self.field={'selector':'#subject','label':'Subject','tag':'input','type':'text'}
        self.manager=Mock();self.manager.list.return_value=[{'session_id':'session-one','url':self.identity['url']}]
        self.manager.inspect_public_form.return_value={'status':'AVAILABLE',**self.identity,'fields':[self.field]}
        from olcr_api.browser_forms import BrowserForms
        self.forms=BrowserForms(self.db,self.manager,lambda:self.settings)

    def prepare(self):
        return self.forms.prepare('session-one',{'Subject':'Hello'})

    def test_prepare_is_read_only_and_confirmation_is_exact_single_use(self):
        review=self.prepare();self.manager.fill_public_form.assert_not_called()
        self.assertEqual('Hello',review['fields'][0]['value']);self.assertFalse(review['submitted'])
        changed={**review,'fields':[{**review['fields'][0],'value':'Changed'}]}
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,changed)
        self.manager.fill_public_form.return_value={'status':'AVAILABLE','filled':1,'submitted':False}
        result=self.forms.confirm(review['id'],True,review)
        self.assertFalse(result['submitted']);self.assertEqual('applied',result['status'])
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        self.assertEqual(1,self.manager.fill_public_form.call_count)

    def test_revocation_expiry_rejection_and_changed_page_prevent_fill(self):
        review=self.prepare();self.settings.external_access_enabled=False
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        self.manager.fill_public_form.assert_not_called();self.settings.external_access_enabled=True
        self.manager.inspect_public_form.return_value={**self.manager.inspect_public_form.return_value,'url':'https://forms.example/other'}
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        self.manager.fill_public_form.assert_not_called()
        self.manager.inspect_public_form.return_value.update(self.identity)
        review=self.prepare()
        with self.db.connect() as con:con.execute('UPDATE browser_form_reviews SET expires_at=0')
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        rejected=self.prepare();self.assertEqual('denied',self.forms.confirm(rejected['id'],False,rejected)['status'])
        self.manager.fill_public_form.assert_not_called()

    def test_restarted_browser_or_changed_field_invalidates_review(self):
        for change in ({'process_id':124},{'document_id':101.0},{'fields':[{**self.field,'type':'password'}]}):
            self.manager.inspect_public_form.return_value={'status':'AVAILABLE',**self.identity,'fields':[self.field]}
            review=self.prepare();self.manager.inspect_public_form.return_value.update(change)
            with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        self.manager.fill_public_form.assert_not_called()

    def test_hidden_unlabelled_or_sensitive_fields_and_arbitrary_keys_are_rejected(self):
        for label in ['Password','Card number','認証コード','Email']:
            with self.assertRaises(ValueError):self.forms.prepare('session-one',{label:'secret'})
        with self.assertRaises(ValueError):self.forms.prepare('session-one',{'Subject':'x'*2001})
        with self.assertRaises(ValueError):self.forms.prepare('session-one',{'Subject':'4111111111111111'})
        self.manager.fill_public_form.assert_not_called()

    def test_confirmation_failure_is_consumed_and_cannot_retry_unknown_partial_write(self):
        review=self.prepare();self.manager.fill_public_form.return_value={'status':'BLOCKED','error':'FIELD_CHANGED'}
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        with self.assertRaises(ValueError):self.forms.confirm(review['id'],True,review)
        self.assertEqual(1,self.manager.fill_public_form.call_count)

class ManagedFormBoundaryTests(unittest.TestCase):
    def make_manager(self):
        import threading,time
        from olcr_api.browser_network import BrowserSessionManager,ManagedBrowserSession
        manager=BrowserSessionManager(lambda:['unused']);self.addCleanup(manager.close_all)
        session=ManagedBrowserSession(['unused'])
        process=Mock(pid=123);process.poll.return_value=None
        runtime=Mock(state='READY',process=process);session.runtime=runtime
        identity={'session_id':'s','process_id':123,'url':'https://forms.example/contact','origin':'https://forms.example','document_id':100}
        fields=[{'selector':'#subject','label':'Subject','tag':'input','type':'text'}]
        def response(value):return {'status':'AVAILABLE','response':{'result':{'content':[{'type':'text','text':json.dumps(json.dumps(value))}]}}}
        import json
        runtime.call.return_value=response({**identity,'fields':fields})
        manager._sessions['s']={'session':session,'created_at':time.time(),'last_activity':time.time(),'last_activity_monotonic':time.monotonic(),'url':identity['url'],'allowed_local_origin':None}
        return manager,session,runtime,identity,fields,response

    def test_manager_requires_same_owned_live_process_and_fresh_exact_url(self):
        manager,session,runtime,identity,fields,response=self.make_manager()
        self.assertEqual('AVAILABLE',manager.inspect_public_form('s')['status'])
        runtime.call.return_value=response({**identity,'url':'https://forms.example/other','fields':fields})
        self.assertEqual('BLOCKED',manager.fill_public_form('s',identity,[{**fields[0],'value':'Hello'}])['status'])
        runtime.process.poll.return_value=0
        self.assertEqual('BLOCKED',manager.inspect_public_form('s')['status'])
        self.assertTrue(all(call.args[0]=='browser_evaluate' for call in runtime.call.call_args_list))

    def test_no_public_form_apply_in_loopback_e2e_session(self):
        manager,session,runtime,identity,fields,response=self.make_manager()
        manager._sessions['s']['allowed_local_origin']='http://localhost:1234'
        self.assertEqual('BLOCKED',manager.inspect_public_form('s')['status'])
        runtime.call.assert_not_called()

class FixedFormScriptTests(unittest.TestCase):
    def evaluate(self,html,fields=None):
        import json,shutil,subprocess
        from olcr_api.browser_forms import PUBLIC_FORM_SCAN,PUBLIC_FORM_SET
        root=Path(__file__).resolve().parents[2]
        if not shutil.which('node') or not (root/'frontend/node_modules/jsdom').is_dir():self.skipTest('local jsdom fixture runtime unavailable')
        code=r'''const {JSDOM}=require('./frontend/node_modules/jsdom');let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{const cfg=JSON.parse(input);const dom=new JSDOM(cfg.html,{url:'https://forms.example/contact',runScripts:'outside-only'});const w=dom.window;w.HTMLElement.prototype.getBoundingClientRect=function(){return {width:100,height:20}};let events=0;for(const name of ['input','change','keydown','submit'])w.document.addEventListener(name,()=>events++);const evidence=JSON.parse(w.eval('('+cfg.scan+')()'));let result=null;if(cfg.fields){const fields=cfg.fields.map(f=>({...evidence.fields.find(x=>x.label===f.label),...f}));const identity={url:w.location.href,origin:w.location.origin,document_id:w.performance.timeOrigin,fields};result=JSON.parse(w.eval('('+cfg.setter.replace('__OLCR_CONFIG__',JSON.stringify(identity))+')()'));}process.stdout.write(JSON.stringify({evidence,result,events,values:[...w.document.querySelectorAll('input,textarea')].map(x=>x.value)}));dom.window.close();});'''
        result=subprocess.run(['node','-e',code],cwd=root,input=json.dumps({'html':html,'scan':PUBLIC_FORM_SCAN,'setter':PUBLIC_FORM_SET,'fields':fields}),text=True,capture_output=True,timeout=10,check=True)
        return json.loads(result.stdout)

    def test_scan_omits_password_payment_duplicate_hidden_unlabelled_and_login_forms(self):
        html='<label>Subject<input name="subject"></label><input aria-label="Hidden" style="display:none"><input name="unlabelled"><label>Card number<input autocomplete="cc-number"></label><label>Expiry date<input type="date" autocomplete="cc-exp"></label><label>Note<textarea></textarea></label><label>Duplicate<input></label><label>Duplicate<input></label><form><label>Email<input type="email"></label><label>Password<input type="password"></label></form>'
        value=self.evaluate(html)
        self.assertEqual(['Subject','Note'],[f['label'] for f in value['evidence']['fields']])
        self.assertNotIn('value',value['evidence']['fields'][0])

    def test_setter_fills_only_reviewed_values_and_emits_no_submission_or_input_events(self):
        html='<form><label>Subject<input name="subject"></label><label>Note<textarea>Original</textarea></label><button>Send</button></form>'
        value=self.evaluate(html,[{'label':'Subject','value':'Hello'}])
        self.assertEqual(['Hello','Original'],value['values']);self.assertEqual(0,value['events']);self.assertTrue(value['result']['ok']);self.assertFalse(value['result']['submitted'])

    def test_forged_or_changed_field_aborts_entire_batch_before_any_value_changes(self):
        html='<label>Subject<input name="subject"></label><input type="password" id="secret" aria-label="Password">'
        value=self.evaluate(html,[{'label':'Subject','value':'Hello'},{'selector':'#secret','label':'Password','tag':'input','type':'text','value':'bad'}])
        self.assertFalse(value['result']['ok']);self.assertEqual(['',''],value['values']);self.assertEqual(0,value['events'])

class FormReviewStorageTests(unittest.TestCase):
    setUp=BrowserFormTests.setUp
    prepare=BrowserFormTests.prepare
    def test_expired_and_completed_payloads_are_removed_on_activity_and_pending_is_bounded(self):
        review=self.prepare()
        with self.db.connect() as con:con.execute('UPDATE browser_form_reviews SET expires_at=0')
        self.forms.sessions()
        with self.db.connect() as con:self.assertEqual(0,con.execute('SELECT COUNT(*) FROM browser_form_reviews').fetchone()[0])
        with self.db.connect() as con:
            con.executemany('INSERT INTO browser_form_reviews VALUES (?,?,?,?,?)',[(str(i),'{}','x',9999999999,'pending') for i in range(100)])
        with self.assertRaisesRegex(ValueError,'BROWSER_FORM_REVIEW_LIMIT'):self.prepare()

class PublicFormOriginTests(unittest.TestCase):
    def test_only_https_public_pages_outside_auth_and_checkout_contexts(self):
        from olcr_api.browser_forms import public_form_origin
        self.assertEqual('https://forms.example',public_form_origin('https://forms.example/contact?topic=hello'))
        for url in ('http://forms.example/contact','https://127.0.0.1/contact','https://host.local/contact','https://user@forms.example/contact','https://forms.example/login.html','https://forms.example/%6cogin','https://forms.example/checkout/payment'):
            with self.assertRaises(ValueError,msg=url):public_form_origin(url)

class FormEvidenceValidationTests(unittest.TestCase):
    setUp=BrowserFormTests.setUp
    def test_malformed_evidence_is_rejected_without_uncontrolled_type_errors(self):
        for fields in (None,[{'selector':'#x','label':'Name','tag':'input','type':{}}]):
            self.manager.inspect_public_form.return_value.update(fields=fields)
            if fields is None:
                with self.assertRaises(ValueError):self.forms.inspect('session-one')
            else:self.assertEqual([],self.forms.inspect('session-one')['fields'])
