"""PC-owned runs: observers read a durable cursor; disconnect never owns execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from .generation_control import Generation, GenerationCancelled


class MobileRuns:
    def __init__(self, path, source):
        self.path, self.source = path, source
        self.workers = {}
        self.generations = {}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mobile_runs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(conversation_id, request_id));
                CREATE TABLE IF NOT EXISTS mobile_run_events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq));
            ''')
            db.execute("UPDATE mobile_runs SET state='interrupted' WHERE state='running'")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
                db.executescript('''CREATE TRIGGER IF NOT EXISTS mobile_conversation_delete
                    AFTER DELETE ON conversations BEGIN
                      DELETE FROM mobile_run_events WHERE run_id IN
                        (SELECT id FROM mobile_runs WHERE conversation_id=OLD.id);
                      DELETE FROM mobile_runs WHERE conversation_id=OLD.id;
                    END;''')

    def active(self, conversation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM mobile_runs WHERE conversation_id=? AND state='running' ORDER BY created_at DESC LIMIT 1",
                             (conversation_id,)).fetchone()
        return dict(row) if row else None

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def get(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT * FROM mobile_runs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise KeyError('RUN_NOT_FOUND')
        return dict(row)

    def start(self, body):
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM mobile_runs WHERE conversation_id=? AND request_id=?',
                             (body['conversation_id'], body['request_id'])).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return dict(old)
            if db.execute("SELECT 1 FROM mobile_runs WHERE conversation_id=? AND state='running'",
                          (body['conversation_id'],)).fetchone():
                raise ValueError('CONVERSATION_BUSY_USE_NEXT_TURN_QUEUE')
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO mobile_runs VALUES(?,?,?,?,?,?)',
                (identifier, body['conversation_id'], body['request_id'], fingerprint, 'running', time.time()))
        generation = self.generations[identifier] = Generation()
        self.workers[identifier] = asyncio.create_task(self._consume(identifier, body, generation))
        return self.get(identifier)

    def append(self, identifier, event):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM mobile_runs WHERE id=?', (identifier,)).fetchone():
                raise KeyError('RUN_DELETED')
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM mobile_run_events WHERE run_id=?', (identifier,)).fetchone()[0]
            db.execute('INSERT INTO mobile_run_events VALUES(?,?,?)', (identifier, seq, json.dumps(event, ensure_ascii=False)))

    async def _consume(self, identifier, body, generation):
        state = 'failed'
        async def watch_deleted():
            while True:
                await asyncio.sleep(.25)
                try:
                    self.get(identifier)
                except KeyError:
                    generation.cancel()
                    return
        watcher = asyncio.create_task(watch_deleted())
        try:
            terminal = False
            async for event in self.source(body, generation):
                self.append(identifier, event)
                kind = event.get('type')
                if kind in {'done', 'cancelled', 'error'}:
                    terminal = True
                    state = {'done': 'completed', 'cancelled': 'cancelled', 'error': 'failed'}[kind]
            if not terminal:
                self.append(identifier, {'type': 'error', 'code': 'STREAM_ENDED_WITHOUT_TERMINAL_EVENT'})
        except KeyError:
            generation.cancel()
            state = 'cancelled'
        except GenerationCancelled:
            state = 'cancelled'
            try:
                self.append(identifier, {'type': 'cancelled'})
            except KeyError:
                pass
        except asyncio.CancelledError:
            generation.cancel()
            state = 'interrupted'
            raise
        except Exception:
            self.append(identifier, {'type': 'error', 'code': 'OLCR_RUN_FAILED'})
        finally:
            watcher.cancel()
            with self.connect() as db:
                db.execute('UPDATE mobile_runs SET state=? WHERE id=?', (state, identifier))
            self.generations.pop(identifier, None)
            self.workers.pop(identifier, None)

    def events(self, identifier, after=0):
        self.get(identifier)
        with self.connect() as db:
            return [{'seq': row['seq'], 'event': json.loads(row['data'])} for row in db.execute(
                'SELECT * FROM mobile_run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 256', (identifier, after))]

    def cancel(self, identifier):
        run = self.get(identifier)
        generation = self.generations.get(identifier)
        if generation:
            generation.cancel()
        return {'state': run['state'], 'cancellation_requested': bool(generation)}

    async def close(self):
        workers = list(self.workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def follow_coding_task(identifier, get_task, pause_task, generation, *, poll_seconds=.5):
    """A chat handoff is not coding completion; observe the actual PC task."""
    previous = None
    active = {'QUEUED', 'RUNNING', 'PLANNING', 'FINAL_REPORTING', 'QWEN_REPLANNING'}
    while True:
        if generation.is_set():
            await asyncio.to_thread(pause_task, identifier)
            yield {'type': 'cancelled'}
            return
        task = get_task(identifier)
        if not task:
            yield {'type': 'error', 'code': 'CODING_TASK_UNAVAILABLE'}
            return
        visible = {key: task.get(key) for key in ('id', 'status', 'activity', 'current_phase_id',
            'pending_user_confirmation', 'pending_authorization', 'subtask_progress')}
        if visible != previous:
            yield {'type': 'task_update', 'task': visible}
            previous = visible
        if task['status'] not in active:
            if task['status'] in {'FAILED', 'BLOCKED'}:
                yield {'type': 'error', 'code': 'CODING_TASK_' + task['status']}
            else:
                yield {'type': 'done', 'task': visible, 'waiting_for_user': task['status'] in {'WAITING_FOR_USER', 'RESUMABLE'}}
            return
        await asyncio.sleep(poll_seconds)
