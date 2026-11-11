"""Shared request ownership. Search is supporting evidence, never a coding answer."""
from __future__ import annotations
from dataclasses import dataclass
import re
from .coding_tasks import coding_classification_diagnostics, SOFTWARE_TARGET

@dataclass(frozen=True)
class RequestIntent:
    owner: str
    facts: dict

    @property
    def blocks_external_answer(self) -> bool:
        return self.owner in {'CODING', 'CLARIFY_CODING'}

    @property
    def mutation(self) -> bool:
        return self.owner == 'CODING' and bool(self.facts['mutation_intent'])


def resolve_request_intent(text: str, **context) -> RequestIntent:
    """Only the current instruction determines ownership; context supplies scope.

    Domain actions keep their typed executors. Software creation mentioning a
    domain (YouTube/calendar/search/conversion) is still software creation.
    Explicit slash commands retain their command owner; /skill is an envelope.
    """
    value=(text or '').strip()
    skill=re.match(r'^/skill\s+\S+\s+([\s\S]+)$',value,re.I)
    if skill:
        value=skill.group(1)
    facts=coding_classification_diagnostics(value,**context)
    if value.startswith('/'):
        return RequestIntent('COMMAND',facts)
    if not SOFTWARE_TARGET.search(value):
        from .assistant_actions import action_intent
        if action_intent(value):
            return RequestIntent('ASSISTANT_ACTION',facts)
    if facts['classification']=='CODING':
        return RequestIntent('CODING',facts)
    if facts['classification']=='AMBIGUOUS' and facts['mutation_intent']:
        return RequestIntent('CLARIFY_CODING',facts)
    information = re.search(
        r'検索(?:して|してください|する方法)|(?:を|で)検索|調べ|探して|確認して|確認してください|取得して|取得してください|説明|解説|教えて|知りたい|使い方|とは|仕組み|概要|比較|おすすめ|'
        r'\b(?:what|who|where|when|why|how|explain|describe|search|find|recommend|compare|tell|show)\b', value,re.I)
    if (SOFTWARE_TARGET.search(value) and not information
            and not facts['planning_intent'] and not facts['explicit_non_coding']):
        return RequestIntent('CLARIFY_CODING',facts)
    return RequestIntent('GENERAL',facts)
