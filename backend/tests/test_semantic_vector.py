import json
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib import error

from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import SearchResult
from olcr_api.retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, RetrievalRouter
from olcr_api.runtime import ContextManager, Runtime
from olcr_api.semantic import ADMISSIBILITY_MAX_TOKENS, NORMALIZATION_MAX_TOKENS, NORMALIZATION_SYSTEM_PROMPT, RerankerFailure, RerankerScore, SemanticRelation, SemanticRelationFailure, EmbeddingFailure, EmbeddingProvider, IntentNormalizationFailure, LocalVectorStore, OllamaSemanticRelationEvaluator, OllamaEmbeddingProvider, OllamaIntentNormalizer, RELATION_CLASSIFIER_SYSTEM_PROMPT, SemanticIntent


class FixtureEmbeddings(EmbeddingProvider):
    def __init__(self): self.calls=[]; self.fail=False
    def embed(self,texts,model):
        self.calls.append((list(texts),model))
        if self.fail: raise EmbeddingFailure("provider_error","fixture failure")
        vectors=[]
        for value in texts:
            text=value.lower()
            if any(x in text for x in ("cedar","codename","wooden","illumination","nickname")): vectors.append([1.,0.,0.])
            elif any(x in text for x in ("authorization","approval","rejection","replay","handshake","persisted action")): vectors.append([0.,1.,0.])
            elif any(x in text for x in ("context","evidence","corpus","budget","prompt allocation")): vectors.append([0.,0.,1.])
            else: vectors.append([-.5,-.5,-.5])
        return vectors


class FakeModel:
    def generate(self,messages,model,stream=False): return {"text":"unused","latency_ms":0}


class FixtureEvaluator:
    model="fixture-judge"
    def __init__(self): self.calls=[];self.error=None;self.relation=lambda query,candidate:"answers"
    def evaluate(self,query,intent,candidate,candidate_id):
        self.calls.append((query,candidate.source,candidate.snippet,candidate_id))
        if self.error: raise SemanticRelationFailure(self.error,"fixture evaluator failure")
        relation=self.relation(query,candidate)
        evidence=candidate.snippet[:20] if relation in {"answers","defines","explains","supports"} else ""
        return SemanticRelation(relation,"relation evidence",candidate_id,evidence,self.model,1.0)


class FixtureReranker:
    model="Qwen/Qwen3-Reranker-0.6B"
    def __init__(self): self.calls=[];self.error=None;self.values={}
    @property
    def status(self): return {"enabled":True,"model":self.model,"state":"ready","device":"fixture"}
    def score(self,query,candidate,candidate_id):
        self.calls.append((query,candidate.source,candidate.snippet,candidate_id))
        if self.error: raise RerankerFailure(self.error,"fixture reranker failure")
        return RerankerScore(candidate_id,self.values.get(candidate.source,.5),1.0)


class FixtureNormalizer:
    model="fixture-judge"
    def __init__(self): self.calls=[];self.error=None
    def normalize(self,query):
        self.calls.append(query)
        if self.error: raise IntentNormalizationFailure(self.error,"fixture normalizer failure")
        return SemanticIntent("requested semantic intent",query,(),self.model,1.0)


class StaticVectorStore:
    state="ready";model="fixture-embed"
    def __init__(self,rows): self.rows=rows;self.last_telemetry={"available":True,"state":"ready","model":self.model}
    def search(self,query,limit):
        self.last_telemetry={"available":True,"attempted":True,"selected":False,"state":"ready","model":self.model,"dimension":3,"candidate_count":len(self.rows),"stale_count":0,"latency_ms":1}
        return self.rows[:limit]


class SemanticVectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=Database(str(self.root/"olcr.db"));self.db.initialize()
        self.provider=FixtureEmbeddings();self.files=FileRetriever([str(self.root)])
        self.store=LocalVectorStore(self.db,self.provider,"fixture-embed",(str(self.root),),min_score=.25);self.evaluator=FixtureEvaluator();self.normalizer=FixtureNormalizer()
        self.router=RetrievalRouter(self.files,FTSRetriever(self.db),self.store,True,self.evaluator,self.normalizer)
    def tearDown(self):self.tmp.cleanup()
    def add(self,name,text):
        path=self.root/name;path.write_text(text);doc=self.db.index_document(str(path),name,text,{},time.time());self.store.index_document(doc,str(path),text);return path
    def test_av01_cedar_lantern_semantic_fallback(self):
        path=self.add("cedar.txt","The verified local runtime codename is Cedar Lantern.")
        rows,method=self.router.retrieve("wooden illumination nickname",5)
        self.assertEqual("semantic",method);self.assertEqual(str(path),rows[0].source);self.assertEqual("semantic",rows[0].method)
    def test_av02_authorization_semantics(self):
        path=self.add("auth.txt","A pending authorization binds the exact persisted action. Approval or rejection is final and replay is blocked.")
        rows,method=self.router.retrieve("handshake reuse prevention",5)
        self.assertEqual(("semantic",str(path)),(method,rows[0].source))
    def test_av03_context_budgeting(self):
        path=self.add("context.txt","Selective context chooses relevant evidence and avoids injecting the full corpus under a configured budget.")
        rows,method=self.router.retrieve("prompt allocation policy",5)
        self.assertEqual(("semantic",str(path)),(method,rows[0].source))
    def test_lexical_first_skips_query_embedding(self):
        self.add("exact.txt","exact-literal-present")
        before=len(self.provider.calls);rows,method=self.router.retrieve("exact-literal-present",5)
        self.assertIn(method,("ripgrep","python_fallback"));self.assertTrue(rows);self.assertEqual(before,len(self.provider.calls))
    def test_vector_disabled_keeps_lexical_runtime(self):
        self.add("exact.txt","disabled-vector-literal")
        router=RetrievalRouter(self.files,FTSRetriever(self.db),DisabledVectorStore(),False)
        self.assertTrue(router.retrieve("disabled-vector-literal",5)[0])
    def test_missing_model_degrades_without_provider_call(self):
        store=LocalVectorStore(self.db,self.provider,"",(str(self.root),));router=RetrievalRouter(self.files,FTSRetriever(self.db),store,True,self.evaluator,self.normalizer)
        before=len(self.provider.calls);self.assertEqual(([],"none"),router.retrieve("semantic missing phrase",5));self.assertEqual(before,len(self.provider.calls));self.assertEqual("model_unavailable",router.semantic_telemetry["state"])
    def test_changed_document_is_stale(self):
        path=self.add("changing.txt","The codename is Cedar Lantern.");path.write_text("The codename changed completely.")
        rows,method=self.router.retrieve("wooden illumination nickname",5)
        self.assertEqual(([],"none"),(rows,method));self.assertEqual(1,self.store.last_telemetry["stale_count"])
    def test_duplicate_chunks_are_deduplicated(self):
        text="The verified local runtime codename is Cedar Lantern."
        self.add("one.txt",text);self.add("two.txt",text)
        rows,method=self.router.retrieve("wooden illumination nickname",10)
        self.assertEqual("semantic",method);self.assertEqual(1,len(rows))
    def test_unauthorized_source_cannot_be_indexed(self):
        with tempfile.TemporaryDirectory() as outside:
            path=Path(outside)/"secret.txt";path.write_text("Cedar Lantern")
            doc=self.db.index_document(str(path),path.name,path.read_text(),{},time.time())
            with self.assertRaises(PermissionError):self.store.index_document(doc,str(path),path.read_text())
    def test_semantic_context_obeys_budget(self):
        self.add("long.txt","Cedar Lantern "+"evidence "*100)
        rows,_=self.router.retrieve("wooden illumination nickname",5)
        messages,selected=ContextManager(100).build("short",rows)
        self.assertLessEqual(sum(len(x["text"]) for x in selected),95);self.assertFalse(any("evidence "*20 in m["content"] for m in messages))
    def test_provider_failure_is_truthful_non_crashing_miss(self):
        self.add("unrelated.txt","unrelated material");self.provider.fail=True
        settings=Settings(allowed_roots=(str(self.root),),db_path=self.db.path,main_model="mock",embedding_model="fixture-embed",vector_enabled=True).validated()
        runtime=Runtime(settings,self.db,self.router,FakeModel());task,response=runtime.execute("search semantic-only-concept")
        self.assertEqual("completed",task.state.value);self.assertFalse(any("source" in x for x in task.selected_context))
        semantic=[x["semantic"] for x in task.selected_context if "semantic" in x][0]
        self.assertEqual("error",semantic["state"]);self.assertEqual("provider_error",semantic["error_category"])
    def test_schema_is_version_three_and_vectors_not_telemetry(self):
        self.add("schema.txt","The codename is Cedar Lantern.")
        with self.db.connect() as db:
            self.assertEqual(3,db.execute("SELECT version FROM schema_version").fetchone()[0])
            payload=json.dumps([dict(x) for x in db.execute("SELECT * FROM vector_embeddings")])
        self.assertIn("vector_json",payload)
        self.assertNotIn("vector_json",json.dumps(self.store.last_telemetry))
    def test_missing_ollama_model_never_initiates_pull(self):
        seen=[]
        def missing(req,timeout):
            seen.append(req.full_url);raise error.HTTPError(req.full_url,404,"missing",{},None)
        with patch("olcr_api.semantic.request.urlopen",side_effect=missing):
            with self.assertRaises(EmbeddingFailure) as caught:OllamaEmbeddingProvider("http://127.0.0.1:11434").embed(["text"],"missing-embed")
        self.assertEqual("model_unavailable",caught.exception.category);self.assertEqual(["http://127.0.0.1:11434/api/embed"],seen);self.assertFalse(any("pull" in x for x in seen))

    def test_first_selectable_relation_returns_exactly_one(self):
        rows=[SearchResult("one","direct",.8,1,"semantic"),SearchResult("two","also direct",.7,1,"semantic")]
        evaluator=FixtureEvaluator();router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer())
        selected,method=router.retrieve("concept",20)
        self.assertEqual(("semantic",["one"]),(method,[x.source for x in selected]));self.assertEqual(1,len(evaluator.calls))
        self.assertFalse(router.semantic_telemetry["abstained"]);self.assertEqual(.8,router.semantic_telemetry["selected_candidate"]["similarity_score"])

    def test_each_selectable_relation_selects_and_non_selectable_relations_abstain(self):
        rows=[SearchResult("candidate","authorized evidence",.8,1,"semantic")]
        for relation,expected in (("answers",True),("defines",True),("explains",True),("supports",True),("related",False),("unrelated",False)):
            evaluator=FixtureEvaluator();evaluator.relation=lambda query,candidate,current=relation:current
            router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer())
            selected,method=router.retrieve("concept",20)
            self.assertEqual(expected,bool(selected),relation);self.assertEqual("semantic" if expected else "none",method,relation)

    def test_reranker_selects_highest_score_at_frozen_threshold_without_margin_gate(self):
        rows=[SearchResult("one","first",.9,1,"semantic"),SearchResult("two","second",.8,1,"semantic"),SearchResult("three","third",.7,1,"semantic"),SearchResult("four","fourth",.6,1,"semantic")]
        reranker=FixtureReranker();reranker.values={"one":.010,"two":.0099,"three":.0098,"four":.99}
        evaluator=FixtureEvaluator();router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer(),reranker,.01)
        selected,method=router.retrieve("concept",20)
        self.assertEqual(("semantic",["one"]),(method,[x.source for x in selected]));self.assertEqual(3,len(reranker.calls));self.assertFalse(evaluator.calls)
        telemetry=router.semantic_telemetry;self.assertEqual(.01,telemetry["threshold"]);self.assertEqual(.0001,round(telemetry["top1_top2_gap"],4));self.assertEqual(3,telemetry["scored_count"])

    def test_reranker_below_threshold_nonfinite_or_failure_abstains(self):
        rows=[SearchResult("candidate","authorized",.8,1,"semantic")]
        for value,error,category in ((.009,None,None),(float("nan"),None,"non_finite_score"),(.5,"load_failed","load_failed")):
            reranker=FixtureReranker();reranker.values={"candidate":value};reranker.error=error
            router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,FixtureEvaluator(),FixtureNormalizer(),reranker,.01)
            self.assertEqual(([],"none"),router.retrieve("concept",20));self.assertTrue(router.semantic_telemetry["abstained"])
            if category:self.assertEqual(category,router.semantic_telemetry["reranker_error"])

    def test_first_rejected_second_accepted(self):
        rows=[SearchResult("wrong","unrelated",.8,1,"semantic"),SearchResult("right","direct",.7,1,"semantic")]
        evaluator=FixtureEvaluator();evaluator.relation=lambda query,candidate:"explains" if candidate.source=="right" else "related"
        router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer())
        selected,method=router.retrieve("concept",20)
        self.assertEqual(("semantic",["right"]),(method,[x.source for x in selected]));self.assertEqual(2,len(evaluator.calls))

    def test_all_rejected_abstains_with_bounded_judgments(self):
        rows=[SearchResult(str(i),"unrelated",.9-i/10,1,"semantic") for i in range(5)]
        evaluator=FixtureEvaluator();evaluator.relation=lambda query,candidate:"unrelated"
        router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer())
        self.assertEqual(([],"none"),router.retrieve("no match",20));self.assertEqual(3,len(evaluator.calls))
        self.assertTrue(router.semantic_telemetry["abstained"]);self.assertEqual(3,router.semantic_telemetry["candidates_evaluated"])

    def test_malformed_timeout_or_provider_failed_evaluator_abstains(self):
        rows=[SearchResult("candidate","text",.8,1,"semantic")]
        for category in ("invalid_response","timeout","provider_unavailable"):
            evaluator=FixtureEvaluator();evaluator.error=category;router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,evaluator,FixtureNormalizer())
            self.assertEqual(([],"none"),router.retrieve("concept",20));self.assertEqual(category,router.semantic_telemetry["relation_error"])
            self.assertTrue(router.semantic_telemetry["abstained"]);self.assertFalse(router.semantic_telemetry["selected"])

    def test_unauthorized_candidate_is_filtered_before_judgment(self):
        with tempfile.TemporaryDirectory() as outside:
            path=Path(outside)/"outside.txt";text="The codename is Cedar Lantern.";path.write_text(text)
            doc=self.db.index_document(str(path),path.name,text,{},time.time())
            with self.db.connect() as db:
                db.execute("INSERT INTO vector_embeddings(document_id,chunk_ordinal,line_start,text,content_hash,document_hash,model,dimension,index_version,vector_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(doc,0,1,text,"content","document","fixture-embed",3,"olcr-lines-v1",json.dumps([1.,0.,0.]),time.time()))
            before=len(self.evaluator.calls);self.assertEqual(([],"none"),self.router.retrieve("wooden illumination nickname",5));self.assertEqual(before,len(self.evaluator.calls))
            self.assertEqual(1,self.router.semantic_telemetry["stale_count"])

    def test_lexical_success_bypasses_embedding_and_relation_evaluation(self):
        self.add("exact-admissibility.txt","exact-admissibility-literal")
        embeddings,evaluations,normalizations=len(self.provider.calls),len(self.evaluator.calls),len(self.normalizer.calls)
        rows,method=self.router.retrieve("exact-admissibility-literal",5)
        self.assertTrue(rows);self.assertIn(method,("ripgrep","python_fallback"));self.assertEqual((embeddings,evaluations,normalizations),(len(self.provider.calls),len(self.evaluator.calls),len(self.normalizer.calls)))
        self.assertFalse(self.router.semantic_telemetry["attempted"])

    def test_independent_model_selection_keeps_answer_and_embedding_models(self):
        settings=Settings(main_model="qwen3.8:latest",embedding_model="embeddinggemma:latest",semantic_judge_model="qwen3.6:latest",allowed_roots=(str(self.root),)).validated()
        store=LocalVectorStore(self.db,self.provider,settings.embedding_model,settings.allowed_roots)
        evaluator=OllamaSemanticRelationEvaluator(settings.ollama_endpoint,settings.semantic_judge_model)
        self.assertEqual("qwen3.8:latest",settings.main_model);self.assertEqual("embeddinggemma:latest",store.model);self.assertEqual("qwen3.6:latest",evaluator.model)

    def test_admissibility_bound_is_256_and_normalization_bound_is_unchanged(self):
        self.assertEqual(256,ADMISSIBILITY_MAX_TOKENS);self.assertEqual(96,NORMALIZATION_MAX_TOKENS)

    def test_structured_relation_contract_parses_selectable_and_non_selectable_relations(self):
        calls=[];responses=iter([
            {"message":{"content":json.dumps({"relation":"explains","reason":"Candidate directly explains the requested policy using a semantic paraphrase.","candidate_id":"candidate-1","evidence":"Selecting relevant evidence"})}},
            {"message":{"content":json.dumps({"relation":"related","reason":"Candidate is topically adjacent but lacks the requested information.","candidate_id":"candidate-2","evidence":""})}},
        ])
        def fake_urlopen(req,timeout): calls.append(json.loads(req.data));return io.BytesIO(json.dumps(next(responses)).encode())
        evaluator=OllamaSemanticRelationEvaluator("http://127.0.0.1:11434","qwen3.6:latest")
        intent=SemanticIntent("select prompt evidence","prompt allocation",(),"qwen3.6:latest",1)
        with patch("olcr_api.semantic.request.urlopen",side_effect=fake_urlopen):
            selected=evaluator.evaluate("prompt allocation policy",intent,SearchResult("context.txt","Selecting relevant evidence under a bounded context budget.",.4),"candidate-1")
            adjacent=evaluator.evaluate("prompt allocation policy",intent,SearchResult("garden.txt","Garden irrigation notes.",.3),"candidate-2")
        self.assertEqual(("explains","related"),(selected.relation,adjacent.relation));self.assertEqual(2,len(calls))
        self.assertEqual(RELATION_CLASSIFIER_SYSTEM_PROMPT,calls[0]["messages"][0]["content"])
        self.assertIn(selected.evidence,"Selecting relevant evidence under a bounded context budget.")
        self.assertIn("Semantic paraphrases count",RELATION_CLASSIFIER_SYSTEM_PROMPT);self.assertIn("Do not decide whether OLCR should select",RELATION_CLASSIFIER_SYSTEM_PROMPT)

    def test_relation_parser_categories_fail_closed(self):
        intent=SemanticIntent("intent","request",(),"qwen3.6:latest",1);candidate=SearchResult("candidate.txt","authorized evidence",.8)
        cases=[
            ({"relation":"answers","reason":"x","candidate_id":"wrong","evidence":"authorized evidence"},"candidate_id_mismatch"),
            ({"relation":"invalid","reason":"x","candidate_id":"candidate-1","evidence":""},"invalid_relation"),
            ({"relation":"answers","reason":"x","candidate_id":"candidate-1","evidence":""},"missing_required_evidence"),
            ({"relation":"answers","candidate_id":"candidate-1","evidence":"authorized evidence"},"schema_mismatch"),
        ]
        for value,category in cases:
            evaluator=OllamaSemanticRelationEvaluator("http://127.0.0.1:11434","qwen3.6:latest")
            with patch("olcr_api.semantic.request.urlopen",return_value=io.BytesIO(json.dumps({"message":{"content":json.dumps(value)}}).encode())):
                with self.assertRaises(SemanticRelationFailure) as caught:evaluator.evaluate("concept",intent,candidate,"candidate-1")
            self.assertEqual(category,caught.exception.category)

    def test_structured_normalizer_parses_once_and_preserves_query_constraints(self):
        calls=[];response={"message":{"content":json.dumps({"intent":"Find a policy for selecting evidence within limited prompt capacity.","requested_information":"Prompt/context allocation policy","constraints":["limited capacity","preserve requested meaning"]})}}
        def fake_urlopen(req,timeout): calls.append(json.loads(req.data));return io.BytesIO(json.dumps(response).encode())
        normalizer=OllamaIntentNormalizer("http://127.0.0.1:11434","qwen3.6:latest")
        with patch("olcr_api.semantic.request.urlopen",side_effect=fake_urlopen): intent=normalizer.normalize("prompt allocation policy")
        self.assertEqual("Prompt/context allocation policy",intent.requested_information);self.assertEqual(1,len(calls));self.assertEqual(NORMALIZATION_SYSTEM_PROMPT,calls[0]["messages"][0]["content"])
        self.assertEqual({"query":"prompt allocation policy"},json.loads(calls[0]["messages"][1]["content"]))

    def test_normalizer_failure_and_invalid_evidence_abstain(self):
        self.normalizer.error="invalid_response";self.assertEqual(([],"none"),self.router.retrieve("concept",5));self.assertEqual("invalid_response",self.router.semantic_telemetry["normalization"]["error"])
        rows=[SearchResult("candidate","authorized evidence",.8,1,"semantic")]
        class BadEvidenceEvaluator(FixtureEvaluator):
            def evaluate(self,query,intent,candidate,candidate_id): return SemanticRelation("answers","bad evidence",candidate_id,"fabricated",self.model,1)
        router=RetrievalRouter(self.files,FTSRetriever(self.db),StaticVectorStore(rows),True,BadEvidenceEvaluator(),FixtureNormalizer())
        self.assertEqual(([],"none"),router.retrieve("concept",20));self.assertEqual("failed",router.semantic_telemetry["relation_decisions"][0]["evidence_validation"])


if __name__=="__main__":unittest.main()
