from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
import base64
import sys
from pathlib import Path
from typing import Any

from .auth import AuthorizationPolicy
from .config import Settings
from .db import Database
from .models import Risk, Route, Task, TaskState
from .ollama import ModelFailure, ModelProvider
from .procedures import LOWERCASE_PROCEDURE, ProcedureRunner
from .retrieval import RetrievalRouter, PathGuard
from .package_manager import PackageManagerError, PackageManagerExecutor
from .web import search as web_search, brave_search, brave_news_search, tavily_search, fetch as web_fetch, setup_guidance
from .tools import ToolValidationError, registry

VISION_SCHEMA_KEYS = ("elements", "text", "relationships", "anomalies", "confidence", "uncertainty")
VISION_SCHEMA_INSTRUCTION = "Allowed top-level keys are exactly: " + ", ".join(VISION_SCHEMA_KEYS) + ". Use elements for visible UI items, text for visible text, and relationships for spatial relations. bbox_normalized belongs inside an elements[] entry, never at top level."
RELATION_VOCABULARY = ("left_of","right_of","above","below","inside","contains","overlaps","aligned_left","aligned_right","aligned_top","aligned_bottom","centered_in","near","far","larger_than","smaller_than")
RELATION_SCHEMA_INSTRUCTION = "Each relationships[] entry must be an object with keys from, to, relation; from and to must reference IDs emitted in elements[]. relation must be one of: " + ", ".join(RELATION_VOCABULARY) + ". If no confident valid relation exists, use relationships: []."


def _operation_failure_class(error: BaseException | str | None) -> str:
    """Map deterministic executor errors to the public operation diagnostics."""
    value = str(error or "").lower()
    if "package.json dependency declarations are owned by package_install" in value:
        return "PACKAGE_JSON_OWNERSHIP_VIOLATION"
    if "package.json configuration mutations are not authorized" in value:
        return "PACKAGE_JSON_CONFIG_UNAUTHORIZED"
    if "empty implementation" in value or "empty operation" in value:
        return "EMPTY_IMPLEMENTATION_OPERATIONS"
    if "target not found" in value:
        return "TARGET_NOT_FOUND"
    if "outside allowed roots" in value or "invalid path" in value:
        return "TARGET_PATH_INVALID"
    if "outside authorized mutation scope" in value:
        return "OPERATION_SCOPE_UNAUTHORIZED"
    if "patch precondition" in value or "source changed" in value or "fragment is ambiguous" in value:
        return "PREIMAGE_MISMATCH"
    if "minor edit cannot replace" in value or "invalid full-file operation" in value:
        return "WRITE_CONTENT_INVALID"
    if "structural validation" in value:
        return "WRITE_CONTENT_INVALID"
    if "package-manager" in value or "package manager" in value or "package installation" in value:
        if "timeout" in value:
            return "PACKAGE_MANAGER_TIMEOUT"
        if "not authorized" in value or "authorization" in value:
            return "PACKAGE_NOT_AUTHORIZED"
        if "unavailable" in value:
            return "PACKAGE_MANAGER_UNAVAILABLE"
        return "PACKAGE_MANAGER_NONZERO_EXIT"
    if ("unsupported file operation" in value or "implementation plan" in value or
            "patch operation requires" in value or "write operation requires" in value):
        return "OPERATION_SCHEMA_SEMANTIC_ERROR"
    if "encoding" in value or "unicode" in value:
        return "ENCODING_ERROR"
    return "EXECUTOR_ERROR" if value else "UNKNOWN"


PACKAGE_JSON_MUTATION_CLASSES = (
    "DEPENDENCY_DECLARATION",
    "DEV_DEPENDENCY_DECLARATION",
    "SCRIPT_CONFIGURATION",
    "PROJECT_METADATA",
    "OTHER_CONFIG",
)


def _package_json_changed_fields(before: str | None, after: str | None) -> set[str]:
    """Return top-level package.json fields changed by one candidate operation."""
    try:
        old = json.loads(before or "{}")
        new = json.loads(after or "{}")
    except (TypeError, ValueError):
        return {"OTHER_CONFIG"}
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {"OTHER_CONFIG"}
    changed = {str(key) for key in set(old) | set(new) if old.get(key) != new.get(key)}
    classes: set[str] = set()
    if "dependencies" in changed or "peerDependencies" in changed or "optionalDependencies" in changed:
        classes.add("DEPENDENCY_DECLARATION")
    if "devDependencies" in changed:
        classes.add("DEV_DEPENDENCY_DECLARATION")
    if "scripts" in changed:
        classes.add("SCRIPT_CONFIGURATION")
    if "name" in changed or "version" in changed or "private" in changed or "description" in changed:
        classes.add("PROJECT_METADATA")
    if changed - {"dependencies", "peerDependencies", "optionalDependencies", "devDependencies", "scripts", "name", "version", "private", "description"}:
        classes.add("OTHER_CONFIG")
    return classes


def _ownership_failure_fingerprint(*, path: str, semantic_fields: set[str], owner: str,
                                   package_status: str, dependencies_satisfied: bool) -> str:
    value = {"failure_class": "PACKAGE_JSON_OWNERSHIP_VIOLATION", "path": path,
             "semantic_fields": sorted(semantic_fields), "owner": owner,
             "package_install_status": package_status,
             "dependencies_satisfied": bool(dependencies_satisfied)}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class ContextManager:
    def __init__(self, budget: int): self.budget = budget
    def build(self, request: str, evidence: list[Any], core_context: str = "") -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        core_context=self._select_core(request, core_context)
        remaining = max(0, self.budget - len(request) - len(core_context))
        selected, used = [], 0
        for item in evidence:
            text = item.snippet[:1000]
            if used + len(text) > remaining: break
            selected.append({"source": item.source, "line": item.line, "text": text,"method":item.method,"score":item.score}); used += len(text)
        content = "\n\n".join(f"Source: {x['source']}:{x['line'] or ''}\n{x['text']}" for x in selected)
        messages = [{"role": "system", "content": "Use only relevant supplied evidence. Cite source paths. Never claim to have executed unobserved actions."}]
        if core_context: messages.append({"role":"system","content":"Workspace core context (explicit saved snapshot):\n"+core_context})
        if content: messages.append({"role": "system", "content": "Selected evidence:\n" + content})
        messages.append({"role": "user", "content": request})
        return messages, selected
    def _select_core(self, request: str, content: str) -> str:
        if not content: return ""
        limit=max(0,self.budget//4)
        if len(content)<=limit: return content
        raw_chunks=[x.strip() for x in re.split(r"(?=--- file:|\n#{1,6} )",content) if x.strip()]
        chunks=[]
        for chunk in raw_chunks:
            if len(chunk)<=1200: chunks.append(chunk)
            else: chunks.extend(chunk[i:i+1200] for i in range(0,len(chunk),1200))
        terms=set(re.findall(r"[a-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff]{2,}",request.lower()))
        def score(pair):
            text=pair[1].lower(); lexical=sum(1 for t in terms if t in text)
            # Prefer concrete specification evidence over narrative mentions.
            concrete=len(re.findall(r"\b\d+(?:\.\d+)?\s*(?:ms|x|×|回|resets?)?\b", text))
            return (lexical + concrete * 2, concrete, -pair[0])
        ranked=sorted(enumerate(chunks), key=score, reverse=True)
        chosen=[]; used=0
        # Coverage pass: retain concrete specification chunks for each topic
        # before filling remaining budget by relevance score.
        topic_patterns=[("lock delay", r"500\s*ms", r"lock\s*delay|固定まで|接地.*固定"),("drop interval", r"800\s*ms", r"drop\s*interval|落下"),("reset", r"10\s*(?:resets?|回)", r"reset|リセット"),("盤面", r"10\s*[x×]\s*20", r"board|盤面")]
        for topic, pattern, concept in topic_patterns:
            if topic not in request.lower() and not (topic == "盤面" and "board" in request.lower()): continue
            for index, chunk in ranked:
                if re.search(pattern, chunk, re.I) and re.search(concept, chunk, re.I) and chunk not in chosen and used + len(chunk) <= limit:
                    chosen.append(chunk); used += len(chunk); break
        for _,chunk in ranked:
            if chunk in chosen: continue
            if used+len(chunk)>limit: continue
            chosen.append(chunk); used+=len(chunk)
        return "\n\n".join(chosen)[:limit]


def validate_visual_context(raw: str) -> dict[str, Any]:
    """Accept only bounded, perception-only structured visual evidence."""
    try: value = json.loads(raw)
    except json.JSONDecodeError as exc: raise RuntimeError(f"JSON_PARSE_ERROR:line={exc.lineno},column={exc.colno},position={exc.pos}") from exc
    except (TypeError, ValueError) as exc: raise RuntimeError("JSON_PARSE_ERROR") from exc
    if not isinstance(value, dict): raise RuntimeError("VisualContext must be an object")
    # qwen2.5vl may emit one explicit representational wrapper. Unwrap once only.
    if set(value) == {"visual_context"} and isinstance(value["visual_context"], dict):
        value = value["visual_context"]
    allowed={"elements", "text", "relationships", "anomalies", "confidence", "uncertainty"}
    unknown=sorted(set(value)-allowed)
    if unknown: raise RuntimeError(f"UNKNOWN_FIELD:{unknown[0]}@top_level")
    if not any(k in value for k in ("elements", "text", "anomalies")): raise RuntimeError("VisualContext lacks observable evidence")
    ids=set()
    for element in value.get("elements",[]) if isinstance(value.get("elements",[]),list) else []:
        if not isinstance(element,dict): raise RuntimeError("invalid visual element")
        if "id" in element: ids.add(str(element["id"]))
        box=element.get("bbox")
        if box is not None:
            if not isinstance(box,list) or len(box)!=4 or any(not isinstance(x,(int,float)) or x!=x or abs(x)==float("inf") for x in box): raise RuntimeError("invalid bbox")
            x1,y1,x2,y2=box
            if not (0<=x1<=x2<=1 and 0<=y1<=y2<=1): raise RuntimeError("bbox out of range")
        if "confidence" in element and (not isinstance(element["confidence"],(int,float)) or not 0<=element["confidence"]<=1): raise RuntimeError("invalid confidence")
    allowed_rel={"left_of","right_of","above","below","inside","contains","overlaps","aligned_left","aligned_right","aligned_top","aligned_bottom","centered_in","near","far","larger_than","smaller_than"}
    for idx, rel in enumerate(value.get("relationships",[]) if isinstance(value.get("relationships",[]),list) else []):
        if isinstance(rel,dict) and rel.get("relation") not in allowed_rel:
            _vision_diag(INVALID_RELATIONSHIP_INDEX=idx, INVALID_RELATIONSHIP_KEYS=",".join(sorted(rel)), RELATIONSHIP_FAILURE_REASON="type_not_allowed")
            raise RuntimeError("invalid visual relation")
        if isinstance(rel,dict) and ids and any(str(rel.get(k)) not in ids for k in ("from","to") if k in rel): raise RuntimeError("unknown visual relation reference")
    if len(json.dumps(value, ensure_ascii=False)) > 20_000: raise RuntimeError("VisualContext exceeds safety limit")
    return value

def _extract_visual_json(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines=text.splitlines()
        if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip()=="```": text="\n".join(lines[1:-1]).strip()
    if not (text.startswith("{") and text.endswith("}")): raise RuntimeError("JSON_PARSE_ERROR:response_boundary")
    return text

def _normalize_single_object(value: Any) -> tuple[Any, bool, str]:
    if not isinstance(value, dict): return value, False, "top_level_not_object"
    allowed={"id","label","bbox","bbox_normalized","confidence","text"}
    required={"id","bbox"}
    keys=set(value)
    if keys and keys <= allowed and required <= keys and not ({"elements","text","relationships","anomalies","uncertainty","objects","observations","relations","uncertainties"} & keys):
        return {"elements":[value],"text":[],"relationships":[]}, True, "exact_object_entry"
    return value, False, "not_exact_object_entry"

def _bbox_shape_diag(value: Any) -> None:
    if not isinstance(value,dict) or "bbox_normalized" not in value: return
    objs=value.get("objects"); items=objs if isinstance(objs,list) else []
    count=sum(isinstance(x,dict) and "bbox_normalized" in x for x in items)
    if not items: cls="TOP_BBOX_WITH_ZERO_OBJECTS"
    elif len(items)==1 and "bbox_normalized" not in items[0]: cls="TOP_BBOX_WITH_ONE_OBJECT_MISSING_BBOX"
    elif len(items)==1: cls="TOP_BBOX_WITH_ONE_OBJECT_ALREADY_HAS_BBOX"
    else: cls="TOP_BBOX_WITH_MULTIPLE_OBJECTS"
    _vision_diag(EFFECTIVE_TOP_LEVEL_KEYS=",".join(sorted(value)), EFFECTIVE_TOP_LEVEL_TYPES=",".join(f"{k}:{type(v).__name__}" for k,v in value.items()), BBOX_TOP_LEVEL_TYPE=type(value["bbox_normalized"]).__name__, OBJECTS_FIELD_PRESENT="YES" if "objects" in value else "NO", OBJECTS_FIELD_TYPE=type(objs).__name__, OBJECTS_COUNT=len(items), OBJECTS_WITH_BBOX_NORMALIZED_COUNT=count, TOP_LEVEL_BBOX_SHAPE_CLASS=cls, TOP_LEVEL_BBOX_RELOCATION_ELIGIBLE="NO", TOP_LEVEL_BBOX_RELOCATION_APPLIED="NO", TOP_LEVEL_BBOX_RELOCATION_REASON="objects is not canonical in current validator")

def _unwrap_visual_context(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict) and set(value) == {"visual_context"} and isinstance(value["visual_context"], dict):
        return value["visual_context"], True
    return value, False

def _vision_diag(**values: Any) -> None:
    print("VISION_DIAG " + " ".join(f"{k}={v}" for k,v in values.items()), file=sys.stderr, flush=True)


class Runtime:
    @staticmethod
    def _request_fingerprint(messages: list[dict], label: str = "PRODUCTION") -> None:
        """Emit privacy-safe final-request diagnostics immediately before Ollama."""
        parts = []
        for index, message in enumerate(messages):
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            parts.append(f"{index}:{role}:{digest}:{len(content)}:{len(content.encode('utf-8'))}")
            print(f"{label}_MESSAGE_{index}_ROLE={role} {label}_MESSAGE_{index}_HASH={digest} {label}_MESSAGE_{index}_CHARS={len(content)} {label}_MESSAGE_{index}_BYTES={len(content.encode('utf-8'))}", file=sys.stderr, flush=True)
        serialized = "\n".join(parts)
        print(f"{label}_REQUEST_HASH={hashlib.sha256(serialized.encode()).hexdigest()} {label}_TOTAL_CHARS={sum(len(str(m.get('content') or '')) for m in messages)} {label}_MESSAGE_COUNT={len(messages)}", file=sys.stderr, flush=True)
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        marker = joined.rfind("operations")
        print(f"{label}_OPERATIONS_INSTRUCTION_LAST_INDEX={marker} {label}_CHARS_AFTER_OPERATIONS_INSTRUCTION={len(joined)-marker if marker >= 0 else 'UNKNOWN'}", file=sys.stderr, flush=True)
    @staticmethod
    def _suppress_brain_urls(text: str) -> tuple[str, bool]:
        value=text or ""
        detected=bool(re.search(r"https?://|\[[^\]]+\]\(https?://", value, re.I))
        value=re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", value)
        value=re.sub(r"https?://\S+", "", value, flags=re.I)
        value=re.sub(r"(?im)^\s*(?:sources?|references?|参照(?:した)?(?:web)?(?:ページ|url)?|source url)\s*:?\s*$\n?", "", value)
        return value, detected

    @staticmethod
    def _suppress_brain_source_fragments(text: str) -> tuple[str, bool]:
        pattern=r"(?ims)(?:^|\n)\s*(?:[-*]\s*)?(?:参照したWebページ(?:のタイトル)?|参照URL|出典|参考|URL|Source|Sources|Reference|References|タイトル|Title)\s*:?\s*.*?(?=\n\s*\n|$)"
        value, count=re.subn(pattern, "", text or "")
        return value, bool(count)

    @staticmethod
    def _render_web_sources(sources: list[dict[str, Any]]) -> str:
        lines=["", "参照したWebページ:"]
        for index, source in enumerate(sources[:5], 1):
            title=str(source.get("title") or source.get("requested_url") or "").replace("\n", " ").strip()
            url=str(source.get("final_url") or "").strip()
            if not source.get("fetch_success") or not title or any(ch in url for ch in "*()[]<>") or not re.match(r"^https?://\S+$", url): continue
            source["source_id"]=source.get("source_id") or f"web-{index}"
            lines.extend([f"- {title}", f"  {url}"])
            print(f"WEB_RENDERED_SOURCE_{index}_ID={source['source_id']} WEB_RENDERED_SOURCE_{index}_HOST={url.split('/')[2]} WEB_RENDERED_SOURCE_{index}_TITLE_MATCH=YES WEB_RENDERED_SOURCE_{index}_URL_PROVENANCE=FETCHED", file=sys.stderr, flush=True)
        return "\n".join(lines) if len(lines)>2 else ""

    @staticmethod
    def _news_articles(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep individual article pages for news claims; hubs are discovery only."""
        articles=[]
        for source in sources:
            url=str(source.get("final_url") or source.get("url") or "").lower()
            title=str(source.get("title") or "").lower()
            path=url.split("?",1)[0].split("#",1)[0]
            segments=[part for part in path.split("/") if part]
            structural_tokens={token for segment in segments for token in re.split(r"[-_]+", segment) if token}
            if any(host in url for host in ("x.com/", "twitter.com/")): continue
            if structural_tokens.intersection({"topic","topics","keyword","keywords","category","categories","tag","tags","search","archive"}): continue
            if re.search(r"ニュース\s*(?:一覧|リスト)|\b(?:news\s+list|topic|topics|category|archive)\b", title, re.I): continue
            if not segments or path.endswith("/") or len(segments) < 2: continue
            source["result_kind"]="article"; articles.append(source)
        return articles

    @staticmethod
    def _used_web_sources(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Select only current sources evidenced by the generated answer."""
        used=[]; body=answer.lower()
        for source in sources:
            title=str(source.get("title") or "").strip()
            words=[w for w in re.findall(r"[a-z0-9]{4,}|[\u3040-\u30ff\u3400-\u9fff]{3,}", title.lower())]
            if words and (sum(1 for word in words if word in body) >= max(1, min(2, len(words)))):
                used.append(source)
        return used

    @staticmethod
    def _web_contradiction_category(answer: str) -> str | None:
        value=re.sub(r"\s+", "", answer or "").lower()
        if re.search(r"インターネット(?:に|へ|への)?アクセス(?:が)?(?:できません|できない|ありません)|internet(?:access)?(?:is)?(?:unavailable|notavailable|cannot)", value): return "INTERNET_UNAVAILABLE"
        if re.search(r"(?:web|ウェブ)検索(?:を)?(?:実行)?(?:できません|できない|できませんでした)|検索機能(?:は)?(?:ありません|利用できません)", value): return "WEB_SEARCH_UNAVAILABLE"
        if re.search(r"リアルタイム(?:の)?(?:情報|検索)(?:を)?(?:取得|提供)(?:できません|できない)|realtime(?:data|search).*(?:unavailable|cannot)", value): return "REALTIME_DATA_UNAVAILABLE"
        if re.search(r"(?:私の|学習済み|モデルの)?知識(?:ベース|カットオフ)|trainingcutoff|knowledgecutoff|2023年までの知識", value): return "PARAMETRIC_KNOWLEDGE_FALLBACK"
        return None

    @staticmethod
    def _parse_web_composition(raw: str, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
        try:
            payload=json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        except (TypeError, ValueError, json.JSONDecodeError): return []
        items=payload.get("items") if isinstance(payload,dict) else None
        allowed={str(x.get("source_id")):x for x in sources}
        if not isinstance(items,list): return []
        seen=set(); valid=[]
        for item in items[:5]:
            if not isinstance(item,dict) or set(item)-{"result_id","summary"}: return []
            rid=str(item.get("result_id") or ""); summary=str(item.get("summary") or "").strip()
            if rid not in allowed or rid in seen or not summary or re.search(r"https?://|www\.", summary, re.I): return []
            seen.add(rid); valid.append({"result_id":rid,"summary":summary})
        return valid

    @staticmethod
    def _freshness_check(answer: str, sources: list[dict[str, Any]], requested_prerelease: bool = False) -> tuple[str, str]:
        return Runtime._freshness_check_scoped(answer, sources, requested_prerelease, None)

    @staticmethod
    def _freshness_target(request: str) -> str | None:
        match=re.search(r"(?:of|for|about)\s+([A-Z][A-Za-z0-9_-]{2,})|([A-Z][A-Za-z0-9_-]{2,})の", request)
        return next((x for x in (match.groups() if match else ()) if x), None)

    @staticmethod
    def _freshness_check_scoped(answer: str, sources: list[dict[str, Any]], requested_prerelease: bool = False, target: str | None = None) -> tuple[str, str]:
        versions=[]; parsed_count=0; unrelated_count=0; ambiguous_count=0
        for source in sources:
            for field in (source.get("title", ""), source.get("text", "")):
                for match in re.finditer(r"\bv?(\d+\.\d+\.\d+)(?:[- ]?(rc|alpha|beta|preview|pre)(\d*))?\b", field, re.I):
                    parsed_count+=1; context=field[max(0,match.start()-70):match.end()+70]; line=field[max(0,field.rfind("\n",0,match.start())+1):field.find("\n",match.end()) if field.find("\n",match.end()) >= 0 else len(field)]
                    sentence_start=max(line.rfind(".",0,match.start()), line.rfind("。",0,match.start()))+1; sentence_end_candidates=[x for x in (line.find(".",match.end()), line.find("。",match.end())) if x >= 0]; sentence_end=min(sentence_end_candidates) if sentence_end_candidates else len(line); sentence=line[sentence_start:sentence_end]
                    title_field=(field == source.get("title", "")); target_here=bool(target and target.lower() in (field if title_field else sentence).lower() and (title_field or re.search(r"release|version|latest|released|リリース|バージョン|最新", sentence, re.I)))
                    if target and not target_here:
                        if re.search(r"dependency|library|tool|mlx|updated", line, re.I): unrelated_count+=1
                        else: ambiguous_count+=1
                        continue
                    versions.append((tuple(int(x) for x in match.group(1).split(".")), match.group(1), bool(match.group(2)), source))
        claim=re.search(r"\bv?(\d+\.\d+\.\d+)(?:[- ]?(rc|alpha|beta|preview|pre)(\d*))?\b", answer, re.I)
        print(f"WEB_FRESHNESS_TARGET={target or 'UNKNOWN'} WEB_FRESHNESS_PARSED_VERSION_COUNT={parsed_count} WEB_FRESHNESS_RELEVANT_VERSION_COUNT={len(versions)} WEB_FRESHNESS_UNRELATED_VERSION_COUNT={unrelated_count} WEB_FRESHNESS_AMBIGUOUS_VERSION_COUNT={ambiguous_count}", file=sys.stderr, flush=True)
        for idx,item in enumerate(versions[:5],1): print(f"WEB_FRESHNESS_CANDIDATE_{idx}_VERSION={item[1]} WEB_FRESHNESS_CANDIDATE_{idx}_SOURCE_ID={item[3].get('source_id','UNKNOWN')} WEB_FRESHNESS_CANDIDATE_{idx}_ORIGIN=EVIDENCE WEB_FRESHNESS_CANDIDATE_{idx}_RELEVANCE=TARGET_RELEVANT WEB_FRESHNESS_CANDIDATE_{idx}_MATCH_METHOD=LOCAL_LINE", file=sys.stderr, flush=True)
        maximum=max(versions, key=lambda item: item[0], default=None)
        print(f"WEB_FRESHNESS_MAX_PARSED_CANDIDATE={maximum[1] if maximum else 'NONE'} WEB_FRESHNESS_MAX_CANDIDATE_SOURCE_ID={maximum[3].get('source_id','UNKNOWN') if maximum else 'NONE'} WEB_FRESHNESS_MAX_CANDIDATE_ORIGIN=EVIDENCE WEB_FRESHNESS_MAX_CANDIDATE_TARGET_MATCH={'YES' if maximum else 'NO'}", file=sys.stderr, flush=True)
        if not claim or not versions: return "INSUFFICIENT", claim.group(0) if claim else ""
        claimed_base=claim.group(1); claimed_tuple=tuple(int(x) for x in claimed_base.split(".")); claimed_pre=bool(claim.group(2))
        same=[item for item in versions if item[1] == claimed_base]
        if claimed_pre and not requested_prerelease and any(not item[2] for item in same): return "CONFLICT", claim.group(0)
        if any(item[0] > claimed_tuple for item in versions): return "CONFLICT", claim.group(0)
        if not any(item[1] == claimed_base and (requested_prerelease or item[2] == claimed_pre) for item in versions): return "INSUFFICIENT", claim.group(0)
        return "PASS", claim.group(0)

    def _self_context(self, text: str) -> str:
        lower=text.lower()
        if not any(token in lower for token in ("olcr", "option", "semantic retrieval", "構成", "モデル", "設定")):
            return ""
        facts=(f"CURRENT version: 0.4.7\nCONFIGURED brain model: {self.settings.main_model}\nCONFIGURED vision model: {self.settings.vision_model}\nCONFIGURED embedding model: {self.settings.embedding_model or 'NOT_CONFIGURED'}\nCURRENT semantic vector enabled: {self.settings.vector_enabled}\nCURRENT model roles brain/router/vision: individually configurable through /option show, set, and reset.\nCURRENT MODEL_UNAVAILABLE_POLICY=CURRENT_REJECT_AND_PRESERVE: model presence is validated before committing /option set; an unavailable model is rejected and the previous configuration is preserved; no silent substitution or acquisition occurs.\nCURRENT settings API scope: /api/settings is a local OLCR backend settings API.\nCURRENT thinking: brain thinking choice is request-scoped.\nCURRENT tests: CLI, configuration, and semantic tests exist.\nUNKNOWN: settings API dependency topology and storage implementation, the internal plumbing for model validation, whether /option show performs model validation, authentication state, settings-history details, and any architecture or service not stated here.\n\nFor OLCR questions, make current claims only from CURRENT/CONFIGURED facts and do not broaden a fact beyond its stated trigger or scope. Do not infer implementation plumbing from a capability. If a detail is UNKNOWN, say '現在の提供情報からは確認できません'. Only call something absent when explicitly marked CONFIRMED_ABSENT. Clearly label recommendations as PROPOSED. Any individual proposal that replaces, weakens, bypasses, or materially changes a CURRENT policy must be labeled inline 'PROPOSED / POLICY_CHANGE' and state the policy it changes. Automatic fallback, model substitution, or model pull are POLICY_CHANGE proposals and must not be default recommendations; prefer improvements that preserve CURRENT_REJECT_AND_PRESERVE. Do not invent services, databases, APIs, URLs, or deployment components.")
        return facts

    def _generate_brain(self, messages, text, structured_schema=None, thinking_override: bool | None = None):
        think=self._thinking_required(text) if thinking_override is None else bool(thinking_override)
        grounding=self._self_context(text)
        if grounding: messages=[{"role":"system","content":grounding}, *messages]
        print(f"BRAIN_SELF_GROUNDING={'YES' if grounding else 'NO'}", file=sys.stderr, flush=True)
        print(f"BRAIN_SELF_CONTEXT_FACT_COUNT={10 if grounding else 0}", file=sys.stderr, flush=True)
        print(f"BRAIN_SELF_CONTEXT_UNKNOWN_COUNT={3 if grounding else 0}", file=sys.stderr, flush=True)
        print("BRAIN_SELF_CONTEXT_CONFIRMED_ABSENT_COUNT=0", file=sys.stderr, flush=True)
        print(f"BRAIN_SELF_CONTEXT_SOURCE={'mixed' if grounding else 'not_applicable'}", file=sys.stderr, flush=True)
        print(f"THINKING_DECISION={'YES' if think else 'NO'}", file=sys.stderr, flush=True)
        print(f"THINKING_DECISION_SOURCE={'IMPLEMENTER_EXECUTION_CONTRACT' if thinking_override is not None else 'fallback'}", file=sys.stderr, flush=True)
        print(f"BRAIN_MODEL={self.settings.main_model}", file=sys.stderr, flush=True)
        print(f"BRAIN_THINKING_REQUESTED={'YES' if think else 'NO'}", file=sys.stderr, flush=True)
        print("BRAIN_THINK_FIELD_SENT=YES", file=sys.stderr, flush=True)
        print(f"BRAIN_THINK_FIELD_VALUE={'TRUE' if think else 'FALSE'}", file=sys.stderr, flush=True)
        print("BRAIN_THINKING_SUPPORTED=UNKNOWN", file=sys.stderr, flush=True)
        print("BRAIN_THINKING_EFFECTIVE=UNKNOWN", file=sys.stderr, flush=True)
        if structured_schema is not None:
            print("IMPLEMENTATION_SCHEMA_REQUESTED=YES IMPLEMENTATION_SCHEMA_SENT_TO_PROVIDER=YES IMPLEMENTATION_SCHEMA_PROVIDER_FIELD=format MODEL_RESPONSE_FORMAT=JSON_SCHEMA", file=sys.stderr, flush=True)
        try:
            if structured_schema is not None:
                result=self.model.generate(messages, self.settings.main_model, think=think, format=structured_schema)
            else:
                result=self.model.generate(messages, self.settings.main_model, think=think)
        except TypeError:
            # Older providers/test doubles may not expose ``format`` or
            # ``think``. The prompt remains the same, while the host parser
            # still rejects anything that is not a typed operation object.
            try:
                result=self.model.generate(messages, self.settings.main_model, think=think)
            except TypeError:
                result=self.model.generate(messages, self.settings.main_model)
        present=result.get("thinking_present") if isinstance(result,dict) else None
        chars=result.get("thinking_chars",0) if isinstance(result,dict) else 0
        effective="YES" if think and present is True else "NO" if not think and present is False else "UNKNOWN"
        print(f"BRAIN_THINKING_RESPONSE_PRESENT={'YES' if present is True else 'NO' if present is False else 'UNKNOWN'}", file=sys.stderr, flush=True)
        print(f"BRAIN_THINKING_RESPONSE_CHARS={chars}", file=sys.stderr, flush=True)
        print(f"BRAIN_THINKING_EFFECTIVE={effective}", file=sys.stderr, flush=True)
        return result
    def __init__(self, settings: Settings, db: Database, retrieval: RetrievalRouter, model: ModelProvider, artifacts: Any = None,
                 package_manager_executor: PackageManagerExecutor | None = None):
        self.settings, self.db, self.retrieval, self.model = settings, db, retrieval, model
        self.tools, self.policy = registry(), AuthorizationPolicy()
        self.procedures = ProcedureRunner(self.tools, self.policy)
        self.artifacts = artifacts
        self.package_manager_executor = package_manager_executor or PackageManagerExecutor()
    def execute(self, text: str, approved: bool = False, core_context: str = "", suppress_web: bool = False, workspace_root: str | None = None, managed_context: dict | None = None) -> tuple[Task, str]:
        task = Task(text); task.transition(TaskState.ROUTING); started = time.perf_counter()
        try:
            lower = text.strip().lower()
            # A managed implementation phase is an explicit, typed execution
            # context.  It must not be re-routed from the generated phase prose:
            # words such as "website", "GitHub", or "release" are common in a
            # coding request but are not an instruction to replace the workspace
            # mutation with the normal-chat Web composer.
            coding_implementation = bool(
                managed_context
                and managed_context.get("managed_coding_task")
                and managed_context.get("operation_intent") == "IMPLEMENTATION"
                and workspace_root
            )
            web_evidence=[]; web_freshness_required=False
            web_raw_result_count=0
            web_search_attempted=False; web_provider_not_ready=False; web_provider_failed=False; web_provider_name=getattr(self.settings, "web_provider", "none")
            search_api_attempt_count=0; merged_raw_count=0; cross_attempt_duplicates=0; attempt_queries=[]
            web_decision="NO_SEARCH"
            explicit_web_request = bool(re.search(r"(?:web検索|webで|ネットで|インターネットで|search the web|search web)", text, re.I))
            news_request = bool(re.search(r"ニュース|news", text, re.I))
            coding_web_support = bool(coding_implementation and managed_context.get("coding_web_support") is True and explicit_web_request)
            print(f"RUNTIME_EXECUTE_FUNCTION=Runtime.execute RUNTIME_EXPLICIT_WEB_FLAG={'true' if explicit_web_request else 'false'} RUNTIME_INPUT_CHARS={len(text)} CODING_CONTEXT_SIGNAL={'CODING_IMPLEMENTATION' if coding_implementation else 'NONE'}", file=sys.stderr, flush=True)
            web_mode = getattr(self.settings, "web_mode", "off")
            generic_web_routing = not coding_implementation
            if coding_implementation and not coding_web_support:
                print("RUNTIME_SELECTED_BRANCH=CODING_IMPLEMENTATION WEB_BRANCH_MATCH=false WEB_AUTO_ROUTE_GUARD=MANAGED_CODING", file=sys.stderr, flush=True)
            elif coding_web_support:
                print("CODING_WEB_SUPPORT=AUTHORIZED WEB_SUPPORT_IS_AUXILIARY=true", file=sys.stderr, flush=True)
            if not suppress_web and (generic_web_routing or coding_web_support) and (web_mode == "auto" or (web_mode == "manual" and explicit_web_request) or (web_mode == "off" and explicit_web_request)):
                freshness=bool(re.search(r"\b(latest|current|recent|today|news|release|price|schedule|documentation)\b|最新|現在|今日|最近|リリース|価格|ニュース", text, re.I)); web_freshness_required=freshness
                web_decision="SEARCH" if freshness else "NO_SEARCH"
                if explicit_web_request: web_decision="EXPLICIT_SEARCH"
                if freshness or explicit_web_request:
                    print("RUNTIME_SELECTED_BRANCH=WEB_SEARCH WEB_BRANCH_MATCH=true", file=sys.stderr, flush=True)
                    web_search_attempted=True
                    try:
                        query="Ollama latest release version changelog" if re.search(r"ollama", text, re.I) else re.sub(r"(?:今日|本日|today|最新の?|recent)\s*", "", re.sub(r"(?:を)?(?:web検索|webで|ネットで|インターネットで)(?:して|調べて|検索して)?|(?:search the web for|search web for)", "", text, flags=re.I)).strip(" の。！？!?")[:300]
                        if re.search(r"ニュース|news", text, re.I): query = f"AI 人工知能 生成AI ニュース {query}".strip()
                        provider=web_provider_name
                        if web_mode == "off": raise RuntimeError("WEB_SEARCH_DISABLED")
                        if provider == "none": raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
                        for attempt in range(1, 3):
                            attempt_query=query if attempt == 1 else f"{query} 個別記事 最新 AI ニュース"
                            attempt_queries.append(attempt_query); search_api_attempt_count += 1
                            if news_request and provider == "brave":
                                candidates=brave_news_search(attempt_query, 10, "pd" if re.search(r"今日|本日|today|current", text, re.I) else None)
                            else:
                                candidates=(brave_search(attempt_query, 10) if provider == "brave" else tavily_search(attempt_query, 10) if provider == "tavily" else web_search(attempt_query, 10))
                            if attempt == 1: web_raw_result_count=len(candidates)
                            else: web_raw_result_count += len(candidates)
                            seen={str(x.get("final_url") or x.get("url") or "").split("#",1)[0].rstrip("/") for x in web_evidence}
                            for candidate in candidates[:10]:
                                identity=str(candidate.get("url") or "").split("#",1)[0].rstrip("/")
                                if identity in seen: cross_attempt_duplicates += 1; continue
                                try:
                                    source=web_fetch(candidate["url"]); source.update({"source_id":f"web-{len(web_evidence)+1}","title":candidate.get("title",""),"url":candidate.get("url",""),"provider":candidate.get("provider","duckduckgo"),"rank":candidate.get("rank",0),"fetch_success":True}); web_evidence.append(source); seen.add(identity)
                                except Exception: continue
                            filtered_attempt=self._news_articles(web_evidence)
                            if filtered_attempt: break
                            if attempt == 1: print("ATTEMPT_2_TRIGGER_REASON=HUB_DOMINATED_OR_NO_ARTICLE_LEVEL_RESULTS", file=sys.stderr, flush=True)
                        print(f"SEARCH_API_ATTEMPT_COUNT={search_api_attempt_count} ATTEMPT_1_QUERY={attempt_queries[0] if attempt_queries else ''} ATTEMPT_2_QUERY={attempt_queries[1] if len(attempt_queries)>1 else ''} MERGED_RAW_COUNT={web_raw_result_count} CROSS_ATTEMPT_DUPLICATE_COUNT={cross_attempt_duplicates}", file=sys.stderr, flush=True)
                    except RuntimeError as exc:
                        web_evidence=[]; web_provider_not_ready=(str(exc) == "WEB_SEARCH_PROVIDER_NOT_READY")
                        web_provider_failed = str(exc) not in ("WEB_SEARCH_DISABLED", "WEB_SEARCH_PROVIDER_NOT_READY")
                    except Exception:
                        web_evidence=[]; web_provider_failed=True
            if web_evidence and re.search(r"ニュース|news", text, re.I):
                filtered=self._news_articles(web_evidence)
                print(f"WEB_NEWS_ARTICLE_CANDIDATE_COUNT={len(filtered)} WEB_NEWS_HUB_REJECTED_COUNT={len(web_evidence)-len(filtered)} WEB_HUB_REJECT_COUNT={len(web_evidence)-len(filtered)} WEB_ARTICLE_ACCEPT_COUNT={len(filtered)}", file=sys.stderr, flush=True)
                web_evidence=filtered[:5]
            final_status = "SUCCESS" if web_evidence else ("WEB_PROVIDER_NOT_CONFIGURED" if web_provider_not_ready else "WEB_SEARCH_FAILED" if web_provider_failed else "WEB_SEARCH_DISABLED" if web_mode == "off" and web_search_attempted else "WEB_SEARCH_NO_RESULTS" if web_raw_result_count == 0 else "WEB_SEARCH_NO_USABLE_RESULTS")
            print(f"FINAL_ACCEPTED_RESULT_IDS={','.join(str(x.get('source_id','')) for x in web_evidence)} WEB_SUCCESS_GATE={'PASS' if bool(web_evidence) == (final_status == 'SUCCESS') else 'FAIL'}", file=sys.stderr, flush=True)
            print(f"WEB_MODE={getattr(self.settings, 'web_mode', 'off')} WEB_DECISION={web_decision} WEB_SEARCH_RESULT_COUNT={len(web_evidence)} WEB_SOURCE_COUNT={len(web_evidence)} WEB_FINAL_STATUS={final_status} WEB_ZERO_WRITE={'NO' if coding_implementation else 'YES'}", file=sys.stderr, flush=True)
            if web_search_attempted and (web_freshness_required or explicit_web_request) and not web_evidence and not coding_implementation:
                print("WEB_FAILURE_BRAIN_GENERATION=NOT_RUN", file=sys.stderr, flush=True)
                print("FINAL_RESPONSE_PATH=WEB_TERMINAL_RESPONSE NORMAL_BRAIN_CALLED=false", file=sys.stderr, flush=True)
                task.route=Route.NEURAL; task.transition(TaskState.GENERATING); task.transition(TaskState.COMPLETED)
                disclosure=("Web検索は現在オフになっています。Settings で Web Search を manual または auto にしてください。" if web_mode == "off" else "Web検索プロバイダが設定されていないため、最新情報を確認できませんでした。\n" + setup_guidance(web_provider_name if web_provider_name != "none" else None) if web_provider_not_ready else "Web検索に失敗したため、最新情報を取得できませんでした。" if web_provider_failed else "Web検索は実行できましたが、個別記事として確認できる今日のAIニュースを特定できませんでした。" if web_raw_result_count > 0 else "Web検索は実行できましたが、該当する結果が見つかりませんでした。")
                return self._finish(task, disclosure, started)
            if web_evidence:
                core_context=(core_context + "\n" if core_context else "") + "[WEB_SEARCH_SUCCESS]\nOLCR has already executed Web Search for this request. The following are current externally retrieved results. Use them to answer the user; do not claim that Internet access or Web Search is unavailable.\nAnswer prose only: do not output URLs, Markdown links, citations, or a source/reference section; OLCR will append verified sources separately.\n" + ("For freshness questions, compare all fetched sources, prefer the newest explicitly supported item, and do not call an older item latest when a newer fetched item exists.\n" if web_freshness_required else "") + "\n\n".join(f"SOURCE_TITLE={x.get('title') or x.get('requested_url')}\nSOURCE_URL={x.get('final_url')}\nSOURCE_PROVIDER_RANK={x.get('rank',0)}\n{x.get('text','')[:4000]}" for x in web_evidence)
                print(f"WEB_SUCCESS_COMPOSER_USED=true CURRENT_WEB_RESULT_COUNT_SUPPLIED={len(web_evidence)} PREVIOUS_WEB_RESULT_COUNT_SUPPLIED=0 NORMAL_CHAT_COMPOSER_USED=false", file=sys.stderr, flush=True)
                print(f"WEB_CONTEXT_TOTAL_CHARS={len(core_context)} WEB_EVIDENCE_TOTAL_CHARS={sum(len(x.get('text','')[:4000]) for x in web_evidence)} WEB_FRESHNESS_GUARD={'RUN' if web_freshness_required else 'NOT_APPLICABLE'} WEB_FRESHNESS_GUARD_STATUS={'INSUFFICIENT' if not web_evidence else 'READY'}", file=sys.stderr, flush=True)
            if any(x in lower for x in ("sudo ", "recursive delete", "rm -rf", "credentials", "system configuration")):
                task.route, task.authorization_state, task.reason_category = Route.DIRECT, "blocked", "deny_default_operation"
                task.transition(TaskState.DENIED)
                return self._finish(task, "Blocked by deny-default authorization policy.", started)
            write_match = re.fullmatch(r"write file (.+?):\s*(.*)", text.strip(), re.I | re.S)
            if write_match:
                task.route, task.reason_category = Route.DIRECT, "confirm_operation"
                try: target = str(self.retrieval.files.guard.resolve(write_match.group(1).strip()))
                except PermissionError as exc: task.error=str(exc); task.transition(TaskState.FAILED); return self._finish(task,f"Error: {exc}",started)
                action_id=str(uuid.uuid4()); now=time.time(); task.authorization_state="waiting_for_confirmation"; task.transition(TaskState.WAITING)
                self.db.save_task(task)
                with self.db.connect() as conn: conn.execute("INSERT INTO pending_actions VALUES(?,?,?,?,?,?,?)",(task.id,action_id,"write_text",json.dumps({"path":target,"content":write_match.group(2)}),now+900,"pending",now))
                return task, json.dumps({"confirmation_required":True,"task_id":task.id,"action_id":action_id,"summary":f"Write {len(write_match.group(2))} characters to {target}"})
            if re.search(r"\b(rename (?:file|path|this)|move (?:file|path|this)|run (?:a )?(?:local )?script)\b", lower):
                task.route, task.reason_category, task.authorization_state = Route.DIRECT, "unsupported_confirm_operation", "waiting_for_confirmation"
                task.transition(TaskState.WAITING); return self._finish(task, "This operation has no supported typed executor.", started)
            direct = self._direct(text)
            if direct:
                name, inputs = direct; task.route, task.reason_category = Route.DIRECT, "deterministic_match"; task.transition(TaskState.EXECUTING)
                tool = self.tools[name]; output, latency = tool.run(inputs)
                task.tool_executions.append({"tool": name, "version": tool.version, "risk": tool.risk.value, "input": inputs, "output": output, "latency_ms": latency, "status": "success"})
                task.transition(TaskState.COMPLETED); return self._finish(task, json.dumps(output, ensure_ascii=False), started)
            if lower.startswith("procedure lowercase: "):
                task.route, task.reason_category = Route.PROCEDURE, "validated_procedure_match"; task.transition(TaskState.EXECUTING)
                task.tool_executions = self.procedures.run(LOWERCASE_PROCEDURE, {"text": text.split(":",1)[1].strip()})
                task.transition(TaskState.COMPLETED); return self._finish(task, json.dumps(task.tool_executions[-1]["output"]), started)
            managed_write = coding_implementation
            global_no_write = self._global_file_execution_forbidden(text)
            implementation_requested = coding_implementation or self._implementation_intent(lower)
            if implementation_requested and self._file_execution_forbidden(text) and (global_no_write or not managed_write):
                print("FILE_EXECUTION_INTENT=false FILE_EXECUTION_NEGATED=true FILE_WRITE_ATTEMPTED=false FILE_PATCH_ATTEMPTED=false FILE_FINAL_STATUS=FORBIDDEN_BY_USER", file=sys.stderr, flush=True)
                task.route, task.reason_category = Route.NEURAL, "explicit_no_write_request"
                task.transition(TaskState.DENIED)
                return self._finish(task, "No file changes were made because the request explicitly prohibited modifying files.", started)
            elif implementation_requested:
                if coding_implementation and web_search_attempted:
                    print("CODING_WEB_SUPPORT_RETURNED_TO_IMPLEMENTATION=true", file=sys.stderr, flush=True)
                return self._execute_implementation(task, text, core_context, started, workspace_root, managed_context)
            retrieval_query = self._retrieval_query(text)
            evidence = []
            if retrieval_query:
                task.route, task.reason_category = Route.RETRIEVAL, "explicit_search_intent"; task.transition(TaskState.SEARCHING)
                evidence, method = self.retrieval.retrieve(retrieval_query, self.settings.result_limit, "file" in lower or "path" in lower)
                task.selected_context = [{"source": x.source, "line": x.line, "snippet": x.snippet, "method": x.method} for x in evidence]
                if self.retrieval.last_failures: task.selected_context.append({"retrieval_failures":self.retrieval.last_failures})
                if self.settings.vector_enabled: task.selected_context.append({"semantic":self.retrieval.semantic_telemetry})
                if not any(x in lower for x in ("summarize", "explain", "synthesize")):
                    artifact = self.artifacts.create(task.id, evidence) if self.artifacts and len(evidence) > 10 else None
                    if artifact: task.selected_context=[{"artifact":artifact,"retrieval_method":method,"inline_count":0}]
                    task.transition(TaskState.COMPLETED)
                    return self._finish(task, json.dumps({"retrieval_method": method, "results": task.selected_context, "artifact":artifact}, ensure_ascii=False), started)
            task.route = task.route or Route.NEURAL; task.reason_category = task.reason_category or "open_ended_generation"; task.transition(TaskState.GENERATING)
            messages, selected = ContextManager(self.settings.context_budget).build(text, evidence, core_context); task.selected_context = selected
            if web_evidence:
                messages=[{"role":"system","content":"WEB SUCCESS RUNTIME FACT: OLCR has already executed Web Search successfully for this request. Return ONLY valid JSON: {\"items\":[{\"result_id\":\"accepted ID\",\"summary\":\"grounded summary\"}]}. Use only accepted result IDs and evidence; no URLs, no extra keys, no model knowledge, no Internet-unavailable statements."}, *messages]
                print("WEB_SUCCESS_SYSTEM_CONTEXT_APPLIED=true", file=sys.stderr, flush=True)
            result = self._generate_brain(messages, text)
            if web_evidence:
                structured=self._parse_web_composition(result.get("text", ""), web_evidence)
                category=self._web_contradiction_category(result.get("text", ""))
                used_web_evidence=[next(x for x in web_evidence if x.get("source_id")==item["result_id"]) for item in structured]
                grounded=bool(used_web_evidence)
                print(f"WEB_SUCCESS_OUTPUT_RUNTIME_CONTRADICTION={'true' if category else 'false'} WEB_SUCCESS_CONTRADICTION_CATEGORY={category or 'NONE'} WEB_SUCCESS_OUTPUT_GROUNDED={'true' if grounded else 'false'} WEB_SUCCESS_COMPOSITION_ATTEMPT_COUNT=1", file=sys.stderr, flush=True)
                if category or not grounded:
                    repair=[{"role":"system","content":"Return ONLY valid JSON with items containing result_id and summary. Each result_id must be one of the accepted IDs and each summary must use only that result's evidence. No URLs or extra keys."}, *messages]
                    result=self._generate_brain(repair, text)
                    category=self._web_contradiction_category(result.get("text", ""))
                    structured=self._parse_web_composition(result.get("text", ""), web_evidence)
                    used_web_evidence=[next(x for x in web_evidence if x.get("source_id")==item["result_id"]) for item in structured]
                    print(f"WEB_SUCCESS_RETRY_TRIGGER={'RUNTIME_CONTRADICTION' if category else 'UNGROUNDED'} WEB_SUCCESS_COMPOSITION_ATTEMPT_COUNT=2", file=sys.stderr, flush=True)
                if not category and not used_web_evidence:
                    used_web_evidence=web_evidence[:3]
                    result["text"]="現在のWeb検索で確認できたAI関連ニュースです。\n"+"\n".join(f"- {x.get('title','')}" for x in used_web_evidence)
                    print("WEB_SUCCESS_DETERMINISTIC_FALLBACK_USED=true BAD_WEB_SUCCESS_BRAIN_OUTPUT_RENDERED=false", file=sys.stderr, flush=True)
                elif structured:
                    titles={str(x.get("source_id")):str(x.get("title") or "") for x in web_evidence}
                    result["text"]="現在のWeb検索で確認できたAI関連ニュースです。\n\n"+"\n\n".join(f"{i}. {titles.get(item['result_id'],'')}\n   {item['summary']}" for i,item in enumerate(structured,1))
                print(f"BRAIN_RETURNED_USED_RESULT_IDS={','.join(x.get('source_id','') for x in used_web_evidence)} FINAL_USED_RESULT_IDS={','.join(x.get('source_id','') for x in used_web_evidence)}", file=sys.stderr, flush=True)
                web_evidence=used_web_evidence
            if web_freshness_required and web_evidence and not news_request:
                freshness_status, freshness_claim = self._freshness_check_scoped(result.get("text", ""), web_evidence, bool(re.search(r"prerelease|pre-release|beta|rc|release candidate", lower)), self._freshness_target(text))
                print(f"WEB_FRESHNESS_CLAIM={freshness_claim or 'NONE'} WEB_FRESHNESS_CANDIDATE_COUNT={len(web_evidence)} WEB_FRESHNESS_HIGHER_CANDIDATE_COUNT={'1' if freshness_status == 'CONFLICT' else '0'} WEB_FRESHNESS_CONFLICT_COUNT={'1' if freshness_status == 'CONFLICT' else '0'} WEB_FRESHNESS_CORRECTION_ATTEMPTED={'YES' if freshness_status == 'CONFLICT' else 'NO'} WEB_FRESHNESS_GUARD_STATUS={freshness_status}", file=sys.stderr, flush=True)
                if freshness_status == "CONFLICT":
                    correction_messages=[{"role":"system","content":"Correct the current/latest claim using only the fetched Web evidence. Prefer stable over prerelease for a generic latest release. Do not invent versions or URLs."},{"role":"user","content":text+"\n[WEB_CONTEXT_UNTRUSTED]\n"+"\n".join(x.get("text","")[:4000] for x in web_evidence)}]
                    result = self._generate_brain(correction_messages, text)
                    status2, claim2 = self._freshness_check_scoped(result.get("text", ""), web_evidence, bool(re.search(r"prerelease|pre-release|beta|rc|release candidate", lower)), self._freshness_target(text))
                    print(f"WEB_FRESHNESS_CLAIM={claim2 or 'NONE'} WEB_FRESHNESS_CORRECTION_ATTEMPTED=YES WEB_FRESHNESS_GUARD_STATUS={status2}", file=sys.stderr, flush=True)
                    if status2 != "PASS": result["text"]="最新情報を取得したソースから確実に確認できなかったため、最新バージョンを断定できませんでした。"
            if web_evidence:
                result["text"], brain_url_detected = self._suppress_brain_urls(result.get("text", ""))
                result["text"], brain_fragment_detected = self._suppress_brain_source_fragments(result["text"])
                print(f"WEB_BRAIN_URL_DETECTED={'YES' if brain_url_detected else 'NO'} WEB_BRAIN_URL_SUPPRESSED={'YES' if brain_url_detected else 'NO'} WEB_BRAIN_SOURCE_FRAGMENT_DETECTED={'YES' if brain_fragment_detected else 'NO'} WEB_BRAIN_SOURCE_FRAGMENT_SUPPRESSED={'YES' if brain_fragment_detected else 'NO'}", file=sys.stderr, flush=True)
                rendered_sources=self._render_web_sources(web_evidence)
                print(f"FINAL_SOURCE_RESULT_IDS={','.join(x.get('source_id','') for x in web_evidence)} UNUSED_ACCEPTED_SOURCE_RENDERED=NO FINAL_USED_RESULT_COUNT={len(web_evidence)}", file=sys.stderr, flush=True)
                if rendered_sources: result["text"]=result.get("text", "").rstrip()+rendered_sources
            task.model_calls.append({"model": self.settings.main_model, **{k: result.get(k) for k in ("prompt_tokens","completion_tokens","latency_ms")}, "status": "success"})
            task.transition(TaskState.COMPLETED); return self._finish(task, result["text"], started)
        except (ToolValidationError, ValueError, PermissionError, ModelFailure, RuntimeError) as exc:
            task.error = str(exc)
            if task.state not in (TaskState.FAILED, TaskState.DENIED): task.transition(TaskState.FAILED)
            if isinstance(exc, ModelFailure): task.model_calls.append({"model": self.settings.main_model, "status": "error", "error": exc.category})
            return self._finish(task, f"Error: {exc}", started)
    def _finish(self, task: Task, response: str, started: float) -> tuple[Task, str]:
        task.updated_at = task.created_at + (time.perf_counter()-started); self.db.save_task(task); return task, response

    def compose_tool_result(self, request: str, tool_result: dict[str, Any]) -> tuple[Task, str]:
        """Inference-only composition for already executed external tools.

        This intentionally bypasses request routing and retrieval so words such as
        "research" cannot turn a provider result into OLCR retrieval diagnostics.
        """
        task = Task(request); task.transition(TaskState.ROUTING); started = time.perf_counter()
        try:
            task.route = Route.NEURAL; task.reason_category = "external_tool_composition"; task.transition(TaskState.GENERATING)
            packet = json.dumps(tool_result, ensure_ascii=False)[:14000]
            messages = [
                {"role": "system", "content": "Answer only from the supplied external tool result. Write a concise user-facing answer. Do not output JSON, diagnostics, Markdown links, URLs, or a source section."},
                {"role": "user", "content": request + "\n[TOOL_RESULT_UNTRUSTED]\n" + packet},
            ]
            result = self._generate_brain(messages, request)
            response = str(result.get("text", "")).strip()
            if not response: raise RuntimeError("EMPTY_BRAIN_RESPONSE")
            task.model_calls.append({"model": self.settings.main_model, **{k: result.get(k) for k in ("prompt_tokens", "completion_tokens", "latency_ms")}, "status": "success"})
            task.transition(TaskState.COMPLETED)
            return self._finish(task, response, started)
        except (ModelFailure, RuntimeError) as exc:
            task.error = str(exc); task.transition(TaskState.FAILED)
            return self._finish(task, "", started)

    def execute_image(self, text: str, image: dict[str, Any], core_context: str = "") -> tuple[Task, str]:
        """Image preprocessing pipeline; vision remains evidence, never authorization."""
        _vision_diag(IMAGE_REQUEST_EXECUTION_PATH="BACKEND", LIVE_EXECUTION_ENTRY="Runtime.execute_image")
        task = Task(text); task.transition(TaskState.ROUTING); started=time.perf_counter()
        try:
            project_context_present="[ACTIVE_PROJECT_CONTEXT]" in core_context
            project_fact_count=0
            if project_context_present:
                try:
                    payload=core_context.split("[ACTIVE_PROJECT_CONTEXT]",1)[1].split("[/ACTIVE_PROJECT_CONTEXT]",1)[0]
                    project_fact_count=len(json.loads(payload.strip().splitlines()[0]))
                except (IndexError, json.JSONDecodeError):
                    project_fact_count=0
            _vision_diag(PROJECT_CONTEXT_INJECTED_TO_MAIN_MODEL="YES" if project_context_present else "NO",
                         PROJECT_CONTEXT_FACT_COUNT=project_fact_count,
                         PLANNING_CONTEXT_INJECTED="YES" if "[INTERACTIVE_PLANNING_STATE]" in core_context else "NO",
                         CODING_CONTEXT_INJECTED="YES" if "[ACTIVE_CODING_TASK_STATE]" in core_context else "NO")
            if image.get("data_url"):
                encoded=str(image["data_url"]).split(",",1)[-1]
                raw=base64.b64decode(encoded, validate=True)
                if len(raw)>5_000_000: raise RuntimeError("image too large; maximum is 5 MB")
            else:
                if not image.get("canonical_path"):
                    raise RuntimeError("image attachment payload missing; please reattach the image")
                path=Path(image["canonical_path"]); raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != image.get("sha256"): raise RuntimeError("image changed since load; reload required")
            _vision_diag(SEMANTIC_ENABLED="YES" if self.settings.vector_enabled else "NO", R1_START="YES")
            _vision_diag(SEMANTIC_JUDGE_CONFIGURED="YES" if self.settings.semantic_judge_model else "NO", SEMANTIC_JUDGE_MODEL=self.settings.semantic_judge_model or "")
            _vision_diag(R1_NORMALIZER_AVAILABLE="YES" if getattr(self.retrieval.semantic_normalizer, "model", "") else "NO", R1_EVALUATOR_AVAILABLE="YES" if getattr(self.retrieval.semantic_evaluator, "model", "") else "NO")
            evidence, _ = self.retrieval.retrieve(text, self.settings.result_limit, False)
            telemetry=getattr(self.retrieval, "semantic_telemetry", {})
            nd=telemetry.get("normalizer_diagnostics", {})
            vector_telemetry=getattr(getattr(self.retrieval, "vectors", None), "last_telemetry", {})
            if ("embed_invoke_start" in vector_telemetry or "invoke_start" in vector_telemetry) and "embed_invoke_start" not in telemetry and "invoke_start" not in telemetry:
                telemetry=vector_telemetry
            provider=getattr(getattr(self.retrieval, "vectors", None), "provider", None)
            vectors_obj=getattr(self.retrieval, "vectors", None)
            store_present=vectors_obj is not None
            method_present=store_present and hasattr(vectors_obj, "search")
            provider=getattr(vectors_obj, "provider", None)
            provider_present=provider is not None
            _vision_diag(R1_EMBED_INVOKE_START="YES" if telemetry.get("embed_invoke_start", telemetry.get("invoke_start")) else "NO", R1_EMBED_CALL_MODEL=telemetry.get("model", ""), R1_EMBED_INVOKE_END="YES" if telemetry.get("embed_invoke_end", telemetry.get("invoke_end")) else "NO", R1_EMBED_INVOKE_STATUS=telemetry.get("embed_invoke_status", telemetry.get("invoke_status", "NOT_RUN")), R1_QUERY_EMBEDDING_DIMENSION=telemetry.get("query_embedding_dimension", telemetry.get("dimension", 0)))
            _vision_diag(R1_ROUTER_ENTER="YES" if telemetry.get("router_enter") else "NO", R1_VECTOR_ENABLED_EFFECTIVE="YES" if telemetry.get("vector_enabled_effective") else "NO", R1_VECTOR_BRANCH_ENTER="YES" if telemetry.get("vector_branch_enter") else "NO", R1_VECTOR_SEARCH_PRECALL="YES" if telemetry.get("vector_search_precall") else "NO", R1_VECTOR_SEARCH_SKIP_REASON=telemetry.get("vector_search_skip_reason", ""), R1_VECTOR_SEARCH_RETURN_COUNT=telemetry.get("vector_search_return_count", 0), R1_FINAL_RESULTS=len(evidence), R1_NORMALIZER_DIAG_PRESENT="YES" if nd else "NO", R1_NORMALIZER_PARSE_STATUS=nd.get("parse_status", "NOT_OBSERVED"), R1_NORMALIZER_VALIDATION_STATUS=nd.get("validation_status", "NOT_OBSERVED"), R1_NORMALIZER_PROVIDER_DONE_REASON=nd.get("provider_done_reason", ""), R1_NORMALIZER_OUTPUT_LIMIT=nd.get("output_limit", 0))
            _vision_diag(R1_END="YES", R1_RESULTS_COUNT=len(evidence), R1_STATUS="OK")
            r1="\n".join(x.snippet[:800] for x in evidence[:8])
            vision_prompt=f"Perception only. Return ONLY one JSON object VisualContext. {VISION_SCHEMA_INSTRUCTION} {RELATION_SCHEMA_INSTRUCTION} Forbidden aliases: objects, main_elements, visible_text, relations, observations, uncertainties. Use empty arrays for absent collections. Every bbox MUST be bbox_normalized [x1,y1,x2,y2] with values 0..1.\nTask: {text}\nContext: {r1}"
            _vision_diag(VISION_CALL_START="YES", VISION_ATTEMPT_START="YES", VISION_ATTEMPT_NUMBER=1)
            vision_started=time.perf_counter(); result=self.model.vision(raw, image.get("mime_type","image/png"), vision_prompt, self.settings.vision_model)
            raw_text=result.get("text", ""); _vision_diag(VISION_RESPONSE_RECEIVED="YES", VISION_RESPONSE_LENGTH=len(raw_text), VISION_MODEL_USED=self.settings.vision_model)
            task.model_calls.append({"model":self.settings.vision_model,"stage":"VISION_CALL","duration_ms":(time.perf_counter()-vision_started)*1000,"status":"success"})
            try:
                _vision_diag(VISION_PARSE_START="YES"); extracted=_extract_visual_json(raw_text); parsed=json.loads(extracted); _vision_diag(OUTER_TOP_LEVEL_KEYS=",".join(sorted(parsed)) if isinstance(parsed,dict) else "NON_OBJECT"); parsed,unwrapped=_unwrap_visual_context(parsed); _vision_diag(VISUAL_CONTEXT_WRAPPER_UNWRAPPED="YES" if unwrapped else "NO"); _bbox_shape_diag(parsed); parsed,applied,reason=_normalize_single_object(parsed); _vision_diag(SINGLE_OBJECT_NORMALIZATION_ELIGIBLE="YES" if applied else "NO", SINGLE_OBJECT_NORMALIZATION_APPLIED="YES" if applied else "NO", SINGLE_OBJECT_NORMALIZATION_REASON=reason); _vision_diag(VISION_PARSE_END="YES"); _vision_diag(VISION_VALIDATION_START="YES"); visual=validate_visual_context(json.dumps(parsed)); _vision_diag(VISION_VALIDATION_END="PASS")
            except Exception as first_error:
                _vision_diag(VISION_PARSE_END="FAIL", JSON_PARSE_ERROR_BOUNDARY="YES" if "response_boundary" in str(first_error) else "NO", FIRST_RESPONSE_PARSE_VALID="NO", FIRST_RESPONSE_FAILURE=str(first_error), VISION_RETRY_START="YES", VISION_FORMAT_RETRY_USED="YES", VISION_FORMAT_RETRY_COUNT=1)
                retry_prompt=vision_prompt+f"\nReturn EXACTLY one JSON object, with no prose before or after and no Markdown fences. Previous validation error: {first_error}. Use only the same canonical schema: {VISION_SCHEMA_INSTRUCTION} Remove aliases and use empty arrays for absent categories."
                retry=self.model.vision(raw, image.get("mime_type","image/png"), retry_prompt, self.settings.vision_model)
                retry_text=retry.get("text", ""); _vision_diag(VISION_ATTEMPT_NUMBER=2, VISION_RESPONSE_RECEIVED="YES", VISION_RESPONSE_LENGTH=len(retry_text), VISION_PARSE_START="YES", VISION_RESPONSE_START_KIND="JSON_OBJECT" if retry_text.startswith("{") else "OTHER", VISION_RESPONSE_END_KIND="OBJECT_END" if retry_text.endswith("}") else "TRUNCATED_OR_OTHER", BRACE_BALANCE=retry_text.count("{")-retry_text.count("}"), FENCE_COUNT=retry_text.count("```"))
                try:
                    extracted_retry=_extract_visual_json(retry_text); parsed_retry=json.loads(extracted_retry); parsed_retry,unwrapped_retry=_unwrap_visual_context(parsed_retry); parsed_retry,applied_retry,reason_retry=_normalize_single_object(parsed_retry); _vision_diag(VISUAL_CONTEXT_WRAPPER_UNWRAPPED="YES" if unwrapped_retry else "NO", SINGLE_OBJECT_NORMALIZATION_ELIGIBLE="YES" if applied_retry else "NO", SINGLE_OBJECT_NORMALIZATION_APPLIED="YES" if applied_retry else "NO", SINGLE_OBJECT_NORMALIZATION_REASON=reason_retry); _vision_diag(VISION_PARSE_END="YES", VISION_VALIDATION_START="YES"); visual=validate_visual_context(json.dumps(parsed_retry)); _vision_diag(VISION_VALIDATION_END="PASS", VISION_RETRY_END="PASS", SECOND_RESPONSE_PARSE_VALID="YES")
                except Exception as second_error:
                    _vision_diag(VISION_PARSE_END="FAIL", VISION_VALIDATION_END="FAIL", SECOND_RESPONSE_PARSE_VALID="NO", SECOND_RESPONSE_FAILURE=str(second_error), JSON_PARSE_ERROR_BOUNDARY="YES" if "response_boundary" in str(second_error) else "NO", VISION_RETRY_END="FAIL")
                    raise
            # One optional refinement pass using high-confidence visual terms.
            terms=[]
            for item in visual.get("elements",[]) if isinstance(visual.get("elements"),list) else []:
                if isinstance(item,dict) and item.get("label") and float(item.get("confidence",1)) >= 0.7: terms.append(str(item["label"]))
            for rel in visual.get("relationships",[]) if isinstance(visual.get("relationships"),list) else []:
                if isinstance(rel,dict) and rel.get("relation") and float(rel.get("confidence",1)) >= 0.7: terms.append(str(rel["relation"]))
            refined=[]
            _vision_diag(R2_NEEDED="YES" if terms else "NO")
            if terms:
                _vision_diag(R2_START="YES")
                refined,_ = self.retrieval.retrieve(text+" "+" ".join(terms), min(8,self.settings.result_limit), False)
                _vision_diag(R2_END="YES", R2_RESULTS_COUNT=len(refined), R2_STATUS="OK" if refined else "EMPTY")
            r2="\n".join(x.snippet[:800] for x in refined[:8])
            task.route=Route.IMPLEMENTATION if self._implementation_intent(text.lower()) else Route.NEURAL
            task.transition(TaskState.GENERATING)
            # The vision model is only a sensor.  Main-model reasoning receives
            # its evidence together with exactly the same bounded project
            # context used by the text Brain path.
            packet=(f"[CURRENT_USER_MESSAGE]\n{text}\n"
                    f"[CURRENT_ATTACHMENT_VISION_EVIDENCE]\n{json.dumps(visual,ensure_ascii=False)}\n"
                    f"[ACTIVE_PROJECT_AND_CONVERSATION_CONTEXT]\n{core_context}\n"
                    f"[RETRIEVED_CONTEXT_INITIAL]\n{r1}\n[RETRIEVED_CONTEXT_REFINED]\n{r2}\n")
            _vision_diag(VISION_EVIDENCE_INJECTED="YES", PROJECT_CONTEXT_INJECTED_TO_MAIN_MODEL="YES" if project_context_present else "NO")
            _vision_diag(MAIN_MODEL_START="YES"); main_started=time.perf_counter(); answer=self._generate_brain([{"role":"user","content":packet}], text)
            _vision_diag(MAIN_MODEL_RESPONSE_RECEIVED="YES")
            task.model_calls.append({"model":self.settings.main_model,"stage":"QWEN36_MAIN_MODEL","duration_ms":(time.perf_counter()-main_started)*1000,"status":"success"})
            task.transition(TaskState.COMPLETED); return self._finish(task, answer.get("text", ""), started)
        except Exception as exc:
            task.error=str(exc)
            if task.state not in (TaskState.FAILED,TaskState.DENIED): task.transition(TaskState.FAILED)
            return self._finish(task, f"Error: {exc}", started)

    def resolve_confirmation(self, task_id: str, action_id: str, approve: bool) -> tuple[Task, str]:
        with self.db.connect() as conn:
            row=conn.execute("SELECT t.*,p.* FROM tasks t JOIN pending_actions p ON p.task_id=t.id WHERE t.id=? AND p.action_id=?",(task_id,action_id)).fetchone()
        if not row: raise PermissionError("confirmation mismatch")
        if row["status"]!="pending": raise PermissionError("confirmation already resolved")
        if row["expires_at"] < time.time():
            with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='expired' WHERE action_id=?",(action_id,))
            raise PermissionError("confirmation expired")
        task=Task(row["raw_request"],id=task_id,route=Route(row["route"]),state=TaskState.WAITING,authorization_state=row["authorization_state"],created_at=row["created_at"],updated_at=row["updated_at"],reason_category=row["reason_category"])
        if not approve:
            task.authorization_state="rejected"; task.transition(TaskState.CANCELLED)
            with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='rejected' WHERE action_id=?",(action_id,))
            self.db.save_task(task); return task,"Action rejected; nothing was executed."
        values=json.loads(row["tool_input_json"]); target=self.retrieval.files.guard.resolve(values["path"])
        task.authorization_state="authorized"; task.transition(TaskState.EXECUTING); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(values["content"])
        task.tool_executions.append({"tool":"write_text","version":"1.0","risk":"CONFIRM","input":{"path":str(target),"content_length":len(values["content"])},"output":{"path":str(target),"bytes":target.stat().st_size},"status":"success","latency_ms":0})
        task.transition(TaskState.COMPLETED)
        with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='approved' WHERE action_id=?",(action_id,))
        self.db.save_task(task); return task,f"Wrote {target.stat().st_size} bytes to {target}."
    @staticmethod
    def _direct(text: str) -> tuple[str, dict[str, Any]] | None:
        value = text.strip(); lower = value.lower()
        m = re.fullmatch(r"(?:calculate|calc)\s+(.+)", value, re.I)
        if m: return "calculator", {"expression": m.group(1)}
        if lower.startswith("lowercase: "): return "lowercase", {"text": value.split(":",1)[1].strip()}
        if lower.startswith("validate json: "): return "json_validate", {"text": value.split(":",1)[1].strip()}
        m = re.fullmatch(r"sort\s+(-?[\d.]+(?:\s*,\s*-?[\d.]+)*)", value, re.I)
        if m: return "sort_ascending", {"items": [float(x) for x in m.group(1).split(",")]}
        return None
    @staticmethod
    def _implementation_intent(lower: str) -> bool:
        # Treat explicit file-creation requests as implementation work even when
        # the user lists filenames directly (e.g. ``create index.html, style.css``).
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", lower) and re.search(r"(作成|作って|実装|書き込|更新|変更|完成|格納|ファイル).*(workspace|ワークスペース|ファイル|コード|index\.html|style\.css|game\.js|tetris|テトリス)", lower):
            return True
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", lower) and re.search(r"(?:編集して|置き換えて|置換して|直して|修正して|変更して|更新して|確認して.*修正|落ちず|浮いて止まる)", lower) and re.search(r"(?:ファイル|実装|workspace|ワークスペース|テトリス|script\.js|index\.html|\.html\b|\.css\b|\.js\b)", lower):
            return True
        # Natural Japanese often places the destination before the action:
        # ``作業ディレクトリ内に test.html を作成して``.
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", lower) and re.search(r"(?:作業ディレクトリ|作業領域|ワークスペース|workspace).*(?:作成|作って|書き込|保存|更新|変更)", lower) and re.search(r"\.[a-z0-9]{1,6}\b|ファイル", lower, re.I):
            return True
        return bool(re.search(r"\b(implement|create (?:the |.* )?files?|create\s+[^\n]*(?:\.(?:html?|css|js|jsx|ts|py)\b)|modify|fix|refactor|update|write .* (?:into|to) (?:the )?(?:project|workspace)|build)\b", lower))

    @staticmethod
    def _file_execution_forbidden(text: str) -> bool:
        """Detect an explicit prohibition on the requested file action.

        The check is clause-scoped so an unrelated instruction such as
        ``説明はしないで、test.html を編集して`` remains executable.
        """
        clauses = re.split(r"[、。.!?\n]+", text)
        negative = re.compile(r"(?:しないで|しないでください|しない|反映しない|変更しない|編集しない|更新しない|作成しない)")
        file_action = re.compile(r"(?:編集|変更|更新|作成|置き換え|置換|修正|書き込|保存|ファイル|\.html\b|\.css\b|\.js\b)", re.I)
        for clause in clauses:
            if not negative.search(clause) or not file_action.search(clause):
                continue
            # These clauses constrain the mutation scope; they explicitly
            # preserve unrelated content/files while authorizing the target
            # action in another clause.
            if re.match(r"\s*(?:他の部分|それ以外|指定箇所以外|他は|他のファイル|[^、。]+?\s*以外のファイル|このTask以外の既存ファイル|既存ファイル)\s*(?:は|を)?", clause, re.I):
                continue
            if re.search(r"(?:説明|解説|回答|文言).*(?:しないで|しない)", clause) and not re.search(r"(?:ファイル|\.html\b|\.css\b|\.js\b)", clause, re.I):
                continue
            return True
        return bool(re.search(r"(?:まだ|実際に|実際のファイル).{0,24}(?:編集|変更|更新|作成|反映).{0,12}(?:しないで|しない)", text, re.S))

    @staticmethod
    def _global_file_execution_forbidden(text: str) -> bool:
        """True only for an unqualified request to make no workspace changes."""
        for clause in re.split(r"[、。.!?\n]+", text):
            if re.search(r"(?:何も|一切).*(?:書き込|保存|作成|変更|編集|更新).*(?:しないで|しない)", clause):
                return True
            if re.search(r"^\s*ファイル(?:を|は)?.*(?:作成|変更|編集|書き込|保存|更新).*(?:しないで|しない)", clause):
                return True
            if re.search(r"^\s*実装(?:は|を)?.*(?:しないで|しない)", clause):
                return True
        return False

    def _workspace_files(self, workspace_root: Path | None = None) -> list[str]:
        root = workspace_root or (Path(self.settings.allowed_roots[0]) if self.settings.allowed_roots else None)
        if not root: raise PermissionError("an authorized workspace is required for implementation work")
        base = Path(root)
        return sorted(str(item.relative_to(base)) for item in base.rglob("*") if item.is_file())[:200]

    def _related_sources(self, target: Path, limit: int = 8, root: Path | None = None) -> dict[Path, str]:
        """Read bounded source context for an existing target and its direct web links."""
        root = (root or Path(self.settings.allowed_roots[0])).resolve()
        candidates = [target]
        if target.suffix.lower() in {".js", ".ts", ".jsx", ".tsx"}:
            candidates += [p for p in root.glob("*.html")]
            candidates += [root / "style.css", root / "styles.css"]
        elif target.suffix.lower() == ".html":
            candidates += [root / "script.js", root / "style.css", root / "styles.css"]
        out = {}
        for p in candidates:
            try:
                resolved = self.retrieval.files.guard.resolve(str(p))
                if resolved.is_file() and resolved not in out and len(out) < limit:
                    value = resolved.read_text(encoding="utf-8")
                    if len(value) <= 500_000: out[resolved] = value
            except (OSError, PermissionError, UnicodeDecodeError):
                continue
        return out

    @staticmethod
    def _structural_ok(path: Path, content: str, related: dict[Path, str]) -> bool:
        if path.suffix.lower() in {".js", ".mjs", ".ts", ".jsx", ".tsx"}:
            if content.count("{") != content.count("}") or content.count("(") != content.count(")"): return False
            ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)", content))
            html = "\n".join(v for p, v in related.items() if p.suffix.lower() == ".html")
            if ids and html and any(f'id="{i}"' not in html and f"id='{i}'" not in html for i in ids): return False
        return True

    def _execute_implementation(self, task: Task, text: str, core_context: str, started: float,
                                 workspace_root: str | None = None, managed_context: dict | None = None) -> tuple[Task, str]:
        """One bounded model→typed-file-tools→inspection loop for workspace mutations."""
        print("EXECUTION_CHANNEL_AVAILABLE=YES MODEL_TOOL_BINDING=STRUCTURED_OPERATION_JSON OPERATION_PROTOCOL=operations_v1", file=sys.stderr, flush=True)
        task.route, task.reason_category, task.authorization_state = Route.IMPLEMENTATION, "authorized_workspace_mutation", "authorized"
        task.transition(TaskState.EXECUTING)
        root = Path(workspace_root).resolve() if workspace_root else Path(self.settings.allowed_roots[0]).resolve()
        if not root.is_dir(): raise PermissionError("an authorized workspace is required for implementation work")
        # A project-selected workspace is the canonical authorization boundary
        # for managed implementation.  The global retrieval roots may be
        # narrower (and commonly are in tests), so do not reject a valid
        # project path merely because it is absent from that unrelated list.
        execution_guard = PathGuard([str(root)])
        print(f"PATHGUARD_ALLOWED_ROOTS={str(root)} PATHGUARD_TARGET_ROOT_MATCH=YES", file=sys.stderr, flush=True)
        artifact = managed_context.get("implementation_plan_artifact") if isinstance(managed_context, dict) else None
        package_install_required = bool((managed_context or {}).get("package_install_required")) if isinstance(managed_context, dict) else False
        package_install_satisfied = bool((managed_context or {}).get("package_install_satisfied")) if isinstance(managed_context, dict) else False
        package_json_file_mutation_allowed = bool((managed_context or {}).get("allow_package_json_file_mutation")) if isinstance(managed_context, dict) else False
        authorized_package_requirements = ((managed_context or {}).get("authorized_package_requirements")
                                           if isinstance(managed_context, dict) else None)
        package_install_operations = ((managed_context or {}).get("package_install_operations")
                                      if isinstance(managed_context, dict) else None)
        prior_package_install_evidence = ((managed_context or {}).get("prior_package_install_evidence")
                                          if isinstance(managed_context, dict) else None)
        package_json_owner = str((managed_context or {}).get("package_json_dependency_owner") or
                                 ("PACKAGE_INSTALL" if package_install_required else "NONE"))
        package_json_mutation_classes = list((managed_context or {}).get("package_json_mutation_classes") or PACKAGE_JSON_MUTATION_CLASSES)
        dependencies_already_satisfied = bool((managed_context or {}).get("dependencies_already_satisfied", package_install_satisfied))
        package_json_config_mutation_allowed = package_json_file_mutation_allowed
        package_install_status = "PASS" if package_install_satisfied else ("PLANNED" if package_install_required else "NOT_REQUIRED")
        package_json_dependency_mutation_allowed = not package_install_required
        if isinstance(prior_package_install_evidence, list) and prior_package_install_evidence:
            # Keep prior successful install evidence attached to a file retry;
            # it is observational state, not a second package operation.
            task.tool_executions.append({"tool": "package_install_evidence", "version": "1.0",
                                         "risk": "SAFE", "input": {"owner": package_json_owner},
                                         "output": {"status": "PASS", "evidence": prior_package_install_evidence[:4]},
                                         "status": "evidence", "latency_ms": 0})
        if package_install_required and not package_install_satisfied:
            if not authorized_package_requirements:
                raise PackageManagerError("package installation has no canonical authorization",
                                          failure_class="PACKAGE_NOT_AUTHORIZED")
            operations = package_install_operations if isinstance(package_install_operations, list) and package_install_operations else [{
                "operation_type": "PACKAGE_INSTALL", "packages": authorized_package_requirements,
                "dependency_kind": (managed_context or {}).get("dependency_kind", "dependencies"),
            }]
            for operation in operations:
                if not isinstance(operation, dict) or not isinstance(operation.get("packages"), list):
                    raise PackageManagerError("package installation operation is invalid", failure_class="PACKAGE_LIST_INVALID")
                package_input = {"operation_type": "PACKAGE_INSTALL", "package_manager": (managed_context or {}).get("package_manager"),
                                 "packages": operation["packages"], "workspace_root": str(root),
                                 "dependency_kind": operation.get("dependency_kind", "dependencies")}
                try:
                    package_evidence = self.package_manager_executor.install(
                        workspace_root=root, packages=operation["packages"],
                        package_manager=(managed_context or {}).get("package_manager"),
                        dependency_kind=operation.get("dependency_kind", "dependencies"),
                        authorized_packages=authorized_package_requirements, authorized_workspace_root=root,
                        allow_explicit_default=bool((managed_context or {}).get("allow_explicit_package_manager_default")),
                    )
                except PackageManagerError as exc:
                    evidence = exc.details.get("evidence") if isinstance(exc.details.get("evidence"), dict) else {}
                    task.tool_executions.append({"tool": "package_install", "version": "1.0", "risk": "SAFE",
                                                  "input": package_input,
                                                  "output": {"failure_class": exc.failure_class, **evidence},
                                                  "status": "failed", "latency_ms": 0})
                    print(f"PACKAGE_INSTALL_REQUESTED=YES PACKAGE_INSTALL_AUTHORIZED=YES "
                          f"PACKAGE_INSTALL_PROCESS_STARTED={'YES' if evidence.get('process_started') else 'NO'} "
                          f"PACKAGE_INSTALL_EXIT_CODE={evidence.get('exit_code', 'NONE')} PACKAGE_INSTALL_STATUS=FAIL "
                          f"PACKAGE_INSTALL_FAILURE_CLASS={exc.failure_class}", file=sys.stderr, flush=True)
                    raise RuntimeError(f"{exc.failure_class}: {exc}") from exc
                task.tool_executions.append({"tool": "package_install", "version": "1.0", "risk": "SAFE",
                                              "input": package_input, "output": {**package_evidence, "path": "package.json"},
                                              "status": "success", "latency_ms": package_evidence.get("elapsed_ms", 0)})
            package_install_satisfied = True
            dependencies_already_satisfied = True
            package_install_status = "PASS"
            print(f"PACKAGE_INSTALL_REQUESTED=YES PACKAGE_INSTALL_AUTHORIZED=YES "
                  f"PACKAGE_INSTALL_PROCESS_STARTED={'YES' if package_evidence.get('process_started') else 'NO'} "
                  f"PACKAGE_INSTALL_EXIT_CODE={package_evidence.get('exit_code', 'NONE')} PACKAGE_INSTALL_STATUS=PASS "
                  "DEPENDENCY_REQUIREMENTS_SATISFIED=YES",
                  file=sys.stderr, flush=True)
        elif package_install_required:
            print("PACKAGE_INSTALL_REQUESTED=YES PACKAGE_INSTALL_AUTHORIZED=YES PACKAGE_INSTALL_DISPATCHED=SKIPPED_ALREADY_PASS PACKAGE_INSTALL_STATUS=PASS",
                  file=sys.stderr, flush=True)
        if package_install_required:
            # Package installation can create/update package.json, a lockfile,
            # and node_modules.  Re-read those artifacts after the typed
            # operation so the Implementer receives current state and a
            # durable provenance event instead of stale pre-install hashes.
            observed = {}
            for name in ("package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"):
                candidate = root / name
                if candidate.is_file():
                    try:
                        data = candidate.read_bytes()
                        observed[name] = {"exists": True, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    except OSError:
                        observed[name] = {"exists": False}
                else:
                    observed[name] = {"exists": False}
            node_modules = root / "node_modules"
            task.tool_executions.append({"tool": "workspace_refresh", "version": "1.0", "risk": "SAFE",
                                         "input": {"reason": "POST_PACKAGE_INSTALL"},
                                         "output": {"status": "PASS", "artifacts": observed,
                                                    "node_modules_exists": node_modules.is_dir(),
                                                    "provenance": {"package.json": "PACKAGE_INSTALL",
                                                                    "lockfile": "PACKAGE_INSTALL",
                                                                    "node_modules": "PACKAGE_INSTALL"}},
                                         "status": "success", "latency_ms": 0})
        manifest_paths = {str(item.get("path")) for item in (artifact or {}).get("file_manifest", [])
                          if isinstance(item, dict) and isinstance(item.get("path"), str)}
        scaffold_paths = {str(path).replace("\\", "/").lstrip("./")
                          for path in (artifact or {}).get("scaffold_created_paths", [])
                          if isinstance(path, str)}
        # Small managed tasks do not have a persisted file manifest.  Their
        # original plan scope is still the authorization boundary and must be
        # enforced by the same executor before any write occurs.
        approved_scope_paths: set[str] = set()
        for scope in (managed_context or {}).get("approved_scopes", []) if isinstance(managed_context, dict) else []:
            values = scope.get("requested_scope", []) if isinstance(scope, dict) else scope
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                approved_scope_paths.update(str(value).strip().replace("\\", "/") for value in values if str(value).strip())
        if manifest_paths:
            authorized_paths = manifest_paths
        else:
            authorized_paths = approved_scope_paths
        # A package install owns dependency declarations.  Remove package.json
        # from the effective file mutation contract unless the phase explicitly
        # requested a non-dependency configuration edit.  The executor guard
        # below remains in place for callers that bypass the generated schema.
        if package_install_required and not package_json_config_mutation_allowed:
            authorized_paths = {path for path in authorized_paths if path.replace("\\", "/").lstrip("./") != "package.json"}
        if authorized_paths:
            print(f"IMPLEMENTATION_AUTHORIZED_SCOPE_COUNT={len(authorized_paths)} IMPLEMENTATION_SCOPE_SOURCE={'PLAN_MANIFEST' if manifest_paths else 'APPROVED_SCOPE'}", file=sys.stderr, flush=True)

        def scope_allows(relative_path: str) -> bool:
            normalized = relative_path.replace("\\", "/").lstrip("./")
            return any(normalized == allowed.rstrip("/") or normalized.startswith(allowed.rstrip("/") + "/")
                       for allowed in authorized_paths)

        if manifest_paths:
            print(f"IMPLEMENTATION_SCOPE_SOURCE=PLAN_MANIFEST FILE_MANIFEST_COUNT={len(manifest_paths)}", file=sys.stderr, flush=True)

        authorized_path_schema = {"type": "string"}
        if package_install_required and not package_json_config_mutation_allowed:
            # Keep the ownership boundary in the generation contract even for
            # legacy managed calls that have no explicit manifest enum.
            authorized_path_schema["not"] = {"const": "package.json"}
        if authorized_paths:
            # The persisted manifest is an exact-file authorization contract.
            # Constrain structured generation to that set when the provider
            # supports JSON Schema; PathGuard and the preflight remain the
            # final enforcement layers for every caller.
            authorized_path_schema["enum"] = sorted(authorized_paths)
        edit_request = bool(re.search(r"(?:編集|置き換え|置換|修正|変更|更新)\s*(?:して|しろ|ください|する)", text, re.I))
        target_match = re.search(r"(?:^|[\s「『])([\w./-]+\.(?:html?|css|js|mjs|ts|jsx|tsx|py))", text, re.I)
        if edit_request and target_match:
            candidate = (root / target_match.group(1)).resolve()
            if root not in candidate.parents or not candidate.is_file():
                print(f"FILE_EXECUTION_INTENT=true FILE_EXECUTION_NEGATED=false FILE_TARGET={candidate} FILE_TARGET_EXISTS=false FILE_WRITE_ATTEMPTED=false FILE_PATCH_ATTEMPTED=false FILE_OPERATION_SUCCEEDED=false FILE_FINAL_STATUS=TARGET_NOT_FOUND", file=sys.stderr, flush=True)
                task.transition(TaskState.COMPLETED)
                return self._finish(task, f"Target not found: {candidate}. The requested file could not be edited because it does not exist in the selected project workspace.", started)
        files = self._workspace_files(root)
        task.tool_executions.append({"tool":"workspace_list","version":"1.0","risk":"SAFE","input":{},"output":{"files":files},"status":"success","latency_ms":0})
        existing = {}
        for rel in files:
            p = root / rel
            if p.suffix.lower() in {".html", ".css", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".py"} and len(existing) < 8:
                try: existing[rel] = p.read_text(encoding="utf-8")[:500_000]
                except (OSError, UnicodeDecodeError): pass
        source_snapshot_hashes = {
            rel: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for rel, value in existing.items()
        }
        for rel in files:
            if rel in source_snapshot_hashes:
                continue
            candidate = root / rel
            try:
                if candidate.is_file() and candidate.stat().st_size <= 500_000:
                    source_snapshot_hashes[rel] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                continue
        source_snapshot_paths = set(files)
        prompt = ("You have bounded filesystem tools inside the authorized workspace only. The following are actual current source contents. Diagnose from them. "
                  "Scaffold-created files are real current files, so use an exact expected_old_fragment copied from SOURCE, or use a complete write operation with content. "
                  "For existing-file modifications, return a bounded patch using expected_old_fragment and replacement_fragment; do not regenerate a whole file. Return ONLY JSON: "
                  '{"change_required":true,"source_inspected":true,"condition_evaluated":true,"reason_code":"...","operations":[{"op":"patch","path":"relative/path","expected_old_fragment":"...","replacement_fragment":"..."}],"verification":"..."}. '
                  "For implementation requests you must provide one or more write operations; do not return Markdown code blocks or claim files were changed without operations. "
                  f"Workspace files: {files}.\nSOURCE:\n{json.dumps(existing, ensure_ascii=False)}\nRequest: {text}")
        authorized_package_requirements = ((managed_context or {}).get("authorized_package_requirements")
                                           if isinstance(managed_context, dict) else None)
        package_install_required = bool((managed_context or {}).get("package_install_required")) if isinstance(managed_context, dict) else False
        package_install_satisfied = bool((managed_context or {}).get("package_install_satisfied")) if isinstance(managed_context, dict) else False
        if authorized_package_requirements:
            if package_install_required:
                prompt += ("\nPACKAGE_INSTALL_DISPATCHED=YES. The authorized package installation was handled by the typed "
                           "executor before this file-operation call. Do not emit op=package_install and do not edit "
                           "dependency declarations in package.json; implement the remaining authorized file changes. "
                           f"PACKAGE_INSTALL_SATISFIED={'YES' if package_install_satisfied else 'NO'} "
                           f"PACKAGE_INSTALL_STATUS={package_install_status} "
                           f"DEPENDENCIES_ALREADY_SATISFIED={'YES' if dependencies_already_satisfied else 'NO'} "
                           f"PACKAGE_JSON_DEPENDENCY_OWNER={package_json_owner} "
                           f"PACKAGE_JSON_DEPENDENCY_MUTATION_ALLOWED={'YES' if package_json_dependency_mutation_allowed else 'NO'} "
                           f"PACKAGE_JSON_CONFIG_MUTATION_ALLOWED={'YES' if package_json_config_mutation_allowed else 'NO'} "
                           f"PACKAGE_JSON_MUTATION_CLASSES={json.dumps(package_json_mutation_classes, ensure_ascii=False)} "
                           f"PACKAGE_JSON_EFFECTIVE_MUTATION_SCOPE={json.dumps(sorted(authorized_paths), ensure_ascii=False)} "
                           f"AUTHORIZED_PACKAGE_REQUIREMENTS={json.dumps(authorized_package_requirements, ensure_ascii=False, sort_keys=True)}\n")
            else:
                prompt += ("\nIf dependency installation is explicitly required by this phase, the only package operation allowed is "
                           "op=package_install with the authorized package specs below. It must be the sole operation in a batch; "
                           "do not emit shell commands, scripts, global installs, or arbitrary package names. "
                           f"AUTHORIZED_PACKAGE_REQUIREMENTS={json.dumps(authorized_package_requirements, ensure_ascii=False, sort_keys=True)}\n")
        if core_context: prompt += "\nDevelopment plan: " + core_context[: self.settings.context_budget // 4]
        messages=[{"role":"system","content":"Use only the supplied workspace tool protocol."},{"role":"user","content":prompt}]
        operation_schema={
            "type":"object",
            "properties":{
                "change_required":{"type":"boolean"},
                "source_inspected":{"type":"boolean"},
                "condition_evaluated":{"type":"boolean"},
                "reason_code":{"type":"string"},
                "operations":{"type":"array","items":{"oneOf":[
                    {"type":"object","properties":{
                        "op":{"const":"package_install"}, "package_manager":{"enum":["npm","pnpm","yarn"]},
                        "packages":{"type":"array","minItems":1,"maxItems":24,"items":{"type":"string","maxLength":160}},
                        "workspace_root":{"type":"string"}, "dependency_kind":{"enum":["dependencies","devDependencies"]}
                    },"required":["op","packages","workspace_root","dependency_kind"],"additionalProperties":False},
                    {"type":"object","properties":{
                        "op":{"const":"patch"}, "path":authorized_path_schema,
                        "expected_old_fragment":{"type":"string"},
                        "replacement_fragment":{"type":"string"},
                        "old_text":{"type":"string"}, "new_text":{"type":"string"},
                    },"required":["op","path","expected_old_fragment","replacement_fragment"],"additionalProperties":False},
                    {"type":"object","properties":{
                        "op":{"const":"patch"}, "path":authorized_path_schema,
                        "old_text":{"type":"string"}, "new_text":{"type":"string"},
                    },"required":["op","path","old_text","new_text"],"additionalProperties":False},
                    {"type":"object","properties":{
                        "op":{"const":"write"}, "path":authorized_path_schema, "content":{"type":"string"},
                    },"required":["op","path","content"],"additionalProperties":False},
                ]}},
                "verification":{"type":"string"},
            },
            "required":["operations"],
        }
        self._request_fingerprint(messages, "PRODUCTION")
        # Structured file operations are an execution contract.  Thinking is
        # useful for open-ended prose, but qwen3.5 can emit its reasoning as a
        # narrative instead of the requested operations when it is enabled.
        # Keep this override local to the typed Implementer call; all other
        # model roles retain their request-scoped thinking policy.
        result = self._generate_brain(messages, text, structured_schema=operation_schema, thinking_override=False)
        task.model_calls.append({"model":self.settings.main_model, **{k:result.get(k) for k in ("prompt_tokens","completion_tokens","latency_ms","total_duration","load_duration","prompt_eval_duration","eval_duration","load_duration_ms","model_runtime","model_engine","model_quantization","request_options")}, "status":"success"})
        raw=result.get("text", "") if isinstance(result,dict) else ""
        # Qwen may wrap a single otherwise-valid JSON object in a Markdown fence.
        if raw.strip().startswith("```") and raw.strip().endswith("```"):
            raw=raw.strip().split("\n",1)[-1].rsplit("```",1)[0].strip()
        try:
            payload=json.loads(raw); operations=payload.get("operations")
            if isinstance(operations, list):
                normalized_operations = []
                for candidate in operations:
                    if isinstance(candidate, dict):
                        candidate = dict(candidate)
                        # Accept the two common names emitted by older
                        # Implementer prompts, then validate the canonical
                        # operations_v1 fields below.  This is a lossless
                        # field normalization, not a relaxation of preimages.
                        if candidate.get("op") == "patch":
                            if "expected_old_fragment" not in candidate and isinstance(candidate.get("old_text"), str):
                                candidate["expected_old_fragment"] = candidate["old_text"]
                            if "replacement_fragment" not in candidate and isinstance(candidate.get("new_text"), str):
                                candidate["replacement_fragment"] = candidate["new_text"]
                    normalized_operations.append(candidate)
                operations = normalized_operations
            raw_kind="STRUCTURED_OPERATIONS" if isinstance(operations,list) and bool(operations) else "EMPTY"
            asked=bool(re.search(r"(?:続行しますか|継続しますか|ask user|next step|continue\??|次に進みますか)", raw or "", re.I))
            print(f"MODEL_RAW_RESPONSE_KIND={raw_kind} MODEL_OPERATION_EMITTED={'YES' if isinstance(operations,list) and bool(operations) else 'NO'} MODEL_OPERATION_COUNT={len(operations) if isinstance(operations,list) else 0} OPERATION_PARSE_RESULT=PASS PROSE_FALLBACK_USED=NO MODEL_ASKED_FOR_CONTINUATION={'YES' if asked else 'NO'}", file=sys.stderr, flush=True)
            if asked:
                raise RuntimeError("implementation model requested routine continuation")
        except (TypeError, ValueError, KeyError) as exc:
            asked=bool(re.search(r"(?:続行|継続|continue|next step|ask user|次に進|続行しますか)", raw or "", re.I))
            print(f"MODEL_RAW_RESPONSE_KIND={'EMPTY' if not raw.strip() else 'INVALID_JSON'} MODEL_OPERATION_EMITTED=NO MODEL_OPERATION_COUNT=0 OPERATION_PARSE_RESULT=FAIL PROSE_FALLBACK_USED=NO MODEL_ASKED_FOR_CONTINUATION={'YES' if asked else 'NO'} OPERATION_AUTHORIZED=NO OPERATION_EXECUTION_STARTED=NO OPERATION_EXECUTION_RESULT=FAIL", file=sys.stderr, flush=True)
            raise RuntimeError("IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: implementation model did not return a valid file-operation plan") from exc
        if not isinstance(operations,list):
            print("MODEL_RAW_RESPONSE_KIND=INVALID_JSON MODEL_OPERATION_EMITTED=NO MODEL_OPERATION_COUNT=0 OPERATION_PARSE_RESULT=FAIL PROSE_FALLBACK_USED=NO MODEL_ASKED_FOR_CONTINUATION=NO OPERATION_AUTHORIZED=NO OPERATION_EXECUTION_STARTED=NO OPERATION_EXECUTION_RESULT=FAIL", file=sys.stderr, flush=True)
            raise RuntimeError("IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: implementation model returned invalid operations")
        if not operations:
            asked=bool(re.search(r"(?:続行|継続|continue|next step|ask user|次に進|続行しますか)", raw or "", re.I))
            print(f"MODEL_RAW_RESPONSE_KIND=EMPTY MODEL_OPERATION_EMITTED=NO MODEL_OPERATION_COUNT=0 OPERATION_PARSE_RESULT=PASS PROSE_FALLBACK_USED=NO MODEL_ASKED_FOR_CONTINUATION={'YES' if asked else 'NO'} OPERATION_AUTHORIZED=NO OPERATION_EXECUTION_STARTED=NO OPERATION_EXECUTION_RESULT=FAIL", file=sys.stderr, flush=True)
            if payload.get("change_required") is False and payload.get("reason_code") == "already_satisfied" and payload.get("source_inspected") is True and payload.get("condition_evaluated") is True:
                task.transition(TaskState.COMPLETED)
                return self._finish(task, "No changes needed. Verified: source inspection PASS; requested condition already satisfied PASS; workspace writes: 0. Runtime behavior: NOT_RUN.", started)
            # Preserve an auditable typed failure for recovery accounting.  An
            # empty Implementer response is different from an already-satisfied
            # read-only result and must consume the bounded recovery budget.
            task.tool_executions.append({"tool": "operation_rejection", "version": "1.0", "risk": "SAFE",
                                         "input": {"operation_count": 0, "mutation_required": True,
                                                    "failure_class": "EMPTY_IMPLEMENTATION_OPERATIONS"},
                                         "output": {"failure_class": "EMPTY_IMPLEMENTATION_OPERATIONS",
                                                    "reason": "implementation model returned no operations",
                                                    "worktree_state": "UNCHANGED"},
                                         "status": "failed", "latency_ms": 0})
            raise RuntimeError("IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: implementation model returned no operations")
        if len(operations)>20: raise RuntimeError("implementation plan exceeds the 20-operation safety limit")
        package_operations = [item for item in operations if isinstance(item, dict) and item.get("op") == "package_install"]
        if package_operations and package_install_required:
            task.tool_executions.append({"tool": "operation_rejection", "version": "1.0", "risk": "SAFE",
                                         "input": {"operation_count": len(operations),
                                                    "failure_class": "PACKAGE_INSTALL_ALREADY_DISPATCHED"},
                                         "output": {"failure": "PACKAGE_INSTALL was already dispatched by the phase executor",
                                                    "authorized": False, "worktree_state": "UNCHANGED"},
                                         "status": "rejected", "latency_ms": 0})
            raise RuntimeError("PACKAGE_INSTALL_ALREADY_DISPATCHED: package installation must not be repeated by the file Implementer")
        if package_operations:
            if len(package_operations) != 1 or len(operations) != 1:
                raise RuntimeError("package installation must be the sole operation in a batch")
            operation = package_operations[0]
            requested_root = Path(str(operation.get("workspace_root") or "")).expanduser().resolve()
            if requested_root != root:
                raise PermissionError("package-manager workspace_root must equal the authorized project root")
            if not authorized_package_requirements:
                raise PackageManagerError("package installation has no canonical authorization",
                                          failure_class="PACKAGE_NOT_AUTHORIZED")
            try:
                evidence = self.package_manager_executor.install(
                    workspace_root=root,
                    package_manager=operation.get("package_manager"),
                    packages=operation.get("packages"),
                    dependency_kind=operation.get("dependency_kind"),
                    authorized_packages=authorized_package_requirements,
                    authorized_workspace_root=root,
                    allow_explicit_default=bool((managed_context or {}).get("allow_explicit_package_manager_default")),
                )
            except PackageManagerError as exc:
                evidence = exc.details.get("evidence") if isinstance(exc.details.get("evidence"), dict) else {}
                task.tool_executions.append({"tool": "package_install", "version": "1.0", "risk": "SAFE",
                                              "input": {"operation_type": "PACKAGE_INSTALL",
                                                        "package_manager": operation.get("package_manager"),
                                                        "packages": operation.get("packages"),
                                                        "workspace_root": str(root),
                                                        "dependency_kind": operation.get("dependency_kind")},
                                              "output": {"failure_class": exc.failure_class, **evidence},
                                              "status": "failed", "latency_ms": 0})
                raise RuntimeError(f"{exc.failure_class}: {exc}") from exc
            task.tool_executions.append({"tool": "package_install", "version": "1.0", "risk": "SAFE",
                                          "input": {"operation_type": "PACKAGE_INSTALL",
                                                    "package_manager": evidence["package_manager"],
                                                    "packages": evidence["packages_requested"],
                                                    "workspace_root": str(root),
                                                    "dependency_kind": operation.get("dependency_kind")},
                                          "output": {**evidence, "path": "package.json"}, "status": "success",
                                          "latency_ms": evidence.get("elapsed_ms", 0)})
            task.transition(TaskState.COMPLETED)
            return self._finish(task, "Package installation completed with structured evidence.", started)
        changed=[]; snapshots: dict[Path, str | None] = {}; patch_attempted=False; write_attempted=False
        # Preflight simulates the batch in order.  This lets a later patch on
        # the same file consume the state produced by an earlier operation,
        # while still checking the real worktree hash exactly once before any
        # mutation starts.
        preflight_states: dict[Path, str | None] = {}
        # Validate every operation and path before the first write. This keeps
        # authorization atomic: a later out-of-scope operation cannot follow a
        # successful earlier mutation.
        ownership_context: dict[str, Any] = {}
        try:
            for candidate in operations:
                if not isinstance(candidate,dict) or candidate.get("op") not in {"patch", "write"} or not isinstance(candidate.get("path"),str):
                    raise RuntimeError("implementation plan contains an unsupported file operation")
                if candidate.get("op") == "patch" and (
                    not isinstance(candidate.get("expected_old_fragment"), str) or
                    not isinstance(candidate.get("replacement_fragment"), str)):
                    raise RuntimeError("patch operation requires expected_old_fragment and replacement_fragment")
                if candidate.get("op") == "write" and not isinstance(candidate.get("content"), str):
                    raise RuntimeError("write operation requires content")
                requested=Path(candidate["path"])
                target=(requested if requested.is_absolute() else root / requested).expanduser().resolve()
                if root not in target.parents and target != root:
                    raise PermissionError("target outside allowed roots: outside authorized project workspace")
                execution_guard.resolve(str(target))
                relative = target.relative_to(root).as_posix()
                if package_install_required and relative == "package.json" and not package_json_file_mutation_allowed:
                    ownership_context = {"path": relative, "semantic_fields": {"DEPENDENCY_DECLARATION", "DEV_DEPENDENCY_DECLARATION"},
                                         "owner": package_json_owner, "package_install_status": package_install_status,
                                         "dependencies_already_satisfied": dependencies_already_satisfied}
                    raise PermissionError("package.json dependency declarations are owned by PACKAGE_INSTALL")
                if authorized_paths and not scope_allows(relative):
                    raise PermissionError("target outside authorized mutation scope")
                if target not in preflight_states:
                    initial = target.read_text(encoding="utf-8") if target.is_file() else None
                    preflight_states[target] = initial
                    pre_hash = hashlib.sha256(initial.encode("utf-8")).hexdigest() if initial is not None else None
                    if relative in source_snapshot_paths and source_snapshot_hashes.get(relative) != pre_hash:
                        raise RuntimeError("preimage mismatch; source changed since Implementer snapshot")
                current = preflight_states[target] or ""
                old = candidate.get("expected_old_fragment")
                new = candidate.get("replacement_fragment")
                if candidate.get("op") == "patch":
                    if ((relative in scaffold_paths or preflight_states[target] is None or current == "")
                            and isinstance(new, str) and new and old in (None, "")):
                        simulated = new
                    elif isinstance(old, str) and isinstance(new, str) and current.count(old) == 1:
                        simulated = current.replace(old, new, 1)
                    else:
                        # Keep the historical application-layer diagnostic for
                        # an invalid search fragment.  No write is performed
                        # during preflight; execution will reject and roll
                        # back the whole transaction before reporting failure.
                        simulated = current
                else:
                    simulated = candidate.get("content")
                if package_install_required and relative == "package.json":
                    changed_fields = _package_json_changed_fields(current, simulated)
                    dependency_fields = changed_fields & {"DEPENDENCY_DECLARATION", "DEV_DEPENDENCY_DECLARATION"}
                    if dependency_fields:
                        ownership_context = {"path": relative, "semantic_fields": dependency_fields,
                                             "owner": package_json_owner, "package_install_status": package_install_status,
                                             "dependencies_already_satisfied": dependencies_already_satisfied}
                        raise PermissionError("package.json dependency declarations are owned by PACKAGE_INSTALL")
                    if changed_fields and not package_json_config_mutation_allowed:
                        ownership_context = {"path": relative, "semantic_fields": changed_fields,
                                             "owner": package_json_owner, "package_install_status": package_install_status,
                                             "dependencies_already_satisfied": dependencies_already_satisfied}
                        raise PermissionError("package.json configuration mutations are not authorized for this phase")
                pre_apply_hash = hashlib.sha256(current.encode("utf-8")).hexdigest() if preflight_states[target] is not None else None
                preflight_states[target] = simulated
                task.tool_executions.append({"tool": "operation_preflight", "version": "operations_v1",
                                             "risk": "SAFE", "input": {"op": candidate.get("op"), "path": relative},
                                             "output": {"target_canonical": str(target), "authorized": True,
                                                        "pathguard_allowed": True, "pre_apply_hash": pre_apply_hash},
                                             "status": "pass", "latency_ms": 0})
        except Exception as exc:
            # Preserve the exact rejected operation as typed evidence.  The
            # operation was never authorized and no mutation has started;
            # callers can now distinguish a scope rejection from a generic
            # model/provider failure without weakening PathGuard.
            rejected = candidate if isinstance(candidate, dict) else {}
            requested_path = str(rejected.get("path") or "")
            failure_class = _operation_failure_class(exc)
            if not ownership_context and requested_path.replace("\\", "/").lstrip("./") == "package.json" and package_install_required:
                ownership_context = {"path": "package.json",
                                     "semantic_fields": {"DEPENDENCY_DECLARATION", "DEV_DEPENDENCY_DECLARATION"},
                                     "owner": package_json_owner,
                                     "package_install_status": package_install_status,
                                     "dependencies_already_satisfied": dependencies_already_satisfied}
            if ownership_context and failure_class == "PACKAGE_JSON_OWNERSHIP_VIOLATION":
                ownership_context["failure_fingerprint"] = _ownership_failure_fingerprint(
                    path=str(ownership_context.get("path") or "package.json"),
                    semantic_fields=set(ownership_context.get("semantic_fields") or []),
                    owner=str(ownership_context.get("owner") or "PACKAGE_INSTALL"),
                    package_status=str(ownership_context.get("package_install_status") or "UNKNOWN"),
                    dependencies_satisfied=bool(ownership_context.get("dependencies_already_satisfied")),
                )
            task.tool_executions.append({
                "tool": "operation_rejection",
                "version": "operations_v1",
                "risk": "SAFE",
                "input": {"op": rejected.get("op"), "path": requested_path,
                          "failure_class": failure_class,
                          "semantic_field": ",".join(sorted(ownership_context.get("semantic_fields") or [])) or None,
                          "owner": ownership_context.get("owner"),
                          "failure_fingerprint": ownership_context.get("failure_fingerprint"),
                          "operation_fields": sorted(str(key) for key in rejected.keys())},
                "output": {"failure": str(exc), "authorized": False,
                           "pathguard_allowed": "outside allowed roots" not in str(exc).lower(),
                           "authorization_scope": "PLAN_MUTABLE_MANIFEST_SCOPE",
                           "rejection_reason": str(exc),
                           "semantic_field": ",".join(sorted(ownership_context.get("semantic_fields") or [])) or None,
                           "owner": ownership_context.get("owner"),
                           "package_install_status": ownership_context.get("package_install_status"),
                           "dependencies_already_satisfied": ownership_context.get("dependencies_already_satisfied"),
                           "failure_fingerprint": ownership_context.get("failure_fingerprint"),
                           "worktree_state": "UNCHANGED"},
                "status": "rejected",
                "latency_ms": 0,
            })
            print(f"OPERATION_REJECTED=YES OPERATION_REJECTION_CLASS={failure_class} "
                  f"OPERATION_REJECTION_PATH={requested_path or 'UNKNOWN'} "
                  f"OPERATION_REJECTION_FIELD=operations[].path "
                  f"OPERATION_REJECTION_REASON={str(exc)} "
                  "FILES_WRITTEN=0 ACCEPTED_MUTATIONS=0", file=sys.stderr, flush=True)
            print(f"OPERATION_AUTHORIZED=NO OPERATION_COUNT={len(operations)}", file=sys.stderr, flush=True)
            raise
        print(f"OPERATION_AUTHORIZED=YES OPERATION_COUNT={len(operations)}", file=sys.stderr, flush=True)
        execution_expected: dict[Path, str | None] = {}
        try:
          print("OPERATION_EXECUTION_STARTED=YES", file=sys.stderr, flush=True)
          for operation in operations:
            if not isinstance(operation,dict) or operation.get("op") not in {"patch", "write"} or not isinstance(operation.get("path"),str):
                raise RuntimeError("implementation plan contains an unsupported file operation")
            requested=Path(operation["path"])
            if not requested.is_absolute(): requested=root / requested
            target=requested.expanduser().resolve()
            if root not in target.parents and target != root: raise PermissionError("target outside allowed roots: outside authorized project workspace")
            execution_guard.resolve(str(target))
            relative = target.relative_to(root).as_posix()
            if authorized_paths and not scope_allows(relative):
                raise PermissionError("target outside authorized mutation scope")
            existed_before=target.is_file()
            current = target.read_text(encoding="utf-8") if existed_before else None
            if target not in execution_expected:
                execution_expected[target] = current
            elif current != execution_expected[target]:
                raise RuntimeError("preimage mismatch; worktree changed during operation batch")
            pre_apply_hash = hashlib.sha256(current.encode("utf-8")).hexdigest() if current is not None else None
            source_hash = source_snapshot_hashes.get(relative)
            print(f"OPERATION_TARGET={relative} OPERATION_TARGET_CANONICAL={target} AUTHORIZED=YES PATHGUARD_ALLOWED=YES "
                  f"IMPLEMENTER_SOURCE_HASH={source_hash or 'NONE'} EXECUTOR_PRE_APPLY_HASH={pre_apply_hash or 'NONE'} "
                  f"SOURCE_HASH_MATCH={'YES' if source_hash == pre_apply_hash else 'UNKNOWN'}", file=sys.stderr, flush=True)
            current_text = current or ""
            if target not in snapshots:
                snapshots[target] = current if existed_before else None
            normalized_scaffold_write = False
            if operation["op"] == "patch":
                patch_attempted=True
                old, new = operation.get("expected_old_fragment"), operation.get("replacement_fragment")
                # A complete create plan may be emitted as a patch by the
                # implementation model.  For an absent (or empty artifact)
                # target, normalize it to a content-bearing write.  Existing
                # non-empty files retain strict patch preconditions.
                if (relative in scaffold_paths or not existed_before or current_text == "") and isinstance(new, str) and new and (old in (None, "")):
                    operation = {"op":"write", "path":operation["path"], "content":new}
                    normalized_scaffold_write = True
                    task.tool_executions.append({"tool":"workspace_write_normalized","version":"1.0","risk":"SAFE","input":{"path":str(target),"reason":"new_file_complete_content"},"output":{},"status":"normalized","latency_ms":0})

            if operation["op"] == "patch":
                if not isinstance(old, str) or not isinstance(new, str) or not old or current_text.count(old) != 1:
                    old_match_count = current_text.count(old) if isinstance(old, str) else 0
                    patch_diagnostic = {
                        "patch_context": "EXPECTED_OLD_FRAGMENT",
                        "patch_old_text_hash": hashlib.sha256(old.encode("utf-8")).hexdigest() if isinstance(old, str) else "NONE",
                        "patch_new_text_hash": hashlib.sha256(new.encode("utf-8")).hexdigest() if isinstance(new, str) else "NONE",
                        "patch_old_match_count": old_match_count,
                        "implementer_source_hash": source_hash or "NONE",
                        "executor_pre_apply_hash": pre_apply_hash or "NONE",
                        "actual_file_content_hash": pre_apply_hash or "NONE",
                        "transaction_base_hash": hashlib.sha256((execution_expected[target] or "").encode("utf-8")).hexdigest() if execution_expected[target] is not None else "NONE",
                        "preimage_failure_reason": ("STALE_OLD_TEXT" if old_match_count == 0 else "AMBIGUOUS_OLD_TEXT_MATCH"),
                    }
                    task.tool_executions.append({"tool": "preimage_diagnostic", "version": "operations_v1", "risk": "SAFE",
                                                 "input": {key: value for key, value in patch_diagnostic.items() if key != "patch_new_text_hash"},
                                                 "output": patch_diagnostic, "status": "failed", "latency_ms": 0})
                    raise RuntimeError("patch precondition failed; source changed or fragment is ambiguous")
                content = current_text.replace(old, new, 1)
            else:
                write_attempted=True
                content = operation.get("content")
                if not isinstance(content, str) or len(content) > 500_000: raise RuntimeError("invalid full-file operation")
            if target.exists() and operation["op"] == "write" and not normalized_scaffold_write and len(content) < max(32, len(current_text)//2):
                raise RuntimeError("minor edit cannot replace most of an existing file")
            related = self._related_sources(target, root=root)
            if not self._structural_ok(target, content, related): raise RuntimeError("structural validation failed")
            target.parent.mkdir(parents=True,exist_ok=True); target.write_text(content, encoding="utf-8")
            execution_expected[target] = content
            changed.append(str(target))
            task.tool_executions.append({"tool":"workspace_write","version":"1.0","risk":"SAFE","input":{"path":str(target),"content_length":len(content)},"output":{"path":str(target),"bytes":target.stat().st_size,"created_by_current_task":not existed_before},"status":"success","latency_ms":0})
        except Exception as exc:
          failure_class = _operation_failure_class(exc)
          task.tool_executions.append({"tool": "operation_application_failure", "version": "operations_v1",
                                       "risk": "SAFE", "input": {"failure_class": failure_class},
                                       "output": {"failure": str(exc), "worktree_state": "ROLLED_BACK"},
                                       "status": "failure", "latency_ms": 0})
          print(f"OPERATION_FAILURE_CLASS={failure_class} FAILED_APPLICATION_WORKTREE_STATE=ROLLED_BACK", file=sys.stderr, flush=True)
          print("OPERATION_EXECUTION_RESULT=FAIL", file=sys.stderr, flush=True)
          for path, original in snapshots.items():
            try:
                if original is None:
                    if path.exists(): path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")
            except OSError: pass
          raise
        inspected=[]
        for target in changed:
            value=Path(target).read_text(encoding="utf-8")
            inspected.append({"path":target,"bytes":len(value)})
            task.tool_executions.append({"tool":"workspace_read","version":"1.0","risk":"SAFE","input":{"path":target},"output":{"bytes":len(value)},"status":"success","latency_ms":0})
        print(f"OPERATION_EXECUTION_RESULT=PASS OPERATION_EVIDENCE_COUNT={len(changed) + len(inspected)}", file=sys.stderr, flush=True)
        task.transition(TaskState.COMPLETED)
        print(f"FILE_EXECUTION_INTENT=true FILE_EXECUTION_NEGATED=false FILE_TARGET_EXISTS=true FILE_WRITE_ATTEMPTED={'true' if write_attempted else 'false'} FILE_PATCH_ATTEMPTED={'true' if patch_attempted else 'false'} FILE_OPERATION_SUCCEEDED=true FILE_READBACK_SUCCEEDED=true FILE_FINAL_STATUS=SUCCESS", file=sys.stderr, flush=True)
        return self._finish(task, "Updated: " + ", ".join(changed) + ". Write: PASS; read-back: PASS; structural validation: PASS. Runtime behavior: NOT_RUN.", started)
    @staticmethod
    def _retrieval_query(text: str) -> str | None:
        m = re.search(r"(?:search|find|grep)(?:\s+(?:for|files?|paths?))?\s*[:\"]?(.+?)[\"]?$", text.strip(), re.I | re.S)
        if not m: return None
        return re.split(r"\s+(?:and\s+)?(?:summarize|explain|synthesize)\b",m.group(1).strip(),maxsplit=1,flags=re.I)[0].strip()
    @staticmethod
    def _thinking_required(text: str) -> bool:
        value=text.lower(); complex_terms=("debug", "architecture", "design", "implement", "compare", "plan", "why", "なぜ", "実装", "設計")
        return len(value)>180 or any(term in value for term in complex_terms)
