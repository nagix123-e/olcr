import unittest
from pathlib import Path

from olcr_api.mlx_worker import _normalize_leading_system_messages, _render_bonsai_prompt


class MlxWorkerTemplateTests(unittest.TestCase):
    def test_preserves_reasoning_markers_and_strips_only_the_terminal_stop_token(self):
        import json
        from olcr_api.mlx_worker import _decode_tokens
        from olcr_api.thinking import split_thinking
        class Tokenizer:
            def decode(self,tokens,**kwargs):
                return ''.join('<|im_end|>' if t==0 else chr(t) for t in tokens)
        answer='{"code":"literal <|im_end|>"}'
        raw='analysis</think>'+answer
        decoded=_decode_tokens(Tokenizer(),list(map(ord,raw))+[0],{0})
        text,reasoning,complete=split_thinking(decoded,reasoning_open=True)
        self.assertEqual({'code':'literal <|im_end|>'},json.loads(text))
        self.assertEqual('analysis',reasoning);self.assertTrue(complete)

    def test_think_tags_inside_generated_source_are_preserved(self):
        from olcr_api.mlx_worker import _visible_ornith_output
        source='{"content":"const tag = \'</think>\';"}'
        self.assertEqual(source,_visible_ornith_output(source))
        self.assertEqual(source,_visible_ornith_output('<think>private reasoning</think>\n'+source))
        self.assertEqual('',_visible_ornith_output('<think>unfinished reasoning'))

    def test_open_reasoning_prompt_returns_only_the_completed_visible_suffix(self):
        from olcr_api.mlx_worker import _visible_ornith_output
        source='{"operations":[{"content":"const tag = \\\"</think>\\\";"}]}'
        self.assertEqual(source,_visible_ornith_output('private analysis</think>\n'+source,reasoning_open=True))
        self.assertEqual('',_visible_ornith_output('unfinished private analysis',reasoning_open=True))

    def test_bonsai_prefill_bounds_chunks_and_preserves_the_last_prompt_token(self):
        import numpy as np
        from unittest.mock import Mock
        from olcr_api.mlx_worker import prefill_bonsai
        for length in (1,512,1537,14680):
            with self.subTest(length=length):
                chunks=[]
                def model(tokens,cache):chunks.append(tokens.copy())
                mx=Mock()
                result=prefill_bonsai(model,np.arange(length)[None,:],[Mock(state=[])],mx)
                self.assertEqual([[length-1]],result.tolist())
                self.assertEqual(list(range(length-1)),[int(v) for chunk in chunks for v in chunk[0]])
                self.assertTrue(all(chunk.shape[1]<=512 for chunk in chunks))
                self.assertEqual(len(chunks),mx.eval.call_count)
                self.assertEqual(len(chunks),mx.clear_cache.call_count)

    def test_ornith_structured_request_injects_schema_and_uses_deterministic_sampling(self):
        import sys
        import types
        from unittest.mock import patch
        from olcr_api.mlx_worker import _ornith
        captured = {}
        class Tokenizer:
            eos_token_ids={0}
            def apply_chat_template(self, messages, **kwargs):
                captured["messages"] = messages
                captured['template_options']=kwargs
                return "prompt"
        def sampler(**kwargs):
            captured["sampler"] = kwargs
        def stream(*args,**kwargs):
            yield types.SimpleNamespace(text='{"answer":"ok"}',generation_tokens=16,prompt_tokens=8,finish_reason='stop')
        modules = {"mlx_lm": types.SimpleNamespace(load=lambda path: (object(), Tokenizer()),
                   generate=lambda *args, **kwargs: '{"answer":"ok"}',stream_generate=stream),
                   "mlx_lm.sample_utils": types.SimpleNamespace(make_sampler=sampler)}
        schema = {"type": "object", "required": ["answer"]}
        original = [{"role": "user", "content": "Return the answer"}]
        with patch.dict(sys.modules, modules), patch('olcr_api.mlx_worker._json_constraint',return_value=object()) as constraint:
            for think in (False,None,True):
                self.assertEqual('{"answer":"ok"}', _ornith(original, 256, think, schema))
                self.assertIs(think is True,captured['template_options']['enable_thinking'])
        self.assertEqual(schema,constraint.call_args.args[2])
        self.assertIn('"required": ["answer"]', captured["messages"][0]["content"])
        self.assertEqual(0.0, captured["sampler"]["temp"])
        self.assertEqual(1, len(original))

    def test_ornith_message_normalization_moves_system_instructions_before_history(self):
        self.assertEqual(
            _normalize_leading_system_messages([
                {"role": "user", "content": "earlier turn"},
                {"role": "system", "content": "execution contract"},
                {"role": "assistant", "content": "prior answer"},
                {"role": "system", "content": "evidence policy"},
                {"role": "user", "content": "current request"},
            ]),
            [
                {"role": "system", "content": "execution contract\n\nevidence policy"},
                {"role": "user", "content": "earlier turn"},
                {"role": "assistant", "content": "prior answer"},
                {"role": "user", "content": "current request"},
            ],
        )

    def test_ornith_nonreasoning_request_closes_the_actual_template_think_prefix(self):
        import sys
        import types
        from unittest.mock import patch
        from jinja2 import Environment
        from olcr_api.mlx_worker import _ornith
        template = (Path(__file__).resolve().parents[2] / "models" / "ornith" /
                    "Ornith-1.5-9B-MLX-8bit" / "chat_template.jinja").read_text()
        prompts = []
        class Tokenizer:
            def apply_chat_template(self, messages, tokenize, **kwargs):
                return Environment().from_string(template).render(messages=messages, **kwargs)
        def generate(model, tokenizer, *, prompt, **kwargs):
            prompts.append(prompt)
            return ('private analysis</think>\n' if prompt.endswith('<think>\n') else '')+'{"decision":"wait"}'
        modules = {"mlx_lm": types.SimpleNamespace(load=lambda path: (object(), Tokenizer()), generate=generate,stream_generate=lambda:None),
                   "mlx_lm.sample_utils": types.SimpleNamespace(make_sampler=lambda **kwargs: None)}
        with patch.dict(sys.modules, modules):
            for think in (False, None, True):
                self.assertEqual('{"decision":"wait"}', _ornith([{"role": "user", "content": "Next action"}], 128, think))
        self.assertTrue(prompts[0].endswith("<think>\n\n</think>\n\n"))
        self.assertTrue(prompts[1].endswith("<think>\n"))
        self.assertTrue(prompts[2].endswith("<think>\n"))

    def test_ornith_stops_at_complete_json_and_records_length_failures(self):
        import sys
        import types
        from unittest.mock import patch
        from olcr_api.mlx_worker import _ornith
        closed=[]
        class Tokenizer:
            eos_token_ids={0}
            def apply_chat_template(self,*args,**kwargs):return 'prompt'
        def stream(*args,**kwargs):
            try:
                yield types.SimpleNamespace(text='{"answer":',generation_tokens=16,prompt_tokens=9,finish_reason=None)
                yield types.SimpleNamespace(text='"ok"} trailing prose',generation_tokens=32,prompt_tokens=9,finish_reason=None)
                self.fail('must close generation after a complete JSON object')
            finally:closed.append(True)
        modules={'mlx_lm':types.SimpleNamespace(load=lambda path:(object(),Tokenizer()),stream_generate=stream,generate=lambda:None),
                 'mlx_lm.sample_utils':types.SimpleNamespace(make_sampler=lambda **kwargs:None)}
        with patch.dict(sys.modules,modules), patch('olcr_api.mlx_worker._json_constraint',return_value=object()):
            result=_ornith([],128,False,{'required':['answer']},return_metadata=True)
        self.assertEqual({'text':'{"answer":"ok"}','completion_tokens':32,'prompt_tokens':9,'finish_reason':'json_complete',
                          'reasoning':None,'thinking_chars':0,'thinking_present':False},result)
        self.assertEqual([True],closed)
        def truncated(*args,**kwargs):
            yield types.SimpleNamespace(text='{"answer":',generation_tokens=128,prompt_tokens=9,finish_reason='length')
        modules['mlx_lm'].stream_generate=truncated
        with patch.dict(sys.modules,modules), patch('olcr_api.mlx_worker._json_constraint',return_value=object()):
            result=_ornith([],128,False,{'required':['answer']},return_metadata=True)
        self.assertEqual('length',result['finish_reason'])
        self.assertEqual('{"answer":',result['text'])

    def test_bonsai_template_has_raise_exception_global(self):
        template_path = (Path(__file__).resolve().parents[2] / "models" / "bonsai" /
                         "Ternary-Bonsai-2-27B-mlx-2bit" / "chat_template.jinja")
        prompt = _render_bonsai_prompt(
            template_path.read_text(encoding="utf-8"),
            [{"role": "system", "content": "Use JSON."},
             {"role": "user", "content": "Create a file."}],
        )
        self.assertIn("<|im_start|>user", prompt)
        self.assertIn("Create a file.", prompt)

    def test_template_exception_helper_remains_authoritative(self):
        with self.assertRaisesRegex(ValueError, "bad template input"):
            _render_bonsai_prompt("{{ raise_exception('bad template input') }}", [])

    def test_multiple_system_messages_are_folded_into_one_leading_message(self):
        prompt = _render_bonsai_prompt(
            "{% for message in messages %}[{{ message.role }}:{{ message.content }}]{% endfor %}",
            [{"role": "system", "content": "first"},
             {"role": "user", "content": "question"},
             {"role": "system", "content": "grounding"}],
        )
        self.assertEqual("[system:first\n\ngrounding][user:question]", prompt)


if __name__ == "__main__":
    unittest.main()
