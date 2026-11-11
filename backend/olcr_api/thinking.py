"""Pure local thinking policy and passive, bounded response-channel parsing."""
from __future__ import annotations

MAX_REASONING_CHARS = 64_000
THINKING_MODES = {'auto', 'on', 'off'}


class ThinkingProgressGuard:
    """Detect exact repeated long passages, independent of token boundaries.

    Only inspect the received reasoning channel. Repeated output/code is not
    evidence of a stalled thought. Keep a bounded window, and require four
    identical 512-character passages before declaring a loop.
    """
    def __init__(self):
        self.tail = ''
        self.since_check = 0

    def repeats(self, delta: str) -> bool:
        self.tail = (self.tail + delta)[-16384:]
        self.since_check += len(delta)
        if self.since_check < 256 or len(self.tail) < 2048:
            return False
        self.since_check = 0
        passage = self.tail[-512:]
        return bool(passage.strip()) and self.tail.count(passage) >= 4


def configured_thinking_mode(configuration: dict, role: str = 'normal_chat') -> str:
    """Reuse the existing per-role effort setting; no parallel mode state."""
    slots = {'normal_chat': 'general_inference', 'custom_ollama': 'general_inference', 'long_implementation': 'coder',
             'short_coding_fix_debug': 'short_coding', 'planner_replanner': 'general_inference'}
    slot = 'brain' if configuration.get('mode') == 'unified' else slots.get(role)
    effort = (configuration.get('efforts') or {}).get(slot)
    return effort if isinstance(effort, str) and effort in THINKING_MODES else 'auto'


def decide_thinking(mode: str, text: str, *, requested: bool | None = None,
                    structured: bool = False, role: str = '') -> dict:
    mode = mode if isinstance(mode, str) and mode in THINKING_MODES else 'auto'
    # Explicit user modes take precedence over internal JSON/latency hints.
    if mode != 'auto':
        enabled, reason = mode == 'on', 'user_mode'
    elif requested is False:
        enabled, reason = False, 'internal_contract'
    else:
        value = text.casefold()
        enabled = (requested is True or role in {'planner_replanner','long_implementation'} or len(value) > 180
                   or any(term in value for term in ('debug', 'architecture', 'design', 'implement',
                       'compare', 'plan', 'why', 'prove', 'verify', 'calculate', 'tradeoff', 'optimize',
                       'なぜ', '実装', '設計', '比較', '原因', '計画', '検算', '証明', '推論',
                       '矛盾', '最適化', '不具合', 'デバッグ', '分析', '検証', '解いて')))
        reason = 'automatic'
    return {'mode': mode, 'enabled': enabled, 'reason': reason}


def split_thinking(output: str, *, reasoning_open: bool = False) -> tuple[str, str, bool]:
    text = output.strip()
    if text.startswith('<think>'):
        text = text[len('<think>'):]
        reasoning_open = True
    if not reasoning_open:
        return text, '', True
    if '</think>' not in text:
        return '', text.strip(), False
    reasoning, answer = text.split('</think>', 1)
    return answer.strip(), reasoning.strip(), True


def received_reasoning(text, *, kind: str = 'trace', complete: bool = True) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return {'text': text[:MAX_REASONING_CHARS], 'kind': 'summary' if kind == 'summary' else 'trace',
            'complete': bool(complete), 'truncated': len(text) > MAX_REASONING_CHARS}


def reasoning_block(calls: list[dict]) -> dict | None:
    logs = []
    for call in calls:
        raw = call.get('reasoning')
        if not isinstance(raw, dict):
            continue
        log = received_reasoning(raw.get('text'), kind=raw.get('kind'), complete=raw.get('complete', True))
        if log:
            log['truncated'] = log['truncated'] or bool(raw.get('truncated'))
            logs.append({**log, 'model': str(call.get('model') or ''), 'stage': str(call.get('stage') or '')})
    return {'type': 'reasoning_log', 'logs': logs} if logs else None
