"""JSON Schema token constraints for the dedicated single-sequence MLX workers."""
from __future__ import annotations


class JSONSchemaConstraint:
    def __init__(self, tokenizer, eos_tokens: list[int], schema: dict):
        try:
            from llguidance import LLMatcher, LLTokenizer
            from llguidance.numpy import allocate_token_bitmask, fill_next_token_bitmask
        except ImportError as exc:
            raise RuntimeError('STRUCTURED_GENERATION_RUNTIME_UNAVAILABLE: install backend/requirements-mlx.txt in OLCR_MLX_PYTHON') from exc
        backend = getattr(tokenizer, 'backend_tokenizer', tokenizer)
        serialized = backend.to_str()
        self.tokenizer = LLTokenizer(serialized, eos_token=eos_tokens, slices=LLTokenizer.json_slices())
        # Structural whitespace can become an endless fallback when the model
        # wants EOS before a required closing delimiter. Compact JSON removes
        # that loop; whitespace inside source-code string values is preserved.
        grammar = LLMatcher.grammar_from_json_schema(schema,overrides={'whitespace_flexible':False})
        self.matcher = LLMatcher(self.tokenizer, grammar, log_level=0)
        if self.matcher.is_error():
            raise RuntimeError('STRUCTURED_GENERATION_GRAMMAR_INVALID: ' + self.matcher.get_error())
        self._bitmask = allocate_token_bitmask(1, self.tokenizer.vocab_size)
        self._fill = fill_next_token_bitmask
        self._seen = None

    def consume(self, token: int) -> None:
        if not self.matcher.consume_token(token):
            raise RuntimeError('STRUCTURED_GENERATION_TOKEN_REJECTED: ' + self.matcher.get_error())

    def mask_logits(self, logits):
        from llguidance.mlx import apply_token_bitmask
        if self.matcher.is_error():
            raise RuntimeError('STRUCTURED_GENERATION_GRAMMAR_ERROR: ' + self.matcher.get_error())
        self._fill(self.matcher, self._bitmask)
        return apply_token_bitmask(logits, self._bitmask).reshape(logits.shape)

    def __call__(self, tokens, logits):
        # mlx_lm's first processor call includes the prompt's remaining prefill
        # chunk. Subsequent calls append generated tokens to that same prefix.
        length = int(tokens.size)
        if self._seen is not None:
            if length < self._seen:
                raise RuntimeError('STRUCTURED_GENERATION_SEQUENCE_REWOUND')
            for token in tokens[self._seen:].tolist():
                self.consume(int(token))
        self._seen = length
        return self.mask_logits(logits)


class ReasoningJSONConstraint:
    """Allow the model's thinking prelude, then constrain only its JSON answer.

    Both the manual Bonsai loop and mlx_lm's logits-processor interface use
    the same state machine. No second model call or fabricated trace is used.
    """
    def __init__(self, constraint, tokenizer, *, reasoning_open=False, max_reasoning_tokens=None):
        self.constraint=constraint
        self.tokenizer=tokenizer
        self.reasoning_open=reasoning_open
        self._in_reasoning=reasoning_open
        self.active=False
        self.generated=[]
        self._seen=None
        self.max_reasoning_tokens=None if max_reasoning_tokens is None else max(1,int(max_reasoning_tokens))
        self._closing=[]

    def consume(self, token):
        if self.active:
            self.constraint.consume(token)
            return
        if self._closing:
            expected=self._closing.pop(0)
            if token != expected:
                raise RuntimeError('STRUCTURED_REASONING_CLOSE_TOKEN_REJECTED')
        self.generated.append(token)
        try:
            from .thinking import split_thinking
        except ImportError:
            from thinking import split_thinking
        def decode(tokens):
            try:return self.tokenizer.decode(tokens,skip_special_tokens=False)
            except TypeError:return self.tokenizer.decode(tokens)
        if self._in_reasoning and '</think>' not in decode(self.generated[-16:]):return
        output=decode(self.generated)
        if not output.strip():return
        answer,_,closed=split_thinking(output,reasoning_open=self.reasoning_open)
        if not closed:
            self._in_reasoning=True
            return
        # An opening tag can span multiple tokenizer tokens. Wait until it
        # can be distinguished from a normal JSON prefix.
        if not self.reasoning_open and output.strip() and '<think>'.startswith(output.strip()):return
        self.active=True
        self.generated=[]
        if answer:
            encoded=self.tokenizer.encode(answer,add_special_tokens=False)
            for item in getattr(encoded,'ids',encoded):self.constraint.consume(int(item))

    def mask_logits(self, logits):
        if self.active:
            return self.constraint.mask_logits(logits)
        # Reserve the remaining output for the actual JSON. Closing the
        # received thought through constrained decoding keeps thinking enabled
        # without letting it consume the complete source-operation budget.
        if self._in_reasoning and self.max_reasoning_tokens is not None and len(self.generated)>=self.max_reasoning_tokens:
            if not self._closing:
                encoded=self.tokenizer.encode('\n</think>\n',add_special_tokens=False)
                self._closing=list(getattr(encoded,'ids',encoded))
            import mlx.core as mx
            return mx.where(mx.arange(logits.shape[-1]) == self._closing[0],logits,-float('inf'))
        return logits

    def __call__(self, tokens, logits):
        length=int(tokens.size)
        if self._seen is not None:
            if length<self._seen:raise RuntimeError('STRUCTURED_GENERATION_SEQUENCE_REWOUND')
            for token in tokens[self._seen:].tolist():self.consume(int(token))
        self._seen=length
        return self.mask_logits(logits)
