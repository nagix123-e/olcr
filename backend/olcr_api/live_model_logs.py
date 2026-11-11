"""Received reasoning and host progress, scoped to a conversation and invocation."""
import json
import time
from fastapi import HTTPException

def _table(con):
    con.execute('CREATE TABLE IF NOT EXISTS live_model_logs(id INTEGER PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,invocation_id TEXT NOT NULL,kind TEXT NOT NULL,data_json TEXT NOT NULL,created_at REAL NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS live_model_logs_conversation ON live_model_logs(conversation_id,id)')

def record_model_log(db,cid,data):
    if not cid:return
    now=time.time();data={**data,'at':now}
    # Model channel snapshots replace only their own invocation; host events
    # remain separate so a failed model's trace never joins the next model.
    with db.connect() as con:
        _table(con)
        if not con.execute('SELECT 1 FROM conversations WHERE id=?',(cid,)).fetchone():return
        if data.get('state')=='request_completed':
            rows=con.execute("SELECT id,data_json FROM live_model_logs WHERE conversation_id=? AND invocation_id=? AND kind='reasoning'",(cid,data.get('invocation_id',''))).fetchall()
            for row in rows:
                con.execute('UPDATE live_model_logs SET data_json=? WHERE id=?',(json.dumps({**json.loads(row[1]),'complete':True},ensure_ascii=False),row[0]))
        if data.get('state')=='worker_progress':
            # One latest snapshot per invocation/stage; lifecycle and received
            # reasoning must survive arbitrarily long generations.
            previous=con.execute("SELECT id,data_json FROM live_model_logs WHERE conversation_id=? AND invocation_id=? AND kind='worker_progress' ORDER BY id DESC LIMIT 1",(cid,data.get('invocation_id',''))).fetchone()
            if previous and json.loads(previous[1]).get('event')==data.get('event'):
                con.execute('UPDATE live_model_logs SET data_json=?,created_at=? WHERE id=?',(json.dumps(data,ensure_ascii=False),now,previous[0]));return
        if data.get('state')=='reasoning':
            text=data.get('text');
            if not isinstance(text,str) or not text:return
            previous=con.execute("SELECT id,data_json FROM live_model_logs WHERE conversation_id=? AND invocation_id=? AND kind='reasoning' ORDER BY id DESC LIMIT 1",(cid,data.get('invocation_id',''))).fetchone()
            before=json.loads(previous[1]) if previous else {}
            combined=text if data.get('snapshot') else before.get('text','')+text
            data['text']=combined[:64000];data['truncated']=len(combined)>64000 or before.get('truncated',False)
            if previous:
                con.execute('UPDATE live_model_logs SET data_json=?,created_at=? WHERE id=?',(json.dumps(data,ensure_ascii=False),now,previous[0]));return
        con.execute('INSERT INTO live_model_logs(conversation_id,invocation_id,kind,data_json,created_at) VALUES(?,?,?,?,?)',(cid,data.get('invocation_id',''),data.get('state','status'),json.dumps(data,ensure_ascii=False),now))

def register_model_log_routes(app,database):
    @app.get('/api/conversations/{cid}/model-logs')
    def logs(cid:str):
        db=database()
        with db.connect() as con:
            _table(con)
            if not con.execute('SELECT 1 FROM conversations WHERE id=?',(cid,)).fetchone():raise HTTPException(404,'CONVERSATION_NOT_FOUND')
            turn=con.execute("SELECT id,created_at FROM messages WHERE conversation_id=? AND role='user' ORDER BY ordinal DESC LIMIT 1",(cid,)).fetchone()
            active=con.execute("SELECT MIN(created_at) FROM coding_tasks WHERE conversation_id=? AND archived=0 AND status IN ('QUEUED','PLANNING','RUNNING','FINAL_REPORTING','QWEN_REPLANNING')",(cid,)).fetchone()[0]
            since=min(turn[1],active) if turn and active else turn[1] if turn else 0
            rows=con.execute('SELECT data_json FROM live_model_logs WHERE conversation_id=? AND created_at>=? ORDER BY id DESC LIMIT 1000',(cid,since)).fetchall()
        return {'turn_id':turn[0] if turn else None,'events':[json.loads(row[0]) for row in reversed(rows)]}
