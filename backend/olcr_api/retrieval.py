from __future__ import annotations

from abc import ABC, abstractmethod
import os
import math
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from .db import Database
from .models import SearchResult


class PathGuard:
    def __init__(self, roots: Iterable[str]): self.roots = tuple(Path(r).resolve() for r in roots)
    def resolve(self, path: str) -> Path:
        value = Path(path).expanduser().resolve()
        if not any(value == root or root in value.parents for root in self.roots): raise PermissionError("path is outside allowed roots")
        return value


class Retriever(ABC):
    @abstractmethod
    def search(self, query: str, limit: int) -> list[SearchResult]: ...


class FileRetriever(Retriever):
    def __init__(self, roots: Iterable[str]): self.guard = PathGuard(roots)
    def filename(self, query: str, limit: int) -> list[SearchResult]:
        found = []
        needle = query.lower()
        for root in self.guard.roots:
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv"}]
                for name in files:
                    path = Path(base) / name
                    if needle in name.lower() or needle in str(path.relative_to(root)).lower():
                        found.append(SearchResult(str(path), name, method="filename"))
                        if len(found) >= limit: return found
        return found
    def search(self, query: str, limit: int) -> list[SearchResult]:
        if not query.strip(): return []
        if shutil.which("rg"):
            cmd = ["rg", "--hidden", "--glob", "!.git/**", "--glob", "!node_modules/**", "--glob", "!.venv/**", "--no-heading", "--line-number", "--color", "never", "--max-count", str(limit), "--", query, *map(str, self.guard.roots)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode not in (0, 1): raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()}")
            rows = []
            for line in proc.stdout.splitlines()[:limit]:
                try: source, number, snippet = line.split(":", 2); rows.append(SearchResult(source, snippet, line=int(number), method="ripgrep"))
                except ValueError: continue
            return rows
        rows = []
        for root in self.guard.roots:
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv"}]
                for name in files:
                    path = Path(base) / name
                    try:
                        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                            if query.lower() in line.lower(): rows.append(SearchResult(str(path), line[:500], line=number, method="python_fallback"))
                            if len(rows) >= limit: return rows
                    except (OSError, UnicodeError): pass
        return rows


class FTSRetriever(Retriever):
    def __init__(self, db: Database, source: str | None = None): self.db, self.source = db, source
    def search(self, query: str, limit: int) -> list[SearchResult]:
        return [SearchResult(x["source"], x["snippet"], score=-x["rank"], method="fts5") for x in self.db.search_fts(query, limit, self.source)]


class VectorStore(ABC):
    @abstractmethod
    def search(self, query: str, limit: int) -> list[SearchResult]: ...


class DisabledVectorStore(VectorStore):
    state="disabled"
    last_telemetry={"available":False,"attempted":False,"state":"disabled","model":""}
    def search(self, query: str, limit: int) -> list[SearchResult]: return []


class RetrievalRouter:
    SEMANTIC_CANDIDATE_LIMIT=3
    SELECTABLE_RELATIONS={"answers","defines","explains","supports"}
    def __init__(self, files: FileRetriever, fts: FTSRetriever, vectors: VectorStore, vector_enabled: bool, semantic_evaluator: object | None=None, semantic_normalizer: object | None=None, reranker: object | None=None, reranker_threshold: float=.01):
        self.files, self.fts, self.vectors, self.vector_enabled, self.semantic_evaluator, self.semantic_normalizer, self.reranker, self.reranker_threshold = files, fts, vectors, vector_enabled, semantic_evaluator, semantic_normalizer, reranker, reranker_threshold
        self.last_failures: list[dict[str, str]] = []
        self.semantic_telemetry: dict = getattr(vectors,"last_telemetry",{"state":"disabled","available":False})
    def retrieve(self, query: str, limit: int, filename_hint: bool = False) -> tuple[list[SearchResult], str]:
        self.last_failures = []
        self.semantic_telemetry={"available":self.vector_enabled and getattr(self.vectors,"state","") not in ("disabled","model_unavailable"),"attempted":False,"state":getattr(self.vectors,"state","disabled"),"model":getattr(self.vectors,"model",""),"router_enter":True,"vector_enabled_effective":self.vector_enabled}
        if filename_hint:
            try: rows = self.files.filename(query, limit)
            except Exception as exc:
                self.last_failures.append({"layer":"filename","category":type(exc).__name__}); rows=[]
            if rows: return rows, "filename"
        try: rows = self.files.search(query, limit)
        except Exception as exc:
            self.last_failures.append({"layer":"lexical","category":type(exc).__name__}); rows=[]
        self.semantic_telemetry["lexical_results_count"]=len(rows)
        if rows: self.semantic_telemetry["lexical_short_circuit"]=True; return rows, rows[0].method
        try: rows = self.fts.search(query, limit)
        except Exception as exc:
            self.last_failures.append({"layer":"fts5","category":type(exc).__name__}); rows=[]
        if rows: return rows, "fts5"
        if self.vector_enabled and self.reranker is not None:
            try:
                candidates=self.vectors.search(query,limit);base=dict(getattr(self.vectors,"last_telemetry",{}));scores=[];reranker_latency=0.0
                base.update({"reranker":getattr(self.reranker,"status",{"enabled":False}),"reranker_attempted":bool(candidates),"candidate_count":len(candidates[:self.SEMANTIC_CANDIDATE_LIMIT]),"scored_count":0,"threshold":self.reranker_threshold,"abstained":True,"selected":False,"qwen_relation_attempted":False})
                for number,candidate in enumerate(candidates[:self.SEMANTIC_CANDIDATE_LIMIT],1):
                    candidate_id=f"candidate-{number}"
                    try: decision=self.reranker.score(query,candidate,candidate_id)
                    except Exception as exc:
                        base.update({"scored_count":len(scores),"reranker_error":getattr(exc,"category",type(exc).__name__),"reranker_latency_ms":reranker_latency,"reranker":getattr(self.reranker,"status",{})});self.semantic_telemetry=base;return [],"none"
                    if not math.isfinite(decision.score):
                        base.update({"scored_count":len(scores),"reranker_error":"non_finite_score","reranker_latency_ms":reranker_latency});self.semantic_telemetry=base;return [],"none"
                    reranker_latency+=decision.latency_ms;scores.append((decision.score,candidate,candidate_id,decision.latency_ms))
                scores.sort(key=lambda item:item[0],reverse=True)
                records=[{"candidate_id":item[2],"source":item[1].source,"similarity_score":item[1].score,"score":item[0],"latency_ms":item[3]} for item in scores]
                top1=scores[0][0] if scores else None;top2=scores[1][0] if len(scores)>1 else None;gap=top1-top2 if top2 is not None else None
                base.update({"reranker":getattr(self.reranker,"status",{}),"scored_count":len(scores),"scores":records,"top1_score":top1,"top2_score":top2,"top1_top2_gap":gap,"reranker_latency_ms":reranker_latency})
                if scores and top1 >= self.reranker_threshold:
                    _,candidate,candidate_id,_=scores[0];base.update({"abstained":False,"selected":True,"selected_candidate_id":candidate_id,"selected_score":top1,"selected_candidate":{"candidate_id":candidate_id,"source":candidate.source,"similarity_score":candidate.score,"reranker_score":top1}});self.semantic_telemetry=base;return [candidate],"semantic"
                self.semantic_telemetry=base;return [],"none"
            except Exception as exc:
                self.last_failures.append({"layer":"semantic","category":getattr(exc,"category",type(exc).__name__)})
                self.semantic_telemetry={"available":True,"attempted":True,"selected":False,"state":"error","model":getattr(self.vectors,"model","") ,"error_category":getattr(exc,"category",type(exc).__name__)}
                return [], "none"
        if self.vector_enabled:
            self.semantic_telemetry["vector_branch_enter"]=True
            try:
                if self.semantic_normalizer is None:
                    self.semantic_telemetry.update({"vector_search_skip_reason":"normalizer_unavailable","vector_precall_status":"SKIPPED"})
                    self.semantic_telemetry.update({"normalization":{"attempted":False,"succeeded":False,"error":"normalizer_unavailable"},"relation_attempted":False,"abstained":True});return [],"none"
                try:
                    intent=self.semantic_normalizer.normalize(query)
                    self.semantic_telemetry["normalizer_diagnostics"]=getattr(self.semantic_normalizer,"last_diagnostics",{})
                    normalization={"attempted":True,"model":intent.model,"succeeded":True,"latency_ms":intent.latency_ms,"intent":intent.intent,"requested_information":intent.requested_information,"constraints":list(intent.constraints)}
                except Exception as exc:
                    self.semantic_telemetry["normalizer_diagnostics"]=getattr(self.semantic_normalizer,"last_diagnostics",{})
                    self.semantic_telemetry.update({"vector_search_skip_reason":"normalizer_error","vector_precall_status":"SKIPPED","vector_precall_error_class":getattr(exc,"category",type(exc).__name__)})
                    self.semantic_telemetry.update({"normalization":{"attempted":True,"model":getattr(self.semantic_normalizer,"model",""),"succeeded":False,"error":getattr(exc,"category",type(exc).__name__)},"relation_attempted":False,"abstained":True});return [],"none"
                self.semantic_telemetry.update({"vector_store_present":self.vectors is not None,"vector_store_type":type(self.vectors).__name__,"vector_search_method_present":hasattr(self.vectors,"search"),"vector_search_precall":True,"vector_search_call_start":True})
                try:
                    candidates=self.vectors.search(query,limit)
                    self.semantic_telemetry.update({"vector_search_call_end":True,"vector_search_invoke_status":"OK","vector_search_return_received":True,"vector_search_return_count":len(candidates)})
                except Exception as exc:
                    self.semantic_telemetry.update({"vector_search_call_end":False,"vector_search_invoke_status":"ERROR","vector_search_error_class":getattr(exc,"category",type(exc).__name__)})
                    raise
                base=dict(self.semantic_telemetry);base.update(getattr(self.vectors,"last_telemetry",{}));decisions=[];evaluator_latency=0.0
                base.update({"normalization":normalization,"relation_attempted":bool(candidates),"candidates_evaluated":0,"relation_model":getattr(self.semantic_evaluator,"model",""),"abstained":True,"selected":False})
                if candidates and self.semantic_evaluator is None:
                    base["relation_error"]="evaluator_unavailable";self.semantic_telemetry=base;return [],"none"
                for number,candidate in enumerate(candidates[:self.SEMANTIC_CANDIDATE_LIMIT],1):
                    candidate_id=f"candidate-{number}"
                    try: decision=self.semantic_evaluator.evaluate(query,intent,candidate,candidate_id)
                    except Exception as exc:
                        base.update({"candidates_evaluated":len(decisions),"relation_error":getattr(exc,"category",type(exc).__name__),"relation_latency_ms":evaluator_latency});self.semantic_telemetry=base;return [],"none"
                    selectable=decision.relation in self.SELECTABLE_RELATIONS;valid_evidence=bool(decision.evidence) and decision.evidence in candidate.snippet
                    selected=selectable and valid_evidence;evaluator_latency+=decision.latency_ms
                    decisions.append({"candidate_id":candidate_id,"source":candidate.source,"similarity_score":candidate.score,"relation":decision.relation,"selected":selected,"reason":decision.reason,"evidence":decision.evidence if valid_evidence else "","evidence_validation":"passed" if selected else ("failed" if selectable else "not_applicable"),"latency_ms":decision.latency_ms})
                    if selected:
                        base.update({"candidates_evaluated":len(decisions),"relation_decisions":decisions,"relation_latency_ms":evaluator_latency,"abstained":False,"selected":True,"selected_candidate":{"candidate_id":candidate_id,"source":candidate.source,"similarity_score":candidate.score,"relation":decision.relation,"evidence":decision.evidence}});self.semantic_telemetry=base;return [candidate],"semantic"
                base.update({"candidates_evaluated":len(decisions),"relation_decisions":decisions,"relation_latency_ms":evaluator_latency});self.semantic_telemetry=base
                return [],"none"
            except Exception as exc:
                self.last_failures.append({"layer":"semantic","category":getattr(exc,"category",type(exc).__name__)})
                self.semantic_telemetry={"available":True,"attempted":True,"selected":False,"state":"error","model":getattr(self.vectors,"model",""),"error_category":getattr(exc,"category",type(exc).__name__)}
                return [], "none"
        if self.last_failures: raise RuntimeError("retrieval failed: " + ", ".join(x["layer"]+":"+x["category"] for x in self.last_failures))
        return [], "none"
