import unittest
import numpy as np
from olcr_api.structured_generation import ReasoningJSONConstraint

class Tokenizer:
    def decode(self,tokens,**kwargs):return ''.join(map(chr,tokens))
    def encode(self,text,**kwargs):return list(map(ord,text))

class Grammar:
    def __init__(self):self.tokens=[];self.masks=0
    def consume(self,token):self.tokens.append(token)
    def mask_logits(self,logits):self.masks+=1;return 'masked'

class ReasoningJSONTests(unittest.TestCase):
    def test_manual_generation_keeps_reasoning_free_and_constrains_only_json(self):
        grammar=Grammar();gate=ReasoningJSONConstraint(grammar,Tokenizer(),reasoning_open=True)
        for token in map(ord,'compare { alternatives }'):
            gate.consume(token);self.assertEqual('raw',gate.mask_logits('raw'))
        for token in map(ord,'</think>'):gate.consume(token)
        self.assertEqual('masked',gate.mask_logits('raw'))
        answer='{"code":"literal </think>"}'
        for token in map(ord,answer):gate.consume(token)
        self.assertEqual(answer,''.join(map(chr,grammar.tokens)))

    def test_logits_processor_skips_prefill_and_handles_a_split_thinking_tag(self):
        grammar=Grammar();gate=ReasoningJSONConstraint(grammar,Tokenizer())
        tokens=[99,99,99]
        self.assertEqual('raw',gate(np.array(tokens),'raw'))
        for char in '<think>work</think>{"answer":1}':
            tokens.append(ord(char));gate(np.array(tokens),'raw')
        self.assertEqual('{"answer":1}',''.join(map(chr,grammar.tokens)))
        self.assertGreater(grammar.masks,0)
        with self.assertRaisesRegex(RuntimeError,'REWOUND'):gate(np.array([0]),'raw')

    def test_boundary_token_containing_json_preserves_its_suffix(self):
        class BoundaryTokenizer(Tokenizer):
            def decode(self,tokens,**kwargs):return ''.join({1:'work',2:'</think>\n{"value":'}.get(t,chr(t)) for t in tokens)
        grammar=Grammar();gate=ReasoningJSONConstraint(grammar,BoundaryTokenizer(),reasoning_open=True)
        gate.consume(1);gate.consume(2)
        for token in map(ord,'3}'):gate.consume(token)
        self.assertEqual('{"value":3}',''.join(map(chr,grammar.tokens)))
