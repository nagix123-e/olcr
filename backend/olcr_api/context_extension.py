"""Local context controls; all shortening is request-local and extractive."""
from __future__ import annotations

import math
import re


def estimated_tokens(text: str) -> int:
    # Conservative heuristic, not a tokenizer. Japanese and source code are
    # deliberately charged more than ordinary English words.
    return math.ceil(sum(0.5 if ord(c) < 128 else 1.5 for c in text))


def compress_messages(messages: list[dict], window: int, reserve: int = 2048) -> tuple[list[dict], dict]:
    """Keep controls/current input verbatim; excerpt older data at 80% usage.

    An extract is never promoted to a system instruction. Tool call sequences
    remain atomic and untouched. Database history is never mutated.
    """
    count = lambda rows: sum(estimated_tokens(str(m.get('content') or '')) + 8 for m in rows)
    before = count(messages)
    report = {'method': 'extractive', 'estimated_tokens_before': before,
              'estimated_tokens_after': before, 'compressed': False,
              'over_budget': before + reserve > window}
    if before + reserve < window * .8:
        return messages, report
    latest = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get('role') == 'user'), -1)
    result = [dict(m) for m in messages]
    def shorten(message: dict, excerpt: str) -> None:
        original = str(message.get('content') or '')
        if len(excerpt) < len(original) and estimated_tokens(excerpt) < estimated_tokens(original):
            message['content'] = excerpt

    candidates = [i for i, m in enumerate(result) if i < latest and m.get('role') in {'user', 'assistant'}
                  and not m.get('tool_calls') and not m.get('tool_call_id')]
    target = max(0, int(window * .5) - reserve)
    # Reduce older turns first, preserving role and chronology. Explicit
    # excerpt markers prevent missing text being treated as complete evidence.
    for i in candidates:
        if count(result) <= target:
            break
        content = str(result[i].get('content') or '')
        if len(content) > 160:
            shorten(result[i], content[:80] + '\n[Earlier context excerpt; middle omitted]\n' + content[-80:])
    # Supplemental context often carries assembled history in a system block.
    # Only recognized data blocks are eligible; all other controls stay intact.
    from .context_policy import compact_json
    for m in result:
        if count(result) <= target:
            break
        # Prefixes in user/assistant/tool text are data, not host metadata.
        # In particular the current user input must never enter this pass.
        if m.get('role') != 'system':
            continue
        content = str(m.get('content') or '')
        if content.startswith('[PROVIDER_EVIDENCE_JSON]\n'):
            import json
            packet, _ = compact_json(json.loads(content.split('\n', 1)[1]), max(128, target // 4))
            shorten(m, '[PROVIDER_EVIDENCE_JSON]\n' + packet)
        elif content.startswith('Selected evidence:') and len(content) > max(256, target // 2):
            allowance = max(128, target // 2)
            shorten(m, content[:allowance] + '\n[Supporting evidence excerpt; remainder omitted]')
        elif content.startswith(('Workspace core context', '[RAG_CONTEXT]')) and '[ACTIVE_SKILL ' not in content:
            # Only shrink a marked history region, never workspace instructions.
            excerpt = re.sub(r'(\[ACTIVE_CONVERSATION\]\n)(.*?)(?=\n\[[A-Z_]+\]|\Z)',
                lambda match: match[1] + match[2][:400] + '\n[Earlier conversation excerpt omitted]\n' + match[2][-800:]
                if len(match[2]) > 1400 else match[0], content, flags=re.S)
            shorten(m, excerpt)
    after = count(result)
    report.update(estimated_tokens_after=after, compressed=after < before,
                  over_budget=after + reserve > window)
    return result, report


def apply_yarn(model, config: dict, target: int) -> dict:
    """Apply pinned MLX YaRN frequencies to text attention only, before prefill."""
    text = config.get('text_config', config)
    original = int(text['max_position_embeddings'])
    if target <= original:
        return {'status': 'native_window', 'original_tokens': original, 'target_tokens': target, 'factor': 1.0}
    from mlx_lm.models.rope_utils import YarnRoPE
    import mlx.nn as nn
    rope = text.get('rope_parameters') or text.get('rope_scaling') or {}
    if rope.get('rope_type', rope.get('type', 'default')) != 'default':
        raise ValueError('YaRN cannot replace an already scaled RoPE configuration')
    dims = int(text.get('head_dim', text['hidden_size'] // text['num_attention_heads']) *
               rope.get('partial_rotary_factor', text.get('partial_rotary_factor', 1.0)))
    base = float(rope.get('rope_theta', text.get('rope_theta', 10000)))
    factor = target / original
    language_model = getattr(model, 'language_model', model)
    changed = 0
    for _, module in language_model.named_modules():
        old = getattr(module, 'rope', None)
        rotary = getattr(module, 'rotary_emb', None)
        if isinstance(old, nn.RoPE):
            module.rope = YarnRoPE(dims, traditional=old.traditional, base=base,
                scaling_factor=factor, original_max_position_embeddings=original, max_position_embeddings=target)
            changed += 1
        elif rotary is not None and hasattr(rotary, '_inv_freq') and hasattr(rotary, 'attention_scaling'):
            yarn = YarnRoPE(dims, base=base, scaling_factor=factor,
                original_max_position_embeddings=original, max_position_embeddings=target)
            rotary._inv_freq = 1.0 / yarn._freqs
            rotary.attention_scaling = yarn.mscale
            # The pinned fused MRoPE kernel omits the attention scaling term.
            rotary.fused_apply = False
            changed += 1
    if not changed:
        raise ValueError('This MLX text architecture does not support runtime YaRN')
    return {'status': 'applied', 'original_tokens': original, 'target_tokens': target,
            'factor': factor, 'attention_layers': changed}
