"""Dedicated MLX subprocess boundary for the local role models.

This module is intentionally invoked by ``.venv-mlx/bin/python``.  It does
not expose a generic model loader to the application; each operation names a
manifest role and selects its pinned, role-specific loader.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
if __package__:
    from .thinking import received_reasoning, split_thinking
else:
    from thinking import received_reasoning, split_thinking
import sys
import time
import traceback
import os


ROOT = Path(__file__).resolve().parents[2]
def _asset(relative: str) -> Path:
    user = Path(os.environ.get('OLCR_MODEL_ASSETS_DIR', str(ROOT / 'models'))) / relative
    return user if (user / '.olcr-complete.json').is_file() else ROOT / 'models' / relative
_VISION_MODELS: dict[str, tuple[object, object]] = {}


def _json_constraint(tokenizer, eos_tokens, schema):
    if __package__:
        from .structured_generation import JSONSchemaConstraint
    else:
        from structured_generation import JSONSchemaConstraint
    return JSONSchemaConstraint(tokenizer, [int(token) for token in eos_tokens if token is not None], schema)


def _thinking_constraint(constraint,tokenizer,prompt,think,max_tokens=None):
    if not think:return constraint
    if __package__:
        from .structured_generation import ReasoningJSONConstraint
    else:
        from structured_generation import ReasoningJSONConstraint
    return ReasoningJSONConstraint(constraint,tokenizer,reasoning_open=prompt.rstrip().endswith('<think>'),
                                  max_reasoning_tokens=min(2048,max(1,max_tokens//4)) if max_tokens is not None else None)


def _decode_tokens(tokenizer,tokens,stop_ids=()):
    if tokens and tokens[-1] in stop_ids:tokens=tokens[:-1]
    try:return tokenizer.decode(tokens,skip_special_tokens=False)
    except TypeError:return tokenizer.decode(tokens)


_last_live_reasoning=''
def _live(data):
    if path:=os.environ.get('OLCR_MLX_LIVE_PATH'):
        try:
            with open(path,'a',encoding='utf8') as output:output.write(json.dumps(data,ensure_ascii=False)+'\n')
        except OSError:pass

def _live_reasoning(output,reasoning_open=False,complete=False):
    global _last_live_reasoning
    _,text,closed=split_thinking(output,reasoning_open=reasoning_open)
    text=text[:64000]
    if text and text!=_last_live_reasoning:
        snapshot=not text.startswith(_last_live_reasoning)
        delta=text if snapshot else text[len(_last_live_reasoning):]
        _live({'state':'reasoning','text':delta,'snapshot':snapshot,'kind':'trace','complete':bool(complete and closed)})
        _last_live_reasoning=text

def _progress(event: str, **details: object) -> None:
    """Emit payload-free checkpoints so the parent can explain worker timeouts."""
    value = {"event": event, **details}
    _live({"state":"worker_progress",**value})
    print("OLCR_MLX_PROGRESS=" + json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
    if progress_path := os.environ.get('OLCR_MLX_PROGRESS_PATH'):
        try:
            path = Path(progress_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf8') as output:
                output.write(json.dumps({'at':time.time(), **value}) + '\n')
        except OSError:
            pass


def _raise_template_exception(message: str) -> None:
    """Provide the Hugging Face chat-template helper expected by Bonsai."""
    raise ValueError(str(message))


def _normalize_leading_system_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep system instructions first and fold multiple system blocks together."""
    system_content = [str(message.get("content") or "") for message in messages
                      if message.get("role") == "system"]
    normalized = [message for message in messages if message.get("role") != "system"]
    if system_content:
        normalized.insert(0, {"role": "system", "content": "\n\n".join(system_content)})
    return normalized


def _render_bonsai_prompt(template_text: str, messages: list[dict[str, str]], think: bool = False) -> str:
    """Render the pinned template with the standard ``raise_exception`` helper."""
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    # OLCR composes grounding, evidence, and the typed execution contract as
    # separate system messages.  Bonsai's pinned template accepts only one
    # system message and requires it to be first; fold those equivalent
    # control messages together without reordering user/assistant content.
    normalized_messages = _normalize_leading_system_messages(messages)

    environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    # Transformers/tokenizers inject this global when rendering the same
    # template.  The dedicated MLX worker renders through Jinja directly, so
    # provide it explicitly rather than allowing a malformed template call to
    # abort every planner/implementer request.
    environment.globals["raise_exception"] = _raise_template_exception
    template = environment.from_string(template_text)
    return template.render(messages=normalized_messages, add_generation_prompt=True,
                           enable_thinking=think)


def complete_json_response(text: str, schema: dict | None) -> str | None:
    """Detect one complete framed JSON response without accepting prose fragments."""
    text = text.strip()
    if text.startswith('```') and '\n' in text:
        text = text.split('\n', 1)[1].strip()
    if not text.startswith('{'):
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or (schema and set(schema.get('required') or []) - value.keys()):
        return None
    return text[:end]


def _visible_ornith_output(output: str, *, reasoning_open: bool = False) -> str:
    return split_thinking(output, reasoning_open=reasoning_open)[0]


def prefill_bonsai(language_model, tokens, cache, mx, chunk_size: int = 512):
    """Bound attention allocations, keeping the last prompt token for decoding."""
    total = int(tokens.shape[-1])
    processed = 0
    while int(tokens.shape[-1]) > 1:
        count = min(chunk_size, int(tokens.shape[-1]) - 1)
        language_model(tokens[:, :count], cache=cache)
        mx.eval([entry.state for entry in cache])
        tokens = tokens[:, count:]
        processed += count
        mx.clear_cache()
        _progress('bonsai_prefill_progress', processed_tokens=processed, prompt_tokens=total)
    return tokens


def _check_context_length(prompt_tokens: int, max_tokens: int, context: dict | None) -> int:
    """Keep the complete prompt and fit the response to the exact remaining room."""
    if not context or not (context.get('yarn_enabled') or context.get('context_auto_compress')):
        return max_tokens
    window = int(context['yarn_target_tokens'] if context.get('yarn_enabled') else context['local_context_tokens'])
    available = window - prompt_tokens
    if available <= 0:
        raise ValueError(f"CONTEXT_WINDOW_EXCEEDED: prompt={prompt_tokens}, response={max_tokens}, window={window}. "
                         "Current input and required instructions were preserved; increase the context window or shorten the input.")
    effective = min(max_tokens, available)
    if effective < max_tokens:
        _progress('context_response_budget_adjusted', prompt_tokens=prompt_tokens,
                  requested_max_tokens=max_tokens, max_tokens=effective, window=window)
    return effective


def _context_controls(model, config: dict, context: dict | None) -> dict:
    if not context or not context.get('yarn_enabled'):
        return {}
    if __package__:
        from .context_extension import apply_yarn
    else:
        from context_extension import apply_yarn
    result = apply_yarn(model, config, int(context['yarn_target_tokens']))
    _progress('yarn_configured', **result)
    return result


def _bonsai(messages: list[dict[str, str]], max_tokens: int, schema: dict | None = None, context: dict | None = None, think: bool = False, *, bounded_reasoning: bool = False) -> dict:
    import mlx.core as mx
    from mlx_lm.sample_utils import make_sampler
    from tokenizers import Tokenizer

    pack = _asset('bonsai/Ternary-Bonsai-2-27B-mlx-2bit')
    sys.path.insert(0, str(pack / "runtime"))
    from vision_artifact import load_vl_model  # type: ignore

    load_started = time.perf_counter()
    _progress("bonsai_model_load_started")
    model, _, config = load_vl_model(pack, load_processor=False)
    _progress("bonsai_model_load_completed", elapsed_ms=round((time.perf_counter() - load_started) * 1000))
    yarn = _context_controls(model, config, context)
    tokenizer = Tokenizer.from_file(str(pack / "tokenizer.json"))
    structured = schema is not None
    think = bool(think)
    prompt = _render_bonsai_prompt(
        (pack / "chat_template.jinja").read_text(encoding="utf-8"), messages, think)
    tokens = mx.array([tokenizer.encode(prompt, add_special_tokens=False).ids])
    prompt_token_count = int(tokens.shape[-1])
    max_tokens = _check_context_length(prompt_token_count, max_tokens, context)
    _progress("bonsai_prompt_ready", prompt_token_count=int(tokens.shape[-1]), max_tokens=max_tokens)
    cache = model.language_model.make_cache()
    sampler = make_sampler(temp=0.0, top_p=1.0, top_k=20)
    generated: list[int] = []
    stop_ids = {tokenizer.token_to_id("<|im_end|>"), tokenizer.token_to_id("<|endoftext|>")}
    constraint = _json_constraint(tokenizer,stop_ids,schema) if isinstance(schema,dict) else None
    if constraint:
        constraint=_thinking_constraint(constraint,tokenizer,prompt,think,max_tokens if bounded_reasoning else None)
        _progress('structured_generation_enabled',engine='llguidance',model='bonsai')
    generation_started = time.perf_counter()
    finish_reason = 'length'
    structured_text = None
    _progress("bonsai_generation_started")
    tokens = prefill_bonsai(model.language_model.model, tokens, cache, mx)
    for _ in range(max_tokens):
        logits = model.language_model.lm_head(model.language_model.model(tokens, cache=cache)[:, -1:, :])[:, -1, :]
        if constraint:
            logits = constraint.mask_logits(logits)
        tokens = sampler(logits)[:, None]
        mx.eval(tokens)
        token = int(tokens.item())
        generated.append(token)
        if think and len(generated)%16==0:_live_reasoning(_decode_tokens(tokenizer,generated),reasoning_open=prompt.rstrip().endswith('<think>'))
        if len(generated) == 1 or len(generated) % 128 == 0:
            _progress("bonsai_generation_progress", generated_tokens=len(generated), max_tokens=max_tokens)
        if token in stop_ids:
            finish_reason = 'stop'
            break
        if constraint:
            constraint.consume(token)
        if isinstance(schema, dict) and len(generated) % 16 == 0:
            visible=split_thinking(_decode_tokens(tokenizer,generated),reasoning_open=think and prompt.rstrip().endswith('<think>'))[0]
            structured_text = complete_json_response(visible, schema)
            if structured_text is not None:
                finish_reason = 'complete_json'
                break
    _progress("bonsai_generation_completed", generated_tokens=len(generated),
              elapsed_ms=round((time.perf_counter() - generation_started) * 1000))
    output=_decode_tokens(tokenizer,generated,stop_ids)
    text, reasoning, complete = split_thinking(output,
                                               reasoning_open=think and prompt.rstrip().endswith('<think>'))
    if isinstance(schema,dict):structured_text=structured_text or complete_json_response(text,schema)
    if structured_text is not None:text=structured_text
    if think:_live_reasoning(output,reasoning_open=prompt.rstrip().endswith('<think>'),complete=True)
    return {'text': text, 'reasoning': received_reasoning(reasoning, complete=complete),
            'thinking_present': bool(reasoning), 'thinking_chars': len(reasoning),
            'finish_reason': finish_reason, 'completion_tokens': len(generated), 'prompt_tokens': prompt_token_count, **({'yarn': yarn} if yarn else {})}


def _vision(model_name: str, image: str, prompt: str, max_tokens: int) -> str:
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template

    cached = _VISION_MODELS.get(model_name)
    if cached is None:
        relative = "UI-Venus-1.5-2B-6bit" if model_name == "ui_venus_mlx_6bit" else "FastVLM-0.5B-bf16"
        model_path = _asset('vision/' + relative)
        load_started = time.perf_counter()
        _progress("vision_model_load_started", model=model_name)
        cached = load(str(model_path))
        _VISION_MODELS[model_name] = cached
        _progress("vision_model_load_completed", model=model_name,
                  elapsed_ms=round((time.perf_counter() - load_started) * 1000))
    model, processor = cached
    templated = apply_chat_template(processor, model.config, prompt, num_images=1)
    output = generate(model, processor, templated, image=image, max_tokens=max_tokens,
                      temp=0.0, verbose=False)
    return str(output.text).strip()


def _ornith(messages: list[dict[str, str]], max_tokens: int, think: bool | None = None,
            format: object | None = None, *, return_metadata: bool = False, context: dict | None = None,
            bounded_reasoning: bool = False) -> str | dict:
    """Run the pinned official Ornith 1.5 9B 8-bit MLX conversion."""
    from mlx_lm import generate, load, stream_generate
    from mlx_lm.sample_utils import make_sampler

    pack = _asset('ornith/Ornith-1.5-9B-MLX-8bit')
    if not (pack / "model.safetensors.index.json").is_file():
        raise FileNotFoundError(f"Ornith MLX weights are unavailable at {pack}")
    load_started = time.perf_counter()
    _progress("ornith_model_load_started", model="ornith-ai/Ornith-1.5-9B-MLX-8bit")
    model, tokenizer = load(str(pack))
    yarn = _context_controls(model, json.loads((pack / "config.json").read_text()), context)
    _progress("ornith_model_load_completed", elapsed_ms=round((time.perf_counter() - load_started) * 1000))
    # Runtime composition can insert system-level execution/evidence controls
    # after history. Ornith's tokenizer requires all system turns to lead.
    structured = isinstance(format, dict) or format == "json"
    if structured:
        schema = json.dumps(format, ensure_ascii=False) if isinstance(format, dict) else "a JSON object"
        messages = [{"role": "system", "content":
                     "Return only valid JSON satisfying this schema, without markdown or commentary: " + schema},
                    *messages]
    # Thinking remains free-form; the JSON grammar starts after </think>.
    template_options = ({"enable_thinking":bool(think)} if structured else
                        {"enable_thinking":think} if isinstance(think,bool) else {})
    prompt = tokenizer.apply_chat_template(
        _normalize_leading_system_messages(messages), tokenize=False, add_generation_prompt=True,
        **template_options)
    if context and (context.get("yarn_enabled") or context.get("context_auto_compress")):
        max_tokens = _check_context_length(len(tokenizer.encode(prompt)), max_tokens, context)
    _progress("ornith_prompt_ready", prompt_chars=len(prompt), max_tokens=max_tokens)
    generation_started = time.perf_counter()
    sampler = make_sampler(temp=0.0 if structured else 0.7, top_p=0.95, top_k=20, min_p=0.0)
    metadata = {}
    if structured:
        parts=[]
        structured_text=None
        constraint=_json_constraint(tokenizer,tokenizer.eos_token_ids,format if isinstance(format,dict) else {'type':'object'})
        constraint=_thinking_constraint(constraint,tokenizer,prompt,bool(think),max_tokens if bounded_reasoning else None)
        _progress('structured_generation_enabled',engine='llguidance',model='ornith')
        responses=stream_generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=sampler,
                                  logits_processors=[constraint])
        try:
            for response in responses:
                parts.append(response.text)
                tokens=response.generation_tokens
                metadata={'prompt_tokens':response.prompt_tokens,'completion_tokens':tokens,
                          'finish_reason':response.finish_reason}
                if tokens % 128 == 0:
                    _progress('ornith_generation_progress',generated_tokens=tokens,max_tokens=max_tokens)
                if tokens % 16 == 0 or response.finish_reason:
                    if think:_live_reasoning(''.join(parts),reasoning_open=prompt.rstrip().endswith('<think>'))
                    framed=_visible_ornith_output(''.join(parts),reasoning_open=bool(think) and prompt.rstrip().endswith('<think>'))
                    complete=complete_json_response(framed,format if isinstance(format,dict) else None)
                    if complete:
                        structured_text=complete
                        metadata['finish_reason']='json_complete'
                        break
        finally:
            responses.close()
        output=''.join(parts)
    elif os.environ.get('OLCR_MLX_LIVE_PATH'):
        parts=[]
        responses=stream_generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=sampler)
        try:
            for response in responses:
                parts.append(response.text)
                if getattr(response,'generation_tokens',0)%16==0 or getattr(response,'finish_reason',None):
                    _live_reasoning(''.join(parts),reasoning_open=prompt.rstrip().endswith('<think>'))
        finally:responses.close()
        output=''.join(parts)
        _live_reasoning(output,reasoning_open=prompt.rstrip().endswith('<think>'),complete=True)
    else:
        output=generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=sampler,verbose=False)
    _progress("ornith_generation_completed", elapsed_ms=round((time.perf_counter() - generation_started) * 1000),**metadata)
    # Keep template-framed reasoning separate from the final answer. No
    # additional generation or formatting call is needed to display this text.
    text, reasoning, complete = split_thinking(output, reasoning_open=prompt.rstrip().endswith('<think>'))
    if structured and structured_text is not None:text=structured_text
    if think:_live_reasoning(output,reasoning_open=prompt.rstrip().endswith('<think>'),complete=True)
    metadata.update(reasoning=received_reasoning(reasoning, complete=complete),
                    thinking_present=bool(reasoning), thinking_chars=len(reasoning))
    if yarn:
        metadata['yarn'] = yarn
    return {'text':text,**metadata} if return_metadata else text


def _generic_mlx(model_path: str, model_id: str, messages: list[dict[str, str]], max_tokens: int,
                 think: bool | None = None, format: object | None = None,
                 context: dict | None = None) -> dict:
    """Run a pinned MLX-LM snapshot whose family has no special OLCR adapter."""
    from mlx_lm import generate, load, stream_generate
    from mlx_lm.sample_utils import make_sampler

    pack = Path(model_path).expanduser().resolve()
    if not (pack / 'config.json').is_file():
        raise FileNotFoundError('MLX model files are unavailable')
    load_started = time.perf_counter()
    _progress('profile_model_load_started', model=model_id)
    model, tokenizer = load(str(pack))
    _progress('profile_model_load_completed', elapsed_ms=round((time.perf_counter() - load_started) * 1000))
    structured = isinstance(format, dict) or format == 'json'
    if structured:
        schema = json.dumps(format, ensure_ascii=False) if isinstance(format, dict) else 'a JSON object'
        messages = [{'role': 'system', 'content':
                     'Return only valid JSON satisfying this schema, without markdown or commentary: ' + schema},
                    *messages]
    template_options = {'enable_thinking': think} if isinstance(think, bool) else {}
    try:
        prompt = tokenizer.apply_chat_template(_normalize_leading_system_messages(messages),
            tokenize=False, add_generation_prompt=True, **template_options)
    except TypeError:
        prompt = tokenizer.apply_chat_template(_normalize_leading_system_messages(messages),
            tokenize=False, add_generation_prompt=True)
    prompt_tokens = len(tokenizer.encode(prompt))
    if context and (context.get('yarn_enabled') or context.get('context_auto_compress')):
        max_tokens = _check_context_length(prompt_tokens, max_tokens, context)
    sampler = make_sampler(temp=0.0 if structured else 0.7, top_p=0.95, top_k=20, min_p=0.0)
    constraint = None
    if structured:
        constraint = _json_constraint(tokenizer, getattr(tokenizer, 'eos_token_ids', []),
                                      format if isinstance(format, dict) else {'type': 'object'})
        constraint = _thinking_constraint(constraint, tokenizer, prompt, bool(think), max_tokens)
    parts=[]
    if os.environ.get('OLCR_MLX_LIVE_PATH') or structured:
        responses=stream_generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=sampler,
                                  logits_processors=[constraint] if constraint else [])
        try:
            for response in responses:
                parts.append(response.text)
                if think and (response.generation_tokens % 16 == 0 or response.finish_reason):
                    _live_reasoning(''.join(parts), reasoning_open=prompt.rstrip().endswith('<think>'))
                if response.finish_reason:
                    break
        finally:
            responses.close()
        output=''.join(parts)
    else:
        output=generate(model,tokenizer,prompt=prompt,max_tokens=max_tokens,sampler=sampler,verbose=False)
    reasoning_open=prompt.rstrip().endswith('<think>')
    text, reasoning, complete=split_thinking(output,reasoning_open=reasoning_open)
    if structured:
        complete_value=complete_json_response(text,format if isinstance(format,dict) else None)
        if complete_value:
            text=complete_value
    if think:
        _live_reasoning(output,reasoning_open=reasoning_open,complete=True)
    return {'text':text,'reasoning':received_reasoning(reasoning,complete=complete),
            'thinking_present':bool(reasoning),'thinking_chars':len(reasoning)}


def _bonsai_vlm(model_path: str, model_id: str, messages: list[dict[str, str]], max_tokens: int,
                think: bool | None = None, format: object | None = None,
                context: dict | None = None) -> dict:
    """Run a stock-runtime Bonsai 2 MLX pack through its supported VLM loader."""
    import mlx.core as mx
    from mlx_vlm import load, stream_generate
    from mlx_vlm.prompt_utils import apply_chat_template

    pack=Path(model_path).expanduser().resolve()
    config_path=pack/'config.json'
    if not config_path.is_file():
        raise FileNotFoundError('Bonsai 2 MLX model files are unavailable')
    config=json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('model_type')!='prism_hadamard_qwen35':
        raise ValueError('BONSAI_VLM_MODEL_TYPE_UNSUPPORTED')
    mx.set_default_device(mx.gpu)
    load_started=time.perf_counter()
    _progress('profile_model_load_started',model=model_id)
    model,processor=load(str(pack))
    _progress('profile_model_load_completed',elapsed_ms=round((time.perf_counter()-load_started)*1000))

    structured=isinstance(format,dict) or format=='json'
    schema=(format if isinstance(format,dict) else {'type':'object'}) if structured else None
    if structured:
        schema_text=json.dumps(schema,ensure_ascii=False)
        messages=[{'role':'system','content':
                   'Return only valid JSON satisfying this schema, without markdown or commentary: '+schema_text},
                  *messages]
    # Bonsai's supported MLX-VLM template accepts one text prompt. Keep each
    # conversation role explicit inside that prompt rather than dropping system
    # or assistant context before invoking the model.
    prompt_text='\n\n'.join(
        f"{str(row.get('role') or 'user').upper()}:\n{str(row.get('content') or '')}"
        for row in messages if isinstance(row,dict))
    thinking=think is not False
    prompt=apply_chat_template(processor,model.config,prompt_text,num_images=0,
                               enable_thinking=thinking)
    tokenizer=processor.tokenizer if hasattr(processor,'tokenizer') else processor
    prompt_tokens=len(tokenizer.encode(prompt))
    if context and (context.get('yarn_enabled') or context.get('context_auto_compress')):
        max_tokens=_check_context_length(prompt_tokens,max_tokens,context)
    constraint=None
    if structured:
        from mlx_vlm.structured import (ThinkingAwareLogitsProcessor,
                                        build_json_schema_logits_processor)
        constraint=build_json_schema_logits_processor(tokenizer,schema)
        constraint=ThinkingAwareLogitsProcessor(constraint,tokenizer,enable_thinking=thinking)
    temperature=0.0 if structured else (1.0 if thinking else 0.7)
    top_p=0.95 if thinking else 0.8
    min_p=0.0
    reasoning_open=prompt.rstrip().endswith('<think>')
    parts=[];last=None
    responses=stream_generate(model,processor,prompt,max_tokens=max_tokens,
        temperature=temperature,top_p=top_p,top_k=20,min_p=min_p,
        repetition_penalty=1.0,enable_thinking=thinking,
        logits_processors=[constraint] if constraint else [],verbose=False)
    try:
        for response in responses:
            parts.append(response.text)
            last=response
            if thinking and (response.generation_tokens%16==0 or response.finish_reason):
                _live_reasoning(''.join(parts),reasoning_open=reasoning_open,
                                complete=bool(response.finish_reason))
            if response.finish_reason:
                break
    finally:
        close=getattr(responses,'close',None)
        if callable(close):close()
    output=''.join(parts)
    text,reasoning,complete=split_thinking(output,reasoning_open=reasoning_open)
    if structured:
        complete_value=complete_json_response(text,schema)
        if not complete_value:
            raise ValueError('BONSAI_VLM_STRUCTURED_OUTPUT_INVALID')
        text=complete_value
    if thinking:
        _live_reasoning(output,reasoning_open=reasoning_open,complete=True)
    return {'text':text,'reasoning':received_reasoning(reasoning,complete=complete),
            'thinking_present':bool(reasoning),'thinking_chars':len(reasoning),
            **({'finish_reason':getattr(last,'finish_reason',None)} if last else {})}


def _serve_vision() -> None:
    """Keep the selected visual model loaded while handling bounded requests."""
    for line in sys.stdin:
        request_path = None
        try:
            request_path = Path(line.strip())
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            text = _vision(payload["model"], payload["image"], payload["prompt"],
                           int(payload.get("max_tokens", 256)))
            response = {"text": text}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}"[:2000]}
            traceback.print_exc(file=sys.stderr)
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=("bonsai", "bonsai_vlm", "ornith", "generic", "vision"))
    parser.add_argument("--payload")
    parser.add_argument("--serve-vision", action="store_true")
    args = parser.parse_args()
    if args.serve_vision:
        _serve_vision()
        return
    if not args.operation or not args.payload:
        parser.error("--operation and --payload are required unless --serve-vision is set")
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    if args.operation == "bonsai":
        result = _bonsai(payload["messages"], int(payload.get("max_tokens", 1024)), payload.get('format'), payload.get('context'), bool(payload.get('think')),
                         bounded_reasoning=bool(payload.get('bounded_reasoning')))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    elif args.operation == 'bonsai_vlm':
        result = _bonsai_vlm(payload['model_path'],payload['model_id'],payload['messages'],
            int(payload.get('max_tokens',2048)),payload.get('think'),payload.get('format'),payload.get('context'))
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return
    elif args.operation == "ornith":
        result = _ornith(payload["messages"], int(payload.get("max_tokens", 2048)),
                       payload.get("think"), payload.get("format"), return_metadata=True, context=payload.get("context"),
                       bounded_reasoning=bool(payload.get('bounded_reasoning')))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    elif args.operation == 'generic':
        result = _generic_mlx(payload['model_path'], payload['model_id'], payload['messages'],
            int(payload.get('max_tokens', 2048)), payload.get('think'), payload.get('format'), payload.get('context'))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    else:
        text = _vision(payload["model"], payload["image"], payload["prompt"], int(payload.get("max_tokens", 256)))
    print(json.dumps({"text": text}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
