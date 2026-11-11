"""OLCR operation questions answered from bundled, hash-verified Graphify sources.

The product guide works without a workspace, external access, or a running LLM.
It does not execute operations and cannot grant permissions or claim live status.
"""
from pathlib import Path
import hashlib
import json
import re
import unicodedata
from .graphify_rag import GraphifyRAG

ROOT=Path(__file__).resolve().with_name('help_knowledge')
GRAPH=GraphifyRAG(enabled=True)


def topics():
    graph=GRAPH._load(ROOT)
    raw=(ROOT/'topics.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=graph['provenance']['source_hashes'].get('topics.json'):
        raise ValueError('stale operation guide topics')
    return json.loads(raw)


def normalize(text: str) -> str:
    value=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    return re.sub(r"\s+",' ',re.sub(r'[-‐‑–—]+',' ',value)).strip()


def contains(query: str, term: str) -> bool:
    term=normalize(term)
    if not term: return False
    # Avoid matching Git in digital, API in capital, etc.
    left=r'(?<![a-z0-9])' if re.match(r'[a-z0-9]',term) else ''
    right=(r'(?:s|es)?(?![a-z0-9])' if re.search(r'[a-z]$',term) and not term.endswith('s') else r'(?![a-z0-9])') if re.search(r'[a-z0-9]$',term) else ''
    return bool(re.search(left+re.escape(term)+right,query))


def matching_topics(text: str, available: list[dict]) -> list[dict]:
    query=normalize(text);scored=[]
    for row in available:
        if row['id']=='overview': continue
        groups=row.get('match_all')
        if groups:
            matches=[max((len(normalize(term)) for term in group if contains(query,term)),default=0) for group in groups]
            if not all(matches): continue
            score=100+sum(matches)
        else:
            score=max((len(normalize(alias)) for alias in row['aliases'] if contains(query,alias)),default=0)
            if not score: continue
        scored.append((score,row))
    ordered=[row for _,row in sorted(scored,key=lambda pair:-pair[0])]
    # A specific article supersedes its broad parent, not unrelated features.
    parents={row.get('parent') for row in ordered if row.get('match_all')}
    return [row for row in ordered if row['id'] not in parents][:3]


def requested(text: str) -> bool:
    if len(text)>2400 or '```' in text or (re.match(r'^\s*/',text) and not re.match(r'^\s*/skill(?:の|とは|って)',text,re.I)):
        return False
    from .mail_chat import matches as mail_operation
    if mail_operation(text):
        return False
    # Quoted command examples in a how-to are questions; actual mutation
    # clauses keep their execution owner, including mixed help + action turns.
    if re.search(r'(?:実装して|修正して|直して|追加して|作って)(?:ください|くれ|ほしい)?(?:[。!！]|$)',text):
        return False
    english_question=bool(re.match(r'^\s*(?:how|why|what|where|can i|please explain)\b',text,re.I))
    if not english_question and re.search(r'\b(?:implement|refactor|fix|build)\b',text,re.I):
        return False
    how=bool(re.search(r'使い方|やり方|手順|見方|どこ|操作方法|方法|どう(?:やって|すれば|使う|する)|するには|できる[？?]|できますか|とは|って何|教えて|ヘルプ|ガイド|できない|使えない|出ない|表示されない|見えない|始まらない|なぜ|違い|上限|いくつ|[？?]|\b(?:how|help|guide|manual|what|why|where|when|which|can i|does|cannot|unavailable|missing)\b',text,re.I))
    product=bool(re.search(r'(?<![a-z0-9_])olcr(?![a-z0-9_])|このアプリ',text,re.I))
    if product and how: return True
    selected=matching_topics(text,topics())
    # A programming subject plus "implementation" does not ask how to operate
    # OLCR. Require a workflow reference before claiming such a question.
    if selected and selected[0]['id']=='coding' and not re.search(
            r'オーケストレーター|依頼|頼|検証結果|進捗|工程|変更ファイル|作業フォルダ|'
            r'\b(?:orchestrator|request|verification results|progress|task|workspace)\b',text,re.I):
        return False
    return how and bool(selected)


def answer(text: str, language: str='ja') -> dict:
    lang='en' if language=='en' else 'ja'
    available=topics()
    selected=matching_topics(text,available)
    if not selected: selected=[next(row for row in available if row['id']=='overview')]
    sections=[];sources=[]
    for row in selected:
        # Search with the public article title: concatenated internal IDs do
        # not tokenize into the source labels that the bundled graph indexes.
        key=row['titles'][lang]
        found,handled=GRAPH.search(key,str(ROOT),1)
        if not handled or not found: raise ValueError('operation guide source unavailable')
        result=found[0];path=Path(result.source)
        # Search verifies the source; recheck after reading to avoid stale excerpts.
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=result.graphify['source_hash']: raise ValueError('stale operation guide source')
        sections.append(raw.decode().strip())
        sources.append({'source':str(path),'line':1,'title':row['titles'][lang],'graphify':result.graphify})
    return {'response':'\n\n'.join(sections),'sources':sources,'response_owner':'OLCR_OPERATION_HELP',
            'knowledge_provenance':{'provider':'graphify','bundled':True,'verified':True,'topics':[row['id'] for row in selected]}}


def identity_response(text: str, language: str = "ja") -> str | None:
    """Exact conversational identity/easter-egg turns, never quoted task data."""
    query = normalize(text).rstrip('。.!?！？').strip()
    if re.fullmatch(r'(?:生命|人生)、?宇宙、?そして万物についての究極の疑問の答え(?:は|を教えて)?|(?:what is )?the answer to (?:the ultimate question of )?life,? the universe,? and everything', query):
        return "42"
    if re.fullmatch(r'olcr(?:とは|って何|は何|とは何ですか|って何ですか)|あなたは(?:何|誰)(?:ですか|なの)?|自己紹介(?:して|してください)|who are you|what are you|what is olcr|introduce yourself', query):
        if language == "en":
            return ("I’m OLCR, your AI agent on this Mac. I can answer questions, research information, "
                    "work with files, help build and fix software, find YouTube videos and music, "
                    "and manage timers and calendar events. What I can use depends on your settings, "
                    "connected models, and permissions. Tell me what you’d like to do.")
        return ("私はOLCR。このMacであなたの作業を手伝うAIエージェントです。質問への回答や調査、"
                "ファイルの操作、アプリの開発・修正、YouTubeの動画や音楽の検索・再生、"
                "タイマーやカレンダーの予定管理などができます。使える機能は、設定や接続したモデル、"
                "許可された権限によって変わります。やりたいことを、そのまま話しかけてください。")
    return None
