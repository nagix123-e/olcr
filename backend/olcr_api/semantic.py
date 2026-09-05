from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any
from urllib import request, error
from .config import MODEL_REQUEST_TIMEOUT_SECONDS

from .db import Database
from .models import SearchResult
from .retrieval import PathGuard, VectorStore


NORMALIZATION_SYSTEM_PROMPT = """You are a deterministic retrieval-intent normalizer. Convert only the user's retrieval query into a compact semantic specification. Preserve its meaning, named entities, negation, numbers, units, and constraints. Clarify terse wording into requested information when safely entailed, but do not invent facts, entities, answers, candidate-specific language, or search expansions. Return only the required JSON object."""
RELATION_CLASSIFIER_SYSTEM_PROMPT = """You are a deterministic retrieval evidence and relation classifier. Given the original query, a normalized semantic intent, and one authorized candidate, identify an exact short substring from the candidate that relates to the requested information and classify that relation. Semantic paraphrases count: exact query words and literal phrase overlap are not required. Use answers when evidence directly supplies a requested fact, value, name, or result; defines when it directly defines a requested concept, term, rule, or policy; explains when it directly explains a requested process, policy, or mechanism; supports when it materially supports the requested information but is not a complete standalone answer; related when topically adjacent but insufficient; unrelated when it does not materially address the request. For answers, defines, explains, or supports, provide a non-empty exact evidence substring copied from the candidate text. For related or unrelated, evidence may be empty. Do not decide whether OLCR should select the candidate, answer the query, rewrite it, use outside knowledge, or call tools. Return only the required JSON object."""
# The canonical intent object includes two strings and an array. 96 tokens was
# observed to terminate valid qwen3.6 responses before the closing JSON. Keep
# this budget scoped to normalization requests only.
NORMALIZATION_MAX_TOKENS = 192
ADMISSIBILITY_MAX_TOKENS = 256
RERANKER_THRESHOLD = 0.01


class EmbeddingFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category=category


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str], model: str) -> list[list[float]]: ...


class SemanticRelationFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category=category


@dataclass(frozen=True)
class SemanticRelation:
    relation: str
    reason: str
    candidate_id: str
    evidence: str
    judge_model: str
    latency_ms: float


class RerankerFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category=category


@dataclass(frozen=True)
class RerankerScore:
    candidate_id: str
    score: float
    latency_ms: float


class LocalReranker:
    @property
    def status(self) -> dict[str, Any]: raise NotImplementedError
    def score(self, query: str, candidate: SearchResult, candidate_id: str) -> RerankerScore: raise NotImplementedError


class QwenReranker(LocalReranker):
    """Lazy local-only Qwen3 reranker using the model's documented yes/no logits."""
    def __init__(self, model: str, enabled: bool, threshold: float=RERANKER_THRESHOLD):
        self.model_name,self.enabled,self.threshold=model,enabled,threshold
        self._model=self._tokenizer=self._torch=None; self._device=""; self._load_latency_ms=None; self._error=None
    @property
    def model(self) -> str: return self.model_name
    @property
    def status(self) -> dict[str, Any]:
        return {"enabled":self.enabled,"model":self.model_name,"state":"ready" if self._model is not None else ("error" if self._error else "uninitialized"),"device":self._device or None,"load_latency_ms":self._load_latency_ms,"error_category":self._error}
    def _load(self) -> None:
        if self._model is not None: return
        if self._error: raise RerankerFailure(self._error,"Local reranker is unavailable")
        if not self.enabled or not self.model_name: raise RerankerFailure("unavailable","Local reranker is disabled or unconfigured")
        started=time.perf_counter()
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            self._device="mps" if torch.backends.mps.is_available() else "cpu"
            self._tokenizer=AutoTokenizer.from_pretrained(self.model_name,padding_side="left",local_files_only=True)
            self._model=AutoModelForCausalLM.from_pretrained(self.model_name,local_files_only=True).eval().to(self._device)
            self._torch=torch; self._load_latency_ms=(time.perf_counter()-started)*1000
        except ModuleNotFoundError as exc:
            self._error="runtime_unavailable"; raise RerankerFailure(self._error,"PyTorch/Transformers reranker runtime is unavailable") from exc
        except Exception as exc:
            self._error="load_failed"; raise RerankerFailure(self._error,"Local reranker model could not load") from exc
    @staticmethod
    def _prompt(query: str, document: str) -> str:
        return ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be 'yes' or 'no'.<|im_end|>\n<|im_start|>user\n"
                "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n"
                f"<Query>: {query}\n<Document>: {document}\n<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
    def score(self, query: str, candidate: SearchResult, candidate_id: str) -> RerankerScore:
        self._load(); started=time.perf_counter()
        try:
            yes=self._tokenizer("yes",add_special_tokens=False).input_ids[0]; no=self._tokenizer("no",add_special_tokens=False).input_ids[0]
            inputs=self._tokenizer(self._prompt(query,candidate.snippet[:1000]),return_tensors="pt").to(self._device)
            with self._torch.no_grad(): logits=self._model(**inputs).logits[0,-1,[no,yes]].float().cpu()
            score=float(self._torch.softmax(logits,dim=0)[1].item())
            if not math.isfinite(score): raise RerankerFailure("non_finite_score","Reranker score is not finite")
            return RerankerScore(candidate_id,score,(time.perf_counter()-started)*1000)
        except RerankerFailure: raise
        except Exception as exc: raise RerankerFailure("scoring_failed","Local reranker scoring failed") from exc


@dataclass(frozen=True)
class SemanticIntent:
    intent: str
    requested_information: str
    constraints: tuple[str, ...]
    model: str
    latency_ms: float


class IntentNormalizationFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category=category


class SemanticIntentNormalizer(ABC):
    @abstractmethod
    def normalize(self, query: str) -> SemanticIntent: ...


class OllamaIntentNormalizer(SemanticIntentNormalizer):
    def __init__(self,endpoint:str,model:str,timeout:float=MODEL_REQUEST_TIMEOUT_SECONDS): self.endpoint,self.model,self.timeout=endpoint.rstrip("/"),model,timeout; self.last_diagnostics={}
    def normalize(self,query:str)->SemanticIntent:
        if not self.model: raise IntentNormalizationFailure("model_unavailable","No local intent normalizer model configured")
        schema={"type":"object","properties":{"intent":{"type":"string"},"requested_information":{"type":"string"},"constraints":{"type":"array","items":{"type":"string"}}},"required":["intent","requested_information","constraints"],"additionalProperties":False}
        payload=json.dumps({"model":self.model,"messages":[{"role":"system","content":NORMALIZATION_SYSTEM_PROMPT},{"role":"user","content":json.dumps({"query":query},ensure_ascii=False)}],"stream":False,"think":False,"format":schema,"options":{"temperature":0,"seed":0,"num_predict":NORMALIZATION_MAX_TOKENS}}).encode()
        req=request.Request(self.endpoint+"/api/chat",data=payload,headers={"Content-Type":"application/json"});started=time.perf_counter()
        self.last_diagnostics={"call_start":True,"model":self.model}
        try:
            with request.urlopen(req,timeout=self.timeout) as response:data=json.load(response)
        except error.HTTPError as exc: raise IntentNormalizationFailure("provider_error",f"Ollama normalizer HTTP {exc.code}") from exc
        except (error.URLError,TimeoutError) as exc: raise IntentNormalizationFailure("timeout" if isinstance(exc,TimeoutError) else "provider_unavailable",str(exc)) from exc
        content=data.get("message",{}).get("content", "") if isinstance(data,dict) else ""
        stripped=content.strip(); fences=stripped.count("```")
        start_kind="EMPTY" if not stripped else ("FENCE" if stripped.startswith("```") else ("JSON_OBJECT" if stripped.startswith("{") else ("JSON_ARRAY" if stripped.startswith("[") else ("JSON_STRING" if stripped.startswith('"') else "TEXT"))))
        end_kind="FENCE" if stripped.endswith("```") else ("OBJECT_END" if stripped.endswith("}") else ("OTHER" if stripped else "EMPTY"))
        self.last_diagnostics.update({"response_received":True,"response_length":len(content),"provider_done":data.get("done") if isinstance(data,dict) else None,"provider_done_reason":data.get("done_reason","") if isinstance(data,dict) else "","output_limit":NORMALIZATION_MAX_TOKENS,"streaming":False,"response_start_kind":start_kind,"response_end_kind":end_kind,"fence_count":fences,"brace_balance":content.count("{")-content.count("}"),"bracket_balance":content.count("[")-content.count("]"),"first_json_object_found":"YES" if stripped.startswith("{") else "NO","parse_start":True,"json_parse_attempt":True})
        try:
            value=json.loads(data["message"]["content"])
            if set(value)!={"intent","requested_information","constraints"} or not isinstance(value["intent"],str) or not value["intent"].strip() or not isinstance(value["requested_information"],str) or not value["requested_information"].strip() or not isinstance(value["constraints"],list) or any(not isinstance(x,str) for x in value["constraints"]): raise ValueError("invalid intent object")
        except (KeyError,TypeError,ValueError,json.JSONDecodeError) as exc:
            self.last_diagnostics.update({"parse_status":"ERROR","json_parse_error_class":"json_decode_error","validation_status":"FAIL","status":"ERROR"}); raise IntentNormalizationFailure("invalid_response","Malformed semantic intent response") from exc
        self.last_diagnostics.update({"parse_status":"OK","json_parsed":True,"top_level_type":"object","required_fields_present":True,"field_types_valid":True,"validation_status":"PASS","status":"OK","normalized_query_present":True})
        return SemanticIntent(value["intent"].strip(),value["requested_information"].strip(),tuple(value["constraints"]),self.model,(time.perf_counter()-started)*1000)


class SemanticRelationEvaluator(ABC):
    @abstractmethod
    def evaluate(self, query: str, intent: SemanticIntent, candidate: SearchResult, candidate_id: str) -> SemanticRelation: ...


class OllamaSemanticRelationEvaluator(SemanticRelationEvaluator):
    """Fail-closed local evaluator. It receives authorized candidate text, never vectors."""
    def __init__(self,endpoint:str,model:str,timeout:float=MODEL_REQUEST_TIMEOUT_SECONDS): self.endpoint,self.model,self.timeout=endpoint.rstrip("/"),model,timeout
    def evaluate(self,query:str,intent:SemanticIntent,candidate:SearchResult,candidate_id:str)->SemanticRelation:
        if not self.model: raise SemanticRelationFailure("model_unavailable","No local semantic relation model configured")
        schema={"type":"object","properties":{"candidate_id":{"type":"string"},"relation":{"type":"string","enum":["answers","defines","explains","supports","related","unrelated"]},"evidence":{"type":"string"},"reason":{"type":"string"}},"required":["candidate_id","relation","evidence","reason"],"additionalProperties":False}
        messages=[
            {"role":"system","content":RELATION_CLASSIFIER_SYSTEM_PROMPT},
            {"role":"user","content":json.dumps({"query":query,"normalized_intent":{"intent":intent.intent,"requested_information":intent.requested_information,"constraints":list(intent.constraints)},"candidate_id":candidate_id,"source_name":Path(candidate.source).name,"candidate_text":candidate.snippet[:1000]},ensure_ascii=False)},
        ]
        payload=json.dumps({"model":self.model,"messages":messages,"stream":False,"think":False,"format":schema,"options":{"temperature":0,"seed":0,"num_predict":ADMISSIBILITY_MAX_TOKENS}}).encode()
        req=request.Request(self.endpoint+"/api/chat",data=payload,headers={"Content-Type":"application/json"});started=time.perf_counter()
        try:
            with request.urlopen(req,timeout=self.timeout) as response: data=json.load(response)
        except error.HTTPError as exc: raise SemanticRelationFailure("provider_error",f"Ollama relation evaluator HTTP {exc.code}") from exc
        except (error.URLError,TimeoutError) as exc: raise SemanticRelationFailure("timeout" if isinstance(exc,TimeoutError) else "provider_unavailable",str(exc)) from exc
        try:
            value=json.loads(data["message"]["content"])
        except json.JSONDecodeError as exc:
            raise SemanticRelationFailure("output_truncated" if data.get("done_reason")=="length" else "malformed_json","Malformed relation response") from exc
        except (KeyError,TypeError) as exc: raise SemanticRelationFailure("schema_mismatch","Malformed relation response") from exc
        if set(value)!={"candidate_id","relation","evidence","reason"} or not all(isinstance(value[key],str) for key in value): raise SemanticRelationFailure("schema_mismatch","Malformed relation response")
        if value["candidate_id"]!=candidate_id: raise SemanticRelationFailure("candidate_id_mismatch","Relation response candidate mismatch")
        if value["relation"] not in {"answers","defines","explains","supports","related","unrelated"}: raise SemanticRelationFailure("invalid_relation","Invalid semantic relation")
        if value["relation"] in {"answers","defines","explains","supports"} and not value["evidence"].strip(): raise SemanticRelationFailure("missing_required_evidence","Selectable relation lacks evidence")
        return SemanticRelation(value["relation"],value["reason"][:240],candidate_id,value["evidence"],self.model,(time.perf_counter()-started)*1000)


class OllamaEmbeddingProvider(EmbeddingProvider):
    def __init__(self, endpoint: str, timeout: float=MODEL_REQUEST_TIMEOUT_SECONDS): self.endpoint,self.timeout=endpoint.rstrip("/"),timeout
    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not model: raise EmbeddingFailure("model_unavailable","No embedding model configured")
        req=request.Request(self.endpoint+"/api/embed",data=json.dumps({"model":model,"input":texts,"truncate":True}).encode(),headers={"Content-Type":"application/json"})
        try:
            with request.urlopen(req,timeout=self.timeout) as response: data=json.load(response)
        except error.HTTPError as exc: raise EmbeddingFailure("model_unavailable" if exc.code==404 else "provider_error",f"Ollama embedding HTTP {exc.code}") from exc
        except (error.URLError,TimeoutError) as exc: raise EmbeddingFailure("provider_unavailable",str(exc)) from exc
        vectors=data.get("embeddings")
        if not isinstance(vectors,list) or len(vectors)!=len(texts): raise EmbeddingFailure("invalid_response","Embedding count mismatch")
        dimensions={len(v) for v in vectors if isinstance(v,list)}
        if len(dimensions)!=1 or not dimensions or 0 in dimensions: raise EmbeddingFailure("invalid_response","Invalid embedding dimensions")
        if any(not isinstance(x,(int,float)) for v in vectors for x in v): raise EmbeddingFailure("invalid_response","Non-numeric embedding")
        return [[float(x) for x in v] for v in vectors]


class LocalVectorStore(VectorStore):
    INDEX_VERSION="olcr-lines-v1"
    def __init__(self,db:Database,provider:EmbeddingProvider,model:str,roots:tuple[str,...],min_score:float=.25,chunk_chars:int=1000):
        self.db,self.provider,self.model,self.guard,self.min_score,self.chunk_chars=db,provider,model,PathGuard(roots),min_score,chunk_chars
        self.state="configured" if model else "model_unavailable"; self.last_telemetry:dict[str,Any]={"available":bool(model),"state":self.state,"model":model}
    @staticmethod
    def _hash(text:str)->str:return hashlib.sha256(text.encode()).hexdigest()
    def _chunks(self,text:str)->list[tuple[int,str]]:
        chunks=[]; current=[]; start=1; size=0
        for number,line in enumerate(text.splitlines() or [text],1):
            addition=len(line)+1
            if current and size+addition>self.chunk_chars:
                chunks.append((start,"\n".join(current)));current=[];start=number;size=0
            current.append(line);size+=addition
        if current:chunks.append((start,"\n".join(current)))
        return [(line,value) for line,value in chunks if value.strip()]
    def index_document(self,document_id:int,source:str,text:str)->dict[str,Any]:
        path=self.guard.resolve(source)
        if not path.is_file(): raise PermissionError("semantic source must be an authorized file")
        actual=path.read_text(errors="strict")
        if actual!=text: raise ValueError("indexed text does not match authorized source")
        chunks=self._chunks(text); started=time.perf_counter(); self.state="indexing";self.last_telemetry={"available":bool(self.model),"state":"indexing","model":self.model}
        try: vectors=self.provider.embed([x[1] for x in chunks],self.model) if chunks else []
        except EmbeddingFailure as exc:
            self.state="model_unavailable" if exc.category=="model_unavailable" else "error"
            self.last_telemetry={"available":False,"state":self.state,"model":self.model,"error_category":exc.category};raise
        dimension=len(vectors[0]) if vectors else 0; doc_hash=self._hash(text)
        if any(len(v)!=dimension for v in vectors): raise EmbeddingFailure("invalid_response","Embedding dimension mismatch")
        with self.db.connect() as db:
            db.execute("DELETE FROM vector_embeddings WHERE document_id=?",(document_id,))
            for ordinal,((line,chunk),vector) in enumerate(zip(chunks,vectors)):
                db.execute("INSERT INTO vector_embeddings(document_id,chunk_ordinal,line_start,text,content_hash,document_hash,model,dimension,index_version,vector_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (document_id,ordinal,line,chunk,self._hash(chunk),doc_hash,self.model,dimension,self.INDEX_VERSION,json.dumps(vector),time.time()))
        self.state="ready"; self.last_telemetry={"available":True,"state":"ready","model":self.model,"dimension":dimension,"indexed_chunks":len(chunks),"latency_ms":(time.perf_counter()-started)*1000}
        return self.last_telemetry
    @staticmethod
    def _cosine(a:list[float],b:list[float])->float:
        if len(a)!=len(b): return -1
        norm=math.sqrt(sum(x*x for x in a))*math.sqrt(sum(x*x for x in b))
        return sum(x*y for x,y in zip(a,b))/norm if norm else 0
    @staticmethod
    def _near(a:str,b:str)->bool:
        left,right=set(a.lower().split()),set(b.lower().split())
        return bool(left and right) and len(left&right)/len(left|right)>.9
    def search(self,query:str,limit:int)->list[SearchResult]:
        started=time.perf_counter()
        self.last_telemetry={"available":bool(self.model),"attempted":False,"model":self.model,"query_embedding_created":False}
        if not self.model:
            self.last_telemetry={"available":False,"attempted":False,"state":"model_unavailable","model":""};return []
        self.last_telemetry.update({"provider_present":self.provider is not None,"method_present":hasattr(self.provider,"embed"),"invoke_start":True,"embed_invoke_start":True})
        try:q=self.provider.embed([query],self.model)[0]
        except EmbeddingFailure as exc:
            self.last_telemetry.update({"attempted":True,"invoke_end":False,"embed_invoke_end":False,"invoke_status":"ERROR","embed_invoke_status":"ERROR","error_category":exc.category,"state":"error"})
            self.state="model_unavailable" if exc.category=="model_unavailable" else "error"
            self.last_telemetry={"available":False,"attempted":True,"selected":False,"state":self.state,"model":self.model,"error_category":exc.category};raise
        self.last_telemetry.update({"attempted":True,"invoke_end":True,"embed_invoke_end":True,"invoke_status":"OK","embed_invoke_status":"OK","query_embedding_created":True,"dimension":len(q),"query_embedding_dimension":len(q)})
        candidates=[]; stale=0; dimension_matches=0; authorized=0; score_pass=0
        with self.db.connect() as db:
            stored_rows=db.execute("SELECT COUNT(*) FROM vector_embeddings").fetchone()[0]
            model_rows=db.execute("SELECT COUNT(*) FROM vector_embeddings WHERE model=? AND index_version=?",(self.model,self.INDEX_VERSION)).fetchone()[0]
            rows=db.execute("SELECT v.*,d.source,d.text document_text FROM vector_embeddings v JOIN documents d ON d.id=v.document_id WHERE v.model=? AND v.index_version=? AND v.dimension=?",(self.model,self.INDEX_VERSION,len(q))).fetchall()
        dimension_matches=len(rows)
        for row in rows:
            try:
                path=self.guard.resolve(row["source"])
                if not path.is_file() or self._hash(path.read_text(errors="strict"))!=row["document_hash"]: stale+=1;continue
            except (PermissionError,OSError,UnicodeError): stale+=1;continue
            authorized+=1
            score=self._cosine(q,json.loads(row["vector_json"]))
            if score>=self.min_score:
                score_pass+=1; candidates.append(SearchResult(row["source"],row["text"],score,row["line_start"],"semantic"))
        candidates.sort(key=lambda x:x.score,reverse=True); selected=[]
        for item in candidates:
            if any(self._near(item.snippet,x.snippet) for x in selected):continue
            selected.append(item)
            if len(selected)>=limit:break
        self.state="ready";self.last_telemetry={"available":True,"attempted":True,"selected":False,"state":"ready","model":self.model,"dimension":len(q),"query_embedding_created":True,"stored_vector_rows_visible":stored_rows,"model_match_vector_rows":model_rows,"candidate_count":len(selected),"vector_candidates":len(candidates),"dimension_match_count":dimension_matches,"authorized_count":authorized,"sha_valid_count":authorized,"score_pass_count":score_pass,"stale_count":stale,"latency_ms":(time.perf_counter()-started)*1000}
        return selected
