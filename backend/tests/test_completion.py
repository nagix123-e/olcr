import json
from pathlib import Path
import threading
import time
import unittest
import tempfile

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.artifacts import ArtifactStore
from olcr_api.db import Database
from olcr_api.models import SearchResult, Task, TaskState
from olcr_api.runtime import Runtime


class SlowModel:
    def __init__(self): self.messages=[]
    def generate(self, messages, model, stream=False):
        self.messages=messages
        if not stream: return {"text":"ok","latency_ms":1}
        def parts():
            for text in ("one ","two ","three"):
                time.sleep(.08); yield {"text":text,"done":text=="three"}
        return parts()


class CounterModel:
    def __init__(self, counters=True): self.counters=counters
    def generate(self,messages,model,stream=False):
        def parts():
            yield {"text":"ok","done":False,"prompt_tokens":None,"completion_tokens":None}
            last={"text":"","done":True}
            if self.counters: last.update(prompt_tokens=7,completion_tokens=3)
            yield last
        return parts()


class CompletionTests(unittest.TestCase):
    def setUp(self): self.client=TestClient(api.app); self.root=Path(api.settings.allowed_roots[0])
    def test_conversation_persists_messages_in_order(self):
        first=self.client.post("/api/chat",json={"message":"lowercase: HELLO"}).json(); cid=first["conversation_id"]
        second=self.client.post("/api/chat",json={"message":"calculate 2+2","conversation_id":cid})
        self.assertEqual(200,second.status_code)
        messages=self.client.get(f"/api/conversations/{cid}").json()["messages"]
        self.assertEqual(["user","assistant","user","assistant"],[x["role"] for x in messages])
        self.assertEqual([0,1,2,3],[x["ordinal"] for x in messages])
    def test_confirmation_resume_mismatch_replay_and_reject(self):
        target=self.root/"confirmed.txt"; pending=api.runtime.execute(f"write file {target}: exact content")[0]
        api.db.save_task(pending,"parent-update-conversation")
        with api.db.connect() as db: action=db.execute("SELECT action_id FROM pending_actions WHERE task_id=?",(pending.id,)).fetchone()[0]
        with self.assertRaises(PermissionError): api.runtime.resolve_confirmation(pending.id,"wrong",True)
        task,_=api.runtime.resolve_confirmation(pending.id,action,True); self.assertEqual("completed",task.state.value); self.assertEqual("exact content",target.read_text())
        with self.assertRaises(PermissionError): api.runtime.resolve_confirmation(pending.id,action,True)
        other=self.root/"rejected.txt"; p2=api.runtime.execute(f"write file {other}: no")[0]
        api.db.save_task(p2,"parent-update-conversation")
        with api.db.connect() as db: a2=db.execute("SELECT action_id FROM pending_actions WHERE task_id=?",(p2.id,)).fetchone()[0]
        self.assertEqual("cancelled",api.runtime.resolve_confirmation(p2.id,a2,False)[0].state.value); self.assertFalse(other.exists())
        with self.assertRaises(PermissionError): api.runtime.resolve_confirmation(p2.id,a2,False)
    def test_stale_confirmation(self):
        target=self.root/"stale.txt"; task=api.runtime.execute(f"write file {target}: no")[0]
        with api.db.connect() as db:
            action=db.execute("SELECT action_id FROM pending_actions WHERE task_id=?",(task.id,)).fetchone()[0]; db.execute("UPDATE pending_actions SET expires_at=0 WHERE action_id=?",(action,))
        with self.assertRaises(PermissionError): api.runtime.resolve_confirmation(task.id,action,True)
        self.assertFalse(target.exists())
    def test_confirmation_survives_database_reopen(self):
        target=self.root/"restart-confirmed.txt"; pending=api.runtime.execute(f"write file {target}: reopened")[0]
        api.db.save_task(pending,"restart-conversation")
        with api.db.connect() as db: action=db.execute("SELECT action_id FROM pending_actions WHERE task_id=?",(pending.id,)).fetchone()[0]
        reopened=Database(api.db.path); reopened.initialize(); resumed=Runtime(api.settings,reopened,api.retrieval,api.runtime.model)
        task,_=resumed.resolve_confirmation(pending.id,action,True)
        self.assertEqual("completed",task.state.value); self.assertEqual("reopened",target.read_text())
    def test_task_update_preserves_pending_and_telemetry_children(self):
        task=Task("preserve children"); task.transition(TaskState.ROUTING); task.transition(TaskState.EXECUTING)
        task.tool_executions=[{"tool":"lowercase","version":"1.0","risk":"SAFE","input":{"text":"A"},"output":{"text":"a"},"status":"success","latency_ms":1}]
        task.model_calls=[{"model":"mock","prompt_tokens":2,"completion_tokens":1,"latency_ms":3,"status":"success"}]
        api.db.save_task(task)
        with api.db.connect() as db:
            db.execute("INSERT INTO pending_actions VALUES(?,?,?,?,?,?,?)",(task.id,"preserve-action","write_text",json.dumps({"path":str(self.root/"unused.txt"),"content":"x"}),time.time()+60,"pending",time.time()))
            tool_id=db.execute("SELECT id FROM tool_executions WHERE task_id=?",(task.id,)).fetchone()[0]
            model_id=db.execute("SELECT id FROM model_calls WHERE task_id=?",(task.id,)).fetchone()[0]
        task.updated_at=time.time(); api.db.save_task(task,"preserved-conversation")
        with api.db.connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM pending_actions WHERE task_id=?",(task.id,)).fetchone())
            self.assertEqual([tool_id],[x[0] for x in db.execute("SELECT id FROM tool_executions WHERE task_id=?",(task.id,))])
            self.assertEqual([model_id],[x[0] for x in db.execute("SELECT id FROM model_calls WHERE task_id=?",(task.id,))])
    def test_artifact_bounded_and_guarded(self):
        task=Task("artifact"); task.transition(TaskState.ROUTING); task.transition(TaskState.EXECUTING); api.db.save_task(task)
        store=ArtifactStore(str(self.root/"managed-artifacts"),api.db,max_bytes=240)
        meta=store.create(task.id,[SearchResult(f"s{i}","x"*60) for i in range(20)])
        self.assertLessEqual(meta["size_bytes"],240); self.assertLess(meta["result_count"],20); self.assertTrue(store.read(meta["id"],0,2)["results"])
        with api.db.connect() as db: db.execute("UPDATE artifacts SET path='/etc/passwd' WHERE id=?",(meta["id"],))
        with self.assertRaises(PermissionError): store.read(meta["id"],0,2)
    def test_settings_validation_and_persistence(self):
        before=self.client.get("/api/settings").json(); bad={**before,"ollama_endpoint":"https://remote.example"}
        self.assertEqual(422,self.client.put("/api/settings",json=bad).status_code); self.assertEqual(before,self.client.get("/api/settings").json())
        good={**before,"context_budget":before["context_budget"]+1}; self.assertEqual(200,self.client.put("/api/settings",json=good).status_code)
        self.assertEqual(good["context_budget"],api.db.load_settings()["context_budget"])
    def test_invalid_lifecycle_transition(self):
        with self.assertRaises(ValueError): Task("x").transition(TaskState.COMPLETED)
    def test_streaming_and_cancellation(self):
        old=api.runtime.model; provider=SlowModel(); api.runtime.model=provider
        completed=self.client.post("/api/chat/stream",json={"message":"stream a short answer"}).text
        self.assertGreaterEqual(completed.count('"type": "chunk"'),3); self.assertIn('"type": "done"',completed)
        result={}
        def call(): result["response"]=self.client.post("/api/chat/stream",json={"message":"create a short answer"})
        thread=threading.Thread(target=call); thread.start()
        for _ in range(50):
            if api.cancel_events: break
            time.sleep(.01)
        self.assertTrue(api.cancel_events); task_id=next(iter(api.cancel_events)); self.assertEqual(200,self.client.post(f"/api/tasks/{task_id}/cancel").status_code)
        thread.join(3); body=result["response"].text; self.assertIn('"type": "cancelled"',body)
        with api.db.connect() as db: self.assertEqual("cancelled",db.execute("SELECT state FROM tasks WHERE id=?",(task_id,)).fetchone()[0])
        api.runtime.model=old
    def test_retrieval_then_streamed_synthesis_uses_selected_context(self):
        (self.root/"evidence.txt").write_text("unique-stream-evidence")
        old=api.runtime.model; provider=SlowModel(); api.runtime.model=provider
        body=self.client.post("/api/chat/stream",json={"message":"search unique-stream-evidence and summarize"}).text
        self.assertIn('"type": "done"',body); self.assertTrue(any("unique-stream-evidence" in m["content"] for m in provider.messages))
        api.runtime.model=old
    def test_literal_fts_queries_are_safe(self):
        api.db.index_document("literal","literal","OLCR LIVE FACT 7F3A AND quoted colon parenthesis",{},time.time())
        self.assertTrue(api.db.search_fts("OLCR-LIVE-FACT-7F3A",10))
        self.assertTrue(api.db.search_fts('AND: "quoted" (colon) parenthesis',10))
        self.assertEqual([],api.db.search_fts('--- ::: () ""',10))
    def test_authorized_hidden_directory_is_searched_but_outside_is_not(self):
        hidden=self.root/".authorized-hidden"; hidden.mkdir(exist_ok=True); target=hidden/"fact.txt"; target.write_text("hidden-authorized-literal")
        rows=api.files.search("hidden-authorized-literal",10); self.assertTrue(any(x.source==str(target) for x in rows))
        with tempfile.TemporaryDirectory() as outside:
            external=Path(outside)/".hidden"; external.mkdir(); (external/"fact.txt").write_text("outside-hidden-literal")
            self.assertEqual([],api.files.search("outside-hidden-literal",10))
    def test_retrieval_failure_persists_failed_task_and_assistant_error(self):
        old_file,old_fts=api.retrieval.files.search,api.retrieval.fts.search
        api.retrieval.files.search=lambda q,l: (_ for _ in ()).throw(RuntimeError("private detail"))
        api.retrieval.fts.search=lambda q,l: (_ for _ in ()).throw(RuntimeError("private detail"))
        response=self.client.post("/api/chat/stream",json={"message":"search forced-failure and summarize"})
        api.retrieval.files.search,api.retrieval.fts.search=old_file,old_fts
        events=[json.loads(x[6:]) for x in response.text.split("\n\n") if x.startswith("data: ")]
        failed=events[-1]["task"]; self.assertEqual("failed",failed["state"]); self.assertNotIn("private detail",events[-1]["message"])
        with api.db.connect() as db:
            row=db.execute("SELECT state,error,conversation_id FROM tasks WHERE id=?",(failed["id"],)).fetchone()
            self.assertEqual("failed",row["state"]); self.assertTrue(row["error"].startswith("retrieval_failed:"))
            roles=[x[0] for x in db.execute("SELECT role FROM messages WHERE conversation_id=? ORDER BY ordinal",(row["conversation_id"],))]
            self.assertEqual(["user","assistant"],roles)
    def test_stream_token_counters_present_and_absent(self):
        old=api.runtime.model
        api.runtime.model=CounterModel(True); text=self.client.post("/api/chat/stream",json={"message":"generate counter metadata"}).text
        task=[json.loads(x[6:]) for x in text.split("\n\n") if x.startswith("data: ")][-1]["task"]
        self.assertEqual((7,3),(task["model_calls"][0]["prompt_tokens"],task["model_calls"][0]["completion_tokens"]))
        api.runtime.model=CounterModel(False); text=self.client.post("/api/chat/stream",json={"message":"generate absent metadata"}).text
        task=[json.loads(x[6:]) for x in text.split("\n\n") if x.startswith("data: ")][-1]["task"]
        self.assertIsNone(task["model_calls"][0]["prompt_tokens"]); self.assertIsNone(task["model_calls"][0]["completion_tokens"])
        api.runtime.model=old


if __name__=="__main__": unittest.main()
