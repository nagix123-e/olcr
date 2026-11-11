"""Exercise the real native tokenizer, grammar engine and Metal token mask."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[2]
NATIVE=ROOT/'.venv-mlx/bin/python'


@unittest.skipUnless(NATIVE.is_file(),'dedicated MLX runtime is unavailable')
class StructuredGenerationTests(unittest.TestCase):
    def test_source_repair_grammar_supports_deletion_and_enforces_snapshot_bounds(self):
        from olcr_api.source_repair import repair_schema
        schema=repair_schema({'ui.js':'function render() {}\nconst alias=render;\nrender();\n'})
        script='schema='+repr(schema)+'\n'+r'''
import copy,json
from pathlib import Path
from tokenizers import Tokenizer
from olcr_api.structured_generation import JSONSchemaConstraint
t=Tokenizer.from_file(str(Path.cwd()/'models/bonsai/Ternary-Bonsai-2-27B-mlx-2bit/tokenizer.json'))
eos=[t.token_to_id('<|im_end|>')]
valid={'edits':[{'action':'delete','path':'ui.js','start_line':2,'end_line':2}],'reason':'Remove obsolete alias'}
def tokens(value):return t.encode(json.dumps(value,separators=(',',':')),add_special_tokens=False).ids
c=JSONSchemaConstraint(t,eos,schema)
for token in tokens(valid):c.consume(token)
assert c.matcher.is_accepting()
invalid=[]
v=copy.deepcopy(valid);v['edits'][0]['path']='outside.js';invalid.append(v)
v=copy.deepcopy(valid);v['edits'][0]['end_line']=4;invalid.append(v)
v=copy.deepcopy(valid);v['edits'][0]['replacement']='';invalid.append(v)
v=copy.deepcopy(valid);v['edits'][0]['action']='replace';invalid.append(v)
for value in invalid:
 c=JSONSchemaConstraint(t,eos,schema);encoded=tokens(value)
 assert c.matcher.validate_tokens(encoded)<len(encoded),value
'''
        result=subprocess.run([str(NATIVE),'-c',script],cwd=ROOT,
            env={**os.environ,'PYTHONPATH':str(ROOT/'backend')},text=True,capture_output=True,timeout=45)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_recovery_transport_grammar_rejects_invalid_action_and_module_shapes(self):
        from olcr_api.coding_verification import verification_observation_schema, browser_observation_schema
        schema=verification_observation_schema({'DONE:1':'Verify the actual module and page behavior'})
        initial=browser_observation_schema({'DONE:1':'Verify the actual module and page behavior'})
        script='schema='+repr(schema)+'\ninitial='+repr(initial)+'\n'+r'''
import copy,json
from pathlib import Path
from tokenizers import Tokenizer
from olcr_api.structured_generation import JSONSchemaConstraint
t=Tokenizer.from_file(str(Path.cwd()/'models/bonsai/Ternary-Bonsai-2-27B-mlx-2bit/tokenizer.json'))
eos=[t.token_to_id('<|im_end|>')]
valid={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
 'actions':[{'action':'viewport','selector':'page','value':{'width':320,'height':640}},
            {'action':'click','selector':'#clear','value':None},
            {'action':'select','selector':'#meal-slot','value':'monday-dinner'},
            {'action':'reload','selector':'page','value':None}],
 'observations':[{'selector':'logic.cjs','measure':'module_call','operator':'eq','expected':3,
                  'export_name':'measure','arguments':['A"\n']}]}]}
def tokens(value):return t.encode(json.dumps(value,ensure_ascii=False,separators=(',',':')),add_special_tokens=False).ids
c=JSONSchemaConstraint(t,eos,schema)
for token in tokens(valid):c.consume(token)
assert c.matcher.is_accepting()
invalid=[]
dynamic={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
 'code':['const before=await page.locator("#total").textContent();','await page.locator("#add").click();','assert.notEqual(await page.locator("#total").textContent(),before);']}]}
c=JSONSchemaConstraint(t,eos,schema);encoded=tokens(dynamic)
assert c.matcher.validate_tokens(encoded)==len(encoded)
bad=copy.deepcopy(dynamic);bad['checks'][0]['code']='assert(true)'
c=JSONSchemaConstraint(t,eos,schema);encoded=tokens(bad)
assert c.matcher.validate_tokens(encoded)<len(encoded)
v=copy.deepcopy(valid);v['checks'][0]['actions'][1]['value']='0';invalid.append(v)
v=copy.deepcopy(valid);v['checks'][0]['actions'][3]['selector']=':root';invalid.append(v)
v=copy.deepcopy(valid);del v['checks'][0]['observations'][0]['arguments'];invalid.append(v)
v=copy.deepcopy(valid);v['checks'][0]['actions'][0]['value']['width']='320';invalid.append(v)
v=copy.deepcopy(valid);v['checks'][0]['code']='assert(true)';invalid.append(v)
v=copy.deepcopy(valid);v['checks'][0]['observations']=[{'selector':'html','measure':'visible','operator':'eq','expected':'visible'}];invalid.append(v)
for value in invalid:
 c=JSONSchemaConstraint(t,eos,schema);encoded=tokens(value)
 assert c.matcher.validate_tokens(encoded)<len(encoded),value
base={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[],
 'observations':[{'selector':'#message','measure':'text','operator':'eq','expected':''}]}]}
for transport in (initial,schema):
 c=JSONSchemaConstraint(t,eos,transport);encoded=tokens(base)
 assert c.matcher.validate_tokens(encoded)==len(encoded)
 workflow=copy.deepcopy(base);workflow['checks'][0]['actions']=[
  {'action':'select','selector':'#meal-slot','value':'monday-dinner'},
  {'action':'reload','selector':'page','value':None}]
 c=JSONSchemaConstraint(t,eos,transport);encoded=tokens(workflow)
 assert c.matcher.validate_tokens(encoded)==len(encoded)
 invalid_reload=copy.deepcopy(workflow);invalid_reload['checks'][0]['actions'][1]['selector']=':root'
 c=JSONSchemaConstraint(t,eos,transport);encoded=tokens(invalid_reload)
 assert c.matcher.validate_tokens(encoded)<len(encoded),transport
 for accepted,count in [(True,24),(False,33)]:
  workflow=copy.deepcopy(base);workflow['checks'][0]['actions']=[{'action':'click','selector':'#add','value':None}]*count
  c=JSONSchemaConstraint(t,eos,transport);encoded=tokens(workflow)
  assert (c.matcher.validate_tokens(encoded)==len(encoded))==accepted
 for measure,operator,expected in [('text','includes',''),('visible','eq','true'),('count','eq','3'),('visible','gt',1)]:
  bad=copy.deepcopy(base);bad['checks'][0]['observations'][0].update(measure=measure,operator=operator,expected=expected)
  c=JSONSchemaConstraint(t,eos,transport);encoded=tokens(bad)
  assert c.matcher.validate_tokens(encoded)<len(encoded),bad
'''
        env={**os.environ,'PYTHONPATH':str(ROOT/'backend')}
        result=subprocess.run([str(NATIVE),'-c',script],cwd=ROOT,env=env,text=True,capture_output=True,timeout=45)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_browser_observation_schema_accepts_literal_actions_and_rejects_raw_code(self):
        from olcr_api.coding_verification import verification_schema
        schema=verification_schema({'VERIFY:1':'index.html exists and renders a Japanese UI with input and a button.'})
        layout_schema=verification_schema({'VERIFY:1':'style.css exists and provides a mobile-friendly responsive layout.'})
        script='schema='+repr(schema)+'\nlayout_schema='+repr(layout_schema)+'\n'+r'''
import json
from pathlib import Path
from tokenizers import Tokenizer
from olcr_api.structured_generation import JSONSchemaConstraint
t=Tokenizer.from_file(str(Path.cwd()/'models/bonsai/Ternary-Bonsai-2-27B-mlx-2bit/tokenizer.json'))
eos=[t.token_to_id('<|im_end|>')]
valid={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
 'actions':[{'action':'fill','selector':'#input','value':'魚"A\nB'},{'action':'click','selector':'#select','value':None}],
 'observations':[{'selector':'#result','measure':'text','operator':'one_of','expected':['A','B']},
  {'selector':'#input','measure':'visible','operator':'eq','expected':True},
  {'selector':'#history','measure':'count','operator':'gte','expected':1}]}]}
constraint=JSONSchemaConstraint(t,eos,schema)
raw=json.dumps(valid,ensure_ascii=False,separators=(',',':'))
tokens=t.encode(raw,add_special_tokens=False).ids
assert constraint.matcher.validate_tokens(tokens)==len(tokens)
for token in tokens:constraint.consume(token)
assert constraint.matcher.is_accepting()
invalid={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':['assert.ok(true);']}]}
constraint=JSONSchemaConstraint(t,eos,schema)
tokens=t.encode(json.dumps(invalid,separators=(',',':')),add_special_tokens=False).ids
assert constraint.matcher.validate_tokens(tokens)<len(tokens)
layout={'entrypoint':'index.html','checks':[
 {'ids':['VERIFY:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page','value':{'width':375,'height':812}}],
  'observations':[{'selector':'html','measure':'overflow_x','operator':'eq','expected':False}]},
 {'ids':['VERIFY:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page','value':{'width':1280,'height':800}}],
  'observations':[{'selector':'html','measure':'overflow_x','operator':'eq','expected':False}]}]}
constraint=JSONSchemaConstraint(t,eos,layout_schema)
tokens=t.encode(json.dumps(layout,separators=(',',':')),add_special_tokens=False).ids
assert constraint.matcher.validate_tokens(tokens)==len(tokens)
layout['checks'][0]['actions']=[{'action':'click','selector':'#select','value':None}]
constraint=JSONSchemaConstraint(t,eos,layout_schema)
tokens=t.encode(json.dumps(layout,separators=(',',':')),add_special_tokens=False).ids
assert constraint.matcher.validate_tokens(tokens)<len(tokens)
print('Native typed browser observation grammar: PASS')
'''
        env={key:value for key,value in os.environ.items() if key!='PYTHONHOME'}
        env['PYTHONPATH']=str(ROOT/'backend')
        result=subprocess.run([str(NATIVE),'-c',script],cwd=ROOT,env=env,text=True,capture_output=True,timeout=45)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_real_schema_rejects_invalid_operations_and_preserves_code_strings(self):
        script=r'''
import json,numpy as np
from pathlib import Path
from tokenizers import Tokenizer
import mlx.core as mx
from olcr_api.structured_generation import JSONSchemaConstraint
root=Path.cwd()
t=Tokenizer.from_file(str(root/'models/bonsai/Ternary-Bonsai-2-27B-mlx-2bit/tokenizer.json'))
eos=[t.token_to_id('<|im_end|>')]
schema={'type':'object','properties':{'operations':{'type':'array','minItems':1,'items':{'oneOf':[
 {'type':'object','properties':{'op':{'const':'write'},'path':{'enum':['app.js']},'content':{'type':'string'}},'required':['op','path','content'],'additionalProperties':False},
 {'type':'object','properties':{'op':{'const':'patch'},'path':{'enum':['app.js']},'expected_old_fragment':{'type':'string'},'replacement_fragment':{'type':'string'}},'required':['op','path','expected_old_fragment','replacement_fragment'],'additionalProperties':False}]}}},'required':['operations'],'additionalProperties':False}
def make():return JSONSchemaConstraint(t,eos,schema)
valid=json.dumps({'operations':[{'op':'write','path':'app.js','content':'const label="料理";\nconst value="A\\nB";'}]},ensure_ascii=False,separators=(',',':'))
c=make()
for token in t.encode(valid,add_special_tokens=False).ids:c.consume(token)
assert c.matcher.is_accepting()
for value in ({'operations':[{'op':'shell','path':'app.js','content':'wrong'}]}, {'operations':[{'op':'write','path':'outside.js','content':'wrong'}]}, {'operations':[]}):
 c=make();tokens=t.encode(json.dumps(value,separators=(',',':')),add_special_tokens=False).ids
 assert c.matcher.validate_tokens(tokens)<len(tokens),value
c=make();bad=t.encode('I',add_special_tokens=False).ids
assert len(bad)==1
logits=mx.zeros((1,c.tokenizer.vocab_size+64));masked=c.mask_logits(logits)
mx.eval(masked)
a=np.array(masked)
assert masked.shape==logits.shape
assert np.isneginf(a[0,bad[0]]) and np.isneginf(a[0,eos[0]])
assert np.isneginf(a[0,c.tokenizer.vocab_size:]).all()
prefix=mx.array([101,102,103]);c(prefix,logits)
first=t.encode('{',add_special_tokens=False).ids
c(mx.concatenate([prefix,mx.array(first)]),logits)
assert c._seen==len(prefix)+len(first)
write=schema['properties']['operations']['items']['oneOf'][0]
ordered={**schema,'properties':{'operations':{'type':'array','minItems':2,'maxItems':2,
 'items':schema['properties']['operations']['items'],'prefixItems':[
 {**write,'properties':{**write['properties'],'path':{'const':name}}} for name in ['a.js','b.js']]}}}
for valid,paths in [(True,['a.js','b.js']),(False,['a.js','a.js']),(False,['b.js','a.js'])]:
 constraint=JSONSchemaConstraint(t,eos,ordered)
 raw=json.dumps({'operations':[{'op':'write','path':name,'content':'const value=1;'} for name in paths]},separators=(',',':'))
 tokens=t.encode(raw,add_special_tokens=False).ids
 assert (constraint.matcher.validate_tokens(tokens)==len(tokens))==valid
c=make();whitespace=t.encode('\n'*20,add_special_tokens=False).ids
assert c.matcher.validate_tokens(whitespace)<len(whitespace)
pending=json.dumps({'operations':[{'op':'write','path':'app.js','content':'const value=1;'}]},separators=(',',':'))[:-1]
c=make()
for token in t.encode(pending,add_special_tokens=False).ids:c.consume(token)
assert not c.matcher.is_accepting()
assert c.matcher.validate_tokens(whitespace)==0
print('Native grammar, schema branches, EOS, padded vocabulary and prompt boundary: PASS')
'''
        env={key:value for key,value in os.environ.items() if key!='PYTHONHOME'}
        env['PYTHONPATH']=str(ROOT/'backend')
        result=subprocess.run([str(NATIVE),'-c',script],cwd=ROOT,env=env,text=True,capture_output=True,timeout=45)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
