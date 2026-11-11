"""Native grammar checks without loading model weights or calling a provider."""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / '.venv-mlx/bin/python'
from olcr_api.mlx_worker import _asset
TOKENIZER = _asset('bonsai/Ternary-Bonsai-2-27B-mlx-2bit') / 'tokenizer.json'


@unittest.skipUnless(NATIVE.is_file() and TOKENIZER.is_file(), 'dedicated MLX runtime/tokenizer is unavailable')
class CodingReasoningNativeTests(unittest.TestCase):
    def test_reasoning_budget_closes_thinking_and_preserves_the_json_source(self):
        script = r'''
import json,re
from pathlib import Path
import mlx.core as mx
import numpy as np
from tokenizers import Tokenizer
from olcr_api.structured_generation import JSONSchemaConstraint, ReasoningJSONConstraint
t=Tokenizer.from_file(tokenizer_path)
schema={'type':'object','properties':{'content':{'type':'string'}},'required':['content'],'additionalProperties':False}
def encode(text):return t.encode(text,add_special_tokens=False).ids
def make():return ReasoningJSONConstraint(JSONSchemaConstraint(t,[t.token_to_id('<|im_end|>')],schema),t,reasoning_open=True,max_reasoning_tokens=8)
c=make()
for token in encode('Consider each requirement carefully. '*20)[:8]:c.consume(token)
closing=[]
for _ in range(16):
 if c.active:break
 logits=c.mask_logits(mx.zeros((1,c.constraint.tokenizer.vocab_size+64)))
 a=np.array(logits)
 assert np.isfinite(a).sum()==1,'Reasoning still has the whole vocabulary after its budget'
 token=int(a.argmax());closing.append(token);c.consume(token)
assert c.active,'Reasoning budget never handed control to the JSON grammar'
assert '</think>' in t.decode(closing,skip_special_tokens=False)
source={'content':'const tag = "</think>";\nconst literal = "<think>";'}
# JSON grammar emits control-looking source as ordinary character tokens.
def source_tokens(value):return [token for part in re.split('(<)',json.dumps(value,separators=(',',':'))) for token in encode(part)]
for token in source_tokens(source):c.consume(token)
assert c.constraint.matcher.is_accepting()
# A naturally closed thought must pass straight through to the same grammar.
c=make()
for token in encode('OK</think>'):
 c.consume(token)
assert c.active
for token in source_tokens(source):c.consume(token)
assert c.constraint.matcher.is_accepting()
'''
        script='tokenizer_path='+repr(str(TOKENIZER))+'\n'+script
        result = subprocess.run([str(NATIVE), '-c', script], cwd=ROOT,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend')}, capture_output=True, text=True, timeout=45)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
