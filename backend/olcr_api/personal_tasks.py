"""User-scoped, bounded personal jobs. Workers never execute commands or mutations.

A run is claimed durably before I/O. Expired leases are reported as interrupted,
never replayed; this deliberately prefers a missed check to duplicate work.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .permissions import capability_enabled, level


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ProfilePreferences(Input):
    display_name: str = Field(default='', max_length=100)
    time_zone: str = Field(default='Asia/Tokyo', max_length=100)
    language: Literal['ja', 'en'] = 'ja'
    response_style: Literal['concise', 'balanced', 'detailed'] = 'balanced'
    personalization_enabled: bool = False
    dietary_preferences: str = Field(default='', max_length=1000)
    accessibility_preferences: str = Field(default='', max_length=1000)
    interests: str = Field(default='', max_length=2000)
    instructions: str = Field(default='', max_length=4000)
    background_enabled: bool = False
    mail_enabled: bool = False
    web_enabled: bool = False
    model_enabled: bool = False

    @field_validator('time_zone')
    @classmethod
    def zone(cls, value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('TIME_ZONE_INVALID') from None
        return value


class GoalDefinition(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    progress: float = Field(default=0, ge=0, le=1000000000)
    target: float = Field(default=100, gt=0, le=1000000000)
    unit: str = Field(default='%', max_length=50)
    status: Literal['active', 'paused', 'completed', 'stopped'] = 'active'
    due_date: str | None = None

    @field_validator('due_date')
    @classmethod
    def due(cls, value):
        if value is not None: date.fromisoformat(value)
        return value

    @model_validator(mode='after')
    def bounds(self):
        if self.progress > self.target: raise ValueError('GOAL_PROGRESS_EXCEEDS_TARGET')
        return self


class TaskDefinition(Input):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal['reminder', 'mail_review', 'web_watch', 'model_review'] = 'reminder'
    enabled: bool = False
    goal_id: str | None = Field(default=None, max_length=128)
    next_run_at: float = Field(default_factory=time.time, ge=0, le=253402300799)
    interval_seconds: int | None = Field(default=None, ge=300, le=31536000)
    connector_id: Literal['service.google.gmail', 'service.apple.mail'] = 'service.google.gmail'
    query: str = Field(default='', max_length=500)
    prompt: str = Field(default='', max_length=4000)
    model_id: str = Field(default='', max_length=512)

    @field_validator('query')
    @classmethod
    def no_urls(cls, value):
        if re.search(r'(?:https?://|file:|localhost|\b127\.0\.0\.)', value, re.I):
            raise ValueError('SEARCH_QUERY_REQUIRED_NOT_URL')
        return value

    @model_validator(mode='after')
    def required(self):
        if not self.title.strip(): raise ValueError('TASK_TITLE_REQUIRED')
        if self.kind == 'web_watch' and not self.query.strip(): raise ValueError('SEARCH_QUERY_REQUIRED')
        if self.kind == 'model_review' and not self.prompt.strip(): raise ValueError('MODEL_PROMPT_REQUIRED')
        return self


class DeadlineProposal(Input):
    title: str = Field(min_length=1, max_length=160)
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    message_id: str = Field(min_length=1, max_length=512)

    @field_validator('date')
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


class DeadlineExtraction(Input):
    proposals: list[DeadlineProposal] = Field(default_factory=list, max_length=30)


class ModelReviewResult(Input):
    summary: str = Field(min_length=1, max_length=12000)
    steps: list[str] = Field(default_factory=list, max_length=12)

    @field_validator('steps')
    @classmethod
    def bounded_steps(cls, value):
        if any(len(step) > 1000 for step in value): raise ValueError('MODEL_STEPS_TOO_LONG')
        return value


def component(host, key):
    value = host.get(key)
    return value() if callable(value) else value


def deadline_proposals(message):
    """Conservative extraction: explicit ISO/Japanese full dates near a deadline cue.

    These are untrusted proposals, not instructions. No message bodies or URLs are
    retained; ambiguous relative dates are intentionally left for the user.
    """
    subject = str(message.get('subject') or next((h.get('value', '') for h in message.get('headers', []) if str(h.get('name', '')).lower() == 'subject'), '') or 'Mail deadline')[:160]
    text = (subject + '\n' + str(message.get('content', ''))[:20000])
    proposals = []
    for line in text.splitlines():
        if not re.search(r'deadline|due\b|submit|by\b|締[め切]|期限|提出|まで', line, re.I): continue
        for match in re.finditer(r'(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|(?=\D|$))', line):
            try: day = date(*map(int, match.groups())).isoformat()
            except ValueError: continue
            if day not in {p['date'] for p in proposals}:
                proposals.append({'title':subject, 'date':day, 'message_id':str(message.get('id', ''))[:512], 'requires_review':True})
    return proposals[:10]


class PersonalTasks:
    LEASE_SECONDS = 600

    def __init__(self, db, host, owner='local-user'):
        if not isinstance(owner, str) or not 1 <= len(owner) <= 128: raise ValueError('PERSONAL_OWNER_INVALID')
        self.db, self.host, self.owner = db, host, owner
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        with db.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS personal_profiles(owner TEXT PRIMARY KEY, data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_goals(id TEXT PRIMARY KEY, owner TEXT NOT NULL, data_json TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_goals_owner ON personal_goals(owner);
                CREATE TABLE IF NOT EXISTS personal_tasks(id TEXT PRIMARY KEY, owner TEXT NOT NULL, data_json TEXT NOT NULL, status TEXT NOT NULL,
                    next_run_at REAL NOT NULL, version INTEGER NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_tasks_due ON personal_tasks(owner,status,next_run_at);
                CREATE TABLE IF NOT EXISTS personal_task_runs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    due_at REAL NOT NULL, started_at REAL NOT NULL, lease_until REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', UNIQUE(task_id,version,due_at));
                CREATE TABLE IF NOT EXISTS personal_inbox(id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
                    created_at REAL NOT NULL, read_at REAL, result_json TEXT NOT NULL);
            ''')

    def profile(self):
        with self.db.connect() as con: row = con.execute('SELECT data_json FROM personal_profiles WHERE owner=?', (self.owner,)).fetchone()
        return json.loads(row[0]) if row else ProfilePreferences().model_dump()

    def save_profile(self, value):
        data = ProfilePreferences.model_validate(value).model_dump()
        with self.db.connect() as con:
            con.execute('INSERT INTO personal_profiles VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET data_json=excluded.data_json', (self.owner, json.dumps(data, ensure_ascii=False)))
        return data

    def callback_profile(self):
        profile = self.profile()
        if profile.get('personalization_enabled'): return profile
        return {key:profile[key] for key in ('language','time_zone')}

    def profile_context(self):
        profile = self.profile()
        if not profile.get('personalization_enabled'): return ''
        # Explicit user-entered preferences only; no automatic inference.
        fields = ('display_name', 'time_zone', 'language', 'response_style', 'dietary_preferences', 'accessibility_preferences', 'interests', 'instructions')
        return 'User-provided personal preferences (apply when relevant):\n' + '\n'.join(f'{key}: {profile[key]}' for key in fields if profile.get(key))

    def goals(self):
        with self.db.connect() as con: rows = con.execute('SELECT * FROM personal_goals WHERE owner=? ORDER BY updated_at DESC,id', (self.owner,)).fetchall()
        return [{'id':r['id'], **json.loads(r['data_json']), 'updated_at':r['updated_at']} for r in rows]

    def save_goal(self, value, ident=None):
        value = GoalDefinition.model_validate(value)
        if ident and not any(g['id'] == ident for g in self.goals()): raise ValueError('GOAL_NOT_FOUND')
        ident = ident or uuid.uuid4().hex
        with self.db.connect() as con:
            con.execute('INSERT INTO personal_goals VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at WHERE owner=excluded.owner', (ident, self.owner, value.model_dump_json(), time.time()))
        return next(g for g in self.goals() if g['id'] == ident)

    @staticmethod
    def _task(row):
        return {**json.loads(row['data_json']), 'id':row['id'], 'status':row['status'], 'next_run_at':row['next_run_at'], 'version':row['version'], 'updated_at':row['updated_at']}

    def tasks(self):
        with self.db.connect() as con: rows = con.execute('SELECT * FROM personal_tasks WHERE owner=? ORDER BY updated_at DESC,id', (self.owner,)).fetchall()
        return [self._task(r) for r in rows]

    def get_task(self, ident):
        with self.db.connect() as con: row = con.execute('SELECT * FROM personal_tasks WHERE owner=? AND id=?', (self.owner, ident)).fetchone()
        if not row: raise ValueError('TASK_NOT_FOUND')
        return self._task(row)

    def save_task(self, value, ident=None):
        value = TaskDefinition.model_validate(value)
        old = self.get_task(ident) if ident else None
        if value.goal_id and not any(g['id'] == value.goal_id for g in self.goals()): raise ValueError('GOAL_NOT_FOUND')
        if not old and len(self.tasks()) >= 100: raise ValueError('TASK_LIMIT_REACHED')
        ident = ident or uuid.uuid4().hex
        with self.db.connect() as con:
            con.execute('INSERT INTO personal_tasks VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json,status=excluded.status,next_run_at=excluded.next_run_at,version=personal_tasks.version+1,updated_at=excluded.updated_at WHERE owner=excluded.owner',
                        (ident, self.owner, value.model_dump_json(), 'active' if value.enabled else 'paused', value.next_run_at, 1, time.time()))
        return self.get_task(ident)

    def control(self, ident, action, now=None):
        if action not in ('pause', 'resume', 'stop'): raise ValueError('TASK_ACTION_INVALID')
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM personal_tasks WHERE id=? AND owner=?', (ident, self.owner)).fetchone()
            if not row: raise ValueError('TASK_NOT_FOUND')
            if action == 'resume' and row['status'] == 'stopped': raise ValueError('TASK_STOPPED_CREATE_OR_EDIT_TO_RESTART')
            data = json.loads(row['data_json']); data['enabled'] = action == 'resume'
            con.execute('UPDATE personal_tasks SET status=?,data_json=?,version=version+1,next_run_at=?,updated_at=? WHERE id=? AND owner=?',
                        ({'pause':'paused','resume':'active','stop':'stopped'}[action], json.dumps(data), (time.time() if now is None else now) if action == 'resume' else row['next_run_at'], time.time(), ident, self.owner))
        return self.get_task(ident)

    def runs(self, task_id=None):
        if task_id: self.get_task(task_id)
        with self.db.connect() as con:
            rows = con.execute('SELECT * FROM personal_task_runs WHERE owner=?' + (' AND task_id=?' if task_id else '') + ' ORDER BY started_at DESC,rowid DESC LIMIT 100', (self.owner, task_id) if task_id else (self.owner,)).fetchall()
        return [{**dict(r), 'result':json.loads(r['result_json'])} for r in rows]

    def inbox(self):
        with self.db.connect() as con: rows = con.execute('SELECT * FROM personal_inbox WHERE owner=? ORDER BY created_at DESC,rowid DESC LIMIT 100', (self.owner,)).fetchall()
        return [{k:v for k,v in dict(r).items() if k not in ('owner','result_json')} | {'result':json.loads(r['result_json'])} for r in rows]

    def mark_read(self, ident):
        with self.db.connect() as con:
            changed = con.execute('UPDATE personal_inbox SET read_at=? WHERE owner=? AND id=?', (time.time(), self.owner, ident)).rowcount
        if not changed: raise ValueError('NOTIFICATION_NOT_FOUND')
        return {'ok':True}

    def _allowed(self, task):
        profile = self.profile()
        if not profile['background_enabled']: raise ValueError('BACKGROUND_DISABLED')
        kind = task['kind']
        setting = {'mail_review':'mail_enabled','web_watch':'web_enabled','model_review':'model_enabled'}.get(kind)
        if setting and not profile[setting]: raise ValueError('TASK_CATEGORY_DISABLED')
        context = self.host()
        if kind in ('mail_review','web_watch'):
            settings = component(context, 'settings')
            if not capability_enabled(settings, 'external_access_enabled'): raise ValueError('EXTERNAL_ACCESS_REQUIRED')
            if level(settings) == 'confirm': raise ValueError('BACKGROUND_REQUIRES_AUTOMATIC_PERMISSION')
        if kind == 'mail_review':
            connectors = component(context, 'service_connectors')
            if connectors is None: raise ValueError('MAIL_REVIEW_UNAVAILABLE')
            connectors.authorize(task['connector_id'], 'read')
        return context

    def claim_due(self, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            expired = con.execute("SELECT * FROM personal_task_runs WHERE owner=? AND status='running' AND lease_until<=?", (self.owner, now)).fetchall()
            for run in expired:
                con.execute("UPDATE personal_task_runs SET status='interrupted',finished_at=?,error='BACKEND_INTERRUPTED' WHERE id=?", (now,run['id']))
                task = con.execute('SELECT * FROM personal_tasks WHERE id=? AND owner=?', (run['task_id'],self.owner)).fetchone()
                if task and task['version'] == run['version']:
                    data = json.loads(task['data_json']); interval = data.get('interval_seconds')
                    # Restart recovery never runs the ambiguous schedule a second time.
                    con.execute('UPDATE personal_tasks SET next_run_at=?,status=? WHERE id=?', (now + interval if interval else now, 'active' if interval else 'paused', task['id']))
                    self._notify(con, run['id'], self._task(task), {'summary':'BACKEND_INTERRUPTED'}, now)
            if not self.profile()['background_enabled']: return None
            row = con.execute("SELECT * FROM personal_tasks t WHERE owner=? AND status='active' AND next_run_at<=? AND NOT EXISTS(SELECT 1 FROM personal_task_runs r WHERE r.task_id=t.id AND r.status='running') ORDER BY next_run_at,id LIMIT 1", (self.owner, now)).fetchone()
            if not row: return None
            ident = uuid.uuid4().hex
            changed = con.execute("INSERT OR IGNORE INTO personal_task_runs(id,owner,task_id,version,due_at,started_at,lease_until,status) VALUES(?,?,?,?,?,?,?,'running')", (ident,self.owner,row['id'],row['version'],row['next_run_at'],now,now+self.LEASE_SECONDS)).rowcount
            if not changed: return None
            return {'id':ident,'task':self._task(row)}

    def _execute(self, task):
        context = self._allowed(task)
        kind = task['kind']
        if kind == 'reminder': return {'summary':task['title']}
        if kind == 'mail_review':
            connectors = component(context, 'service_connectors')
            listing = connectors.execute(task['connector_id'], 'list', {'query':task['query'], 'limit':10})
            proposals = []
            messages = []
            for item in listing.get('messages', [])[:10]:
                self._ensure_current(task)
                self._allowed(task)
                message = connectors.execute(task['connector_id'], 'get', {'id':str(item['id'])})
                proposals.extend(deadline_proposals(message))
                messages.append({'id':str(item['id']), 'subject':str(message.get('subject') or next((h.get('value','') for h in message.get('headers',[]) if str(h.get('name','')).lower()=='subject'),''))[:160], 'content':str(message.get('content',''))[:20000]})
            extractor = context.get('personal_task_extract_deadlines')
            if callable(extractor) and messages and self.profile()['model_enabled']:
                self._ensure_current(task)
                self._allowed(task)
                extracted = DeadlineExtraction.model_validate(extractor(messages=messages, profile=self.callback_profile(), max_seconds=120, max_tokens=1200))
                if not self.profile()['model_enabled']: raise ValueError('TASK_CATEGORY_DISABLED')
                allowed_ids = {m['id'] for m in messages}
                if any(p.message_id not in allowed_ids for p in extracted.proposals): raise ValueError('DEADLINE_MESSAGE_INVALID')
                proposals = [p.model_dump() | {'requires_review':True} for p in extracted.proposals]
            return {'summary':f'{len(proposals)} deadline proposals', 'checked':len(listing.get('messages', [])[:10]), 'proposals':proposals[:30], 'untrusted_source':True}
        if kind == 'web_watch':
            settings = component(context, 'settings')
            provider = getattr(settings, 'web_provider', 'none')
            if getattr(settings, 'web_mode', 'off') == 'off': raise ValueError('WEB_SEARCH_DISABLED')
            from .web import search, brave_search, tavily_search
            adapter = {'duckduckgo':search, 'ddg':search, 'brave':brave_search, 'tavily':tavily_search}.get(provider)
            if adapter is None: raise ValueError('WEB_PROVIDER_NOT_CONFIGURED')
            rows = adapter(task['query'], 5)
            # No crawling or opening result URLs, only provider search summaries.
            sources = [{key:str(row.get(key, ''))[:1000] for key in ('title','url','snippet')} for row in rows[:5]]
            return {'summary':f'{len(sources)} search results', 'sources':sources, 'untrusted_source':True}
        callback = context.get('personal_task_model_review')
        if not callable(callback): raise ValueError('MODEL_REVIEW_UNAVAILABLE')
        result = callback(task=task, profile=self.callback_profile(), goals=self.goals(), max_seconds=180, max_tokens=1800)
        return ModelReviewResult.model_validate(result).model_dump()

    def _ensure_current(self, task):
        current = self.get_task(task['id'])
        if current['version'] != task['version'] or current['status'] != 'active': raise ValueError('TASK_CANCELLED')

    def _notify(self, con, ident, task, result, now):
        con.execute('INSERT OR IGNORE INTO personal_inbox VALUES(?,?,?,?,?,NULL,?)', (ident,self.owner,task['id'],task['title'],now,json.dumps(result,ensure_ascii=False)))
        con.execute('DELETE FROM personal_inbox WHERE owner=? AND id NOT IN(SELECT id FROM personal_inbox WHERE owner=? ORDER BY created_at DESC,rowid DESC LIMIT 200)', (self.owner,self.owner))

    def tick(self, now=None):
        if not self._tick_lock.acquire(blocking=False): return
        try:
            claim = self.claim_due(now)
            if not claim: return
            task, ident = claim['task'], claim['id']
            status, error, result = 'completed', '', {}
            try:
                self._ensure_current(task)
                result = self._execute(task)
                self._ensure_current(task)
                self._allowed(task)
            except Exception as exc:
                # Never persist provider exception text (may include tokens or mail).
                code = str(exc)
                error = code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}',code) else 'TASK_EXECUTION_FAILED'
                status = 'cancelled' if error == 'TASK_CANCELLED' else 'failed'
                result = {}
            finished = time.time() if now is None else now
            with self.db.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                current = con.execute('SELECT * FROM personal_tasks WHERE id=? AND owner=?',(task['id'],self.owner)).fetchone()
                if not current or current['version'] != task['version'] or current['status'] != 'active':
                    status,error,result = 'cancelled','TASK_CANCELLED',{}
                changed = con.execute("UPDATE personal_task_runs SET status=?,result_json=?,error=?,finished_at=? WHERE id=? AND owner=? AND status='running'", (status,json.dumps(result,ensure_ascii=False),error,finished,ident,self.owner)).rowcount
                if not changed: return
                if status != 'cancelled':
                    previous = con.execute("SELECT result_json,error FROM personal_task_runs WHERE task_id=? AND id<>? AND status IN('completed','failed') ORDER BY started_at DESC,rowid DESC LIMIT 1", (task['id'],ident)).fetchone()
                    if not previous or previous['result_json'] != json.dumps(result,ensure_ascii=False) or previous['error'] != error:
                        self._notify(con,ident,task,result or {'summary':error},finished)
                    interval = task.get('interval_seconds')
                    con.execute('UPDATE personal_tasks SET next_run_at=?,status=? WHERE id=? AND version=?', (finished + interval if interval else finished, 'active' if interval else 'completed' if status=='completed' else 'paused',task['id'],task['version']))
                con.execute('DELETE FROM personal_task_runs WHERE owner=? AND status<>\'running\' AND id NOT IN(SELECT id FROM personal_task_runs WHERE owner=? ORDER BY started_at DESC,rowid DESC LIMIT 500)', (self.owner,self.owner))
        finally:
            self._tick_lock.release()

    def preview_calendar(self, run_id, proposal_index, conversation_id):
        with self.db.connect() as con:
            row = con.execute('SELECT * FROM personal_task_runs WHERE id=? AND owner=?', (run_id,self.owner)).fetchone()
        if not row or row['status'] != 'completed': raise ValueError('RUN_NOT_FOUND')
        run = dict(row) | {'result':json.loads(row['result_json'])}
        task = self.get_task(run['task_id'])
        self._allowed(task)
        proposals = run['result'].get('proposals',[])
        if not 0 <= proposal_index < len(proposals): raise ValueError('PROPOSAL_NOT_FOUND')
        if not self.db.conversation(conversation_id): raise ValueError('CONVERSATION_NOT_FOUND')
        connectors = component(self.host(), 'service_connectors')
        if connectors is None or not any(c['id']=='calendar' and c['enabled'] for c in connectors.categories()):
            raise ValueError('CALENDAR_PERMISSION_DISABLED')
        service = component(self.host(), 'assistant_service')
        if service is None: raise ValueError('CALENDAR_UNAVAILABLE')
        proposal = proposals[proposal_index]
        # Stable preview ID prevents double clicks from creating duplicate drafts.
        ident = uuid.uuid5(uuid.NAMESPACE_URL, f'personal:{self.owner}:{run_id}:{proposal_index}:{conversation_id}').hex
        day = date.fromisoformat(proposal['date'])
        return service.prepare_calendar(conversation_id=conversation_id, ident=ident, payload={
            'summary':proposal['title'], 'start':day.isoformat(), 'end':(day+timedelta(days=1)).isoformat(),
            'all_day':True, 'timeZone':self.profile()['time_zone'], 'description':'Unverified deadline extracted from mail. Please check the original message.'})

    def start(self, interval=15):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try: self.tick()
                except Exception: pass  # transient DB/startup failure; no sensitive logs
                self._stop.wait(max(1, interval))
        self._thread = threading.Thread(target=loop,name='olcr-personal-tasks',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=1)
