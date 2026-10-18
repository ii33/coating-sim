"""Persistent conversations, runtime history and shared token budgets; no Qt dependency."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

STATE_PATH = Path(__file__).resolve().parents[2] / "artifacts/assistant/state.sqlite3"
class TokenBudgetExceeded(ValueError):
    pass

class AssistantStore:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session TEXT,role TEXT,content TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,session TEXT,model TEXT,created REAL,day TEXT,input_tokens INTEGER,output_tokens INTEGER,estimated INTEGER);
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,created REAL,day TEXT,tokens INTEGER);
                CREATE TABLE IF NOT EXISTS runtime_history(session TEXT PRIMARY KEY,history TEXT);
                CREATE TABLE IF NOT EXISTS task_events(id INTEGER PRIMARY KEY,session TEXT NOT NULL,request_id TEXT NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS task_events_session ON task_events(session,id);
                CREATE TABLE IF NOT EXISTS context_state(session TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS usage_created ON usage(created);
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session,id);
                CREATE TABLE IF NOT EXISTS background_notifications(
                    job_id TEXT PRIMARY KEY, session TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS background_notifications_session
                    ON background_notifications(session,state,created);
                CREATE TABLE IF NOT EXISTS assistant_ui_session_modes(
                    session TEXT PRIMARY KEY, mode TEXT NOT NULL,
                    generation INTEGER NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_ui_requests(
                    session TEXT NOT NULL, request_id TEXT NOT NULL,
                    mode TEXT NOT NULL, generation INTEGER NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(session,request_id));
            """)
            existing = {row[1] for row in db.execute('PRAGMA table_info(reservations)')}
            for name,definition in (('owner','INTEGER NOT NULL DEFAULT 0'),('session',"TEXT NOT NULL DEFAULT ''"),('model',"TEXT NOT NULL DEFAULT ''")):
                if name not in existing:
                    try:
                        db.execute(f'ALTER TABLE reservations ADD COLUMN {name} {definition}')
                    except sqlite3.OperationalError:
                        if name not in {row[1] for row in db.execute('PRAGMA table_info(reservations)')}:
                            raise
            for table, columns in {
                "messages": {"request_id": "TEXT NOT NULL DEFAULT ''", "metadata": "TEXT NOT NULL DEFAULT '{}'"},
                "usage": {"request_id": "TEXT NOT NULL DEFAULT ''",
                          "cached_input_tokens": "INTEGER DEFAULT NULL",
                          "cache_creation_input_tokens": "INTEGER DEFAULT NULL",
                          "cache_reporting_supported": "INTEGER DEFAULT NULL"},
                "reservations": {"request_id": "TEXT NOT NULL DEFAULT ''", "heartbeat": "REAL NOT NULL DEFAULT 0"},
                "runtime_history": {"request_id": "TEXT NOT NULL DEFAULT ''",
                                    "state": "TEXT NOT NULL DEFAULT 'complete'",
                                    "updated": "REAL NOT NULL DEFAULT 0"},
            }.items():
                existing_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing_columns:
                        try:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                        except sqlite3.OperationalError:
                            if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                                raise
    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    def new_session(self, title="新会话"):
        key = uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?)", (key,title[:80],time.time()))
        return key
    def set_ui_session_mode(self, session, mode):
        """Desktop-owned authority, never populated from worker progress events.

        Every actual mode transition revokes unsubmitted receipts from the old
        generation, including when the user later returns to the same mode.
        """
        if mode not in ("chat", "operate", "research"):
            raise ValueError("invalid assistant UI mode")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (session,)).fetchone():
                raise ValueError("会话不存在")
            db.execute("""INSERT INTO assistant_ui_session_modes VALUES(?,?,1,?)
                ON CONFLICT(session) DO UPDATE SET mode=excluded.mode,
                generation=assistant_ui_session_modes.generation +
                    CASE WHEN assistant_ui_session_modes.mode != excluded.mode THEN 1 ELSE 0 END,
                updated=excluded.updated""", (session, mode, time.time()))
            return dict(db.execute("SELECT * FROM assistant_ui_session_modes WHERE session=?",
                                   (session,)).fetchone())
    def bind_ui_request(self, session, request_id, mode):
        """Bind the actual UI run mode once; callers cannot relabel old requests."""
        if mode not in ("chat", "operate", "research") or not request_id:
            raise ValueError("invalid assistant UI request")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM assistant_ui_session_modes WHERE session=?",
                                 (session,)).fetchone()
            if current is None or not db.execute("SELECT 1 FROM conversations WHERE id=?", (session,)).fetchone():
                raise ValueError("assistant_ui_mode_missing: 当前会话缺少可信 UI 模式")
            prior = db.execute("SELECT * FROM assistant_ui_requests WHERE session=? AND request_id=?",
                               (session, request_id)).fetchone()
            if prior is not None:
                if prior["mode"] != mode or prior["generation"] != current["generation"]:
                    raise ValueError("assistant_ui_request_conflict: 请求已绑定另一模式代际")
                return dict(prior)
            db.execute("INSERT INTO assistant_ui_requests VALUES(?,?,?,?,?)",
                       (session, request_id, mode, current["generation"], time.time()))
            return dict(db.execute("SELECT * FROM assistant_ui_requests WHERE session=? AND request_id=?",
                                   (session, request_id)).fetchone())
    def sessions(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM conversations ORDER BY updated DESC")]
    def messages(self, session):
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT id,role,content,created,request_id,metadata FROM messages WHERE session=? ORDER BY id", (session,))]
        for row in rows:
            try:
                row["metadata"] = json.loads(row["metadata"])
            except ValueError:
                row["metadata"] = {}
        return rows
    def add_message(self, session, role, content, *, request_id="", metadata=None):
        if role not in ("user", "assistant", "event"):
            raise ValueError("Invalid display role")
        with self._db() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (session,)).fetchone():
                raise ValueError("会话不存在")
            cursor = db.execute("INSERT INTO messages(session,role,content,created,request_id,metadata) VALUES(?,?,?,?,?,?)",
                (session,role,content,time.time(),request_id,json.dumps(metadata or {},ensure_ascii=False)))
            if role == "user":
                db.execute("UPDATE conversations SET title=? WHERE id=? AND title='新会话'", (content.strip().replace(chr(10),' ')[:40],session))
            db.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(),session))
            return cursor.lastrowid
    def update_message_metadata(self, session, request, metadata):
        with self._db() as db:
            row = db.execute("SELECT id,metadata FROM messages WHERE session=? AND request_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (session,request)).fetchone()
            if row:
                current = json.loads(row['metadata']); current.update(metadata)
                db.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(current,ensure_ascii=False),row['id']))
    def delete_session(self, session):
        """Deleting conversation content never resets spent token quotas."""
        with self._db() as db:
            for table in ("messages", "runtime_history", "task_events", "background_notifications", "context_state",
                          "assistant_ui_requests", "assistant_ui_session_modes"):
                db.execute(f"DELETE FROM {table} WHERE session=?", (session,))
            db.execute("DELETE FROM conversations WHERE id=?", (session,))
    @staticmethod
    def _usage_totals(db, where="", parameters=()):
        row = db.execute("""SELECT
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(estimated),0) AS estimated_requests,
            COUNT(*) AS requests,
            SUM(cached_input_tokens) AS cached_input_tokens,
            SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
            COALESCE(SUM(CASE WHEN cached_input_tokens IS NOT NULL
                THEN input_tokens ELSE 0 END),0) AS cache_reported_input_tokens,
            COUNT(cached_input_tokens) AS cache_reported_requests,
            COUNT(cache_creation_input_tokens) AS cache_creation_reported_requests
            FROM usage""" + where, parameters).fetchone()
        result = dict(row)
        result["cache_reporting_supported"] = (
            True if result["cache_reported_requests"] or result["cache_creation_reported_requests"] else None)
        return result
    def round_usage(self, request):
        with self._db() as db:
            return self._usage_totals(db, " WHERE request_id=?", (request,))
    def session_usage(self, session):
        """Historical/estimated requests remain in total usage, never in cache denominators."""
        with self._db() as db:
            return self._usage_totals(db, " WHERE session=?", (session,))
    def clear_session(self, session):
        replacement = uuid4().hex
        with self._db() as db:
            db.execute("DELETE FROM messages WHERE session=?", (session,))
            db.execute("DELETE FROM runtime_history WHERE session=?", (session,))
            db.execute("DELETE FROM context_state WHERE session=?", (session,))
            db.execute("DELETE FROM task_events WHERE session=?", (session,))
            db.execute("DELETE FROM background_notifications WHERE session=?", (session,))
            db.execute("DELETE FROM assistant_ui_requests WHERE session=?", (session,))
            db.execute("DELETE FROM assistant_ui_session_modes WHERE session=?", (session,))
            db.execute("UPDATE conversations SET id=?,title='新会话',updated=? WHERE id=?", (replacement,time.time(),session))
        return replacement
    def request_was_stopped(self, session, request_id):
        """Explicit user stop suppresses late automatic completion replies."""
        if not request_id:
            return False
        with self._db() as db:
            rows = db.execute("SELECT event,payload FROM task_events WHERE session=? AND request_id=? ORDER BY id DESC LIMIT 80",
                              (session,request_id)).fetchall()
            messages = db.execute("SELECT metadata FROM messages WHERE session=? AND request_id=? AND role='assistant'",
                                  (session,request_id)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row['payload'])
            except (TypeError,ValueError):
                continue
            if row['event'] == 'cancelled' or (isinstance(payload,dict)
                    and payload.get('state') in ('cancelling','cancelled','interrupted')):
                return True
        for row in messages:
            try:
                metadata = json.loads(row['metadata'])
                if isinstance(metadata,dict) and metadata.get('status') in ('cancelled','interrupted'):
                    return True
            except (TypeError,ValueError):
                pass
        return False
    def receive_background_notification(self, session, request_id, job_id, payload):
        """Persist one terminal notification per job; never resurrect a session."""
        if not session or not job_id or not isinstance(payload,dict):
            return False
        stopped = self.request_was_stopped(session, request_id)
        encoded = json.dumps(payload,ensure_ascii=False)
        if len(encoded) > 16384:
            raise ValueError('后台完成通知超出摘要范围')
        now = time.time()
        with self._db() as db:
            if not db.execute('SELECT 1 FROM conversations WHERE id=?',(session,)).fetchone():
                return False
            cursor = db.execute("INSERT OR IGNORE INTO background_notifications(job_id,session,request_id,payload,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                (job_id,session,request_id,encoded,'suppressed' if stopped else 'pending',now,now))
            return bool(cursor.rowcount) and not stopped
    def pending_background_notifications(self, session):
        with self._db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM background_notifications WHERE session=? AND state='pending' ORDER BY created",
                                                    (session,))]
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows
    def settle_background_notification(self, job_id, state):
        if state not in ('started','responded','suppressed','failed'):
            raise ValueError('Invalid background notification state')
        with self._db() as db:
            db.execute('UPDATE background_notifications SET state=?,updated=? WHERE job_id=?',
                       (state,time.time(),job_id))
    def background_workflow(self, session, request_id, *, job_id='', estimate_id=''):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM task_events WHERE session=? AND request_id=? AND event='background_registered' ORDER BY id DESC",
                              (session,request_id)).fetchall()
        for row in rows:
            try:
                value = json.loads(row['payload'])
            except (TypeError,ValueError):
                continue
            if (isinstance(value,dict) and ((job_id and value.get('job_id') == job_id)
                    or (estimate_id and value.get('estimate_id') == estimate_id))):
                return str(value.get('workflow_run_id') or '')
        return ''
    def load_history(self, session):
        with self._db() as db:
            r = db.execute("SELECT history FROM runtime_history WHERE session=?", (session,)).fetchone()
            return r[0].encode('utf-8') if r else None
    def history_state(self, session):
        """Lifecycle metadata distinguishes a saved answer from a crash frontier."""
        with self._db() as db:
            row = db.execute("SELECT request_id,state,updated FROM runtime_history WHERE session=?", (session,)).fetchone()
            return dict(row) if row else {}
    def request_memory_scope(self, session: str, request: str) -> str:
        """Exact host-written scope marker; never infer it from message content."""
        with self._db() as db:
            row = db.execute("SELECT payload FROM task_events WHERE session=? AND request_id=? "
                "AND event='project_memory_scope' ORDER BY id DESC LIMIT 1", (session, request)).fetchone()
        if row is None:
            return 'global'  # Legacy history is never automatically adopted by a project.
        try:
            value = json.loads(row[0])['scope']
            return value if isinstance(value, str) and value else 'unavailable'
        except (ValueError, KeyError, TypeError):
            return 'unavailable'
    def save_history(self, session, history, *, request_id="", state="complete"):
        if state not in ("active", "complete", "failed", "cancelled", "interrupted", "limited"):
            raise ValueError("Invalid runtime history state")
        text = history.decode('utf-8') if isinstance(history,bytes) else history
        json.loads(text)
        with self._db() as db:
            db.execute("""INSERT INTO runtime_history(session,history,request_id,state,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(session) DO UPDATE SET history=excluded.history,request_id=excluded.request_id,
                state=excluded.state,updated=excluded.updated""", (session,text,request_id,state,time.time()))
    @staticmethod
    def _limits(config):
        return (max(0,int(config.get('assistant_daily_token_limit',0))),max(0,int(config.get('assistant_period_token_limit',0))),max(1,int(config.get('assistant_period_minutes',60))))
    def _remaining(self, db, config):
        daily,period,minutes = self._limits(config)
        now,day = time.time(),datetime.now().date().isoformat()
        expired = db.execute("SELECT * FROM reservations WHERE MAX(created,heartbeat)<?", (now-900,)).fetchall()
        for row in expired:
            db.execute('INSERT OR IGNORE INTO usage(id,session,model,created,day,input_tokens,output_tokens,estimated,request_id) VALUES(?,?,?,?,?,?,?,?,?)',
                       (row['id'],row['session'],row['model'],row['created'],row['day'],row['tokens'],0,1,row['request_id']))
        db.execute("DELETE FROM reservations WHERE MAX(created,heartbeat)<?", (now-900,))
        available = []
        for limit,column,value in ((daily,'day',day),(period,'created',now-minutes*60)):
            if limit:
                op = '=' if column == 'day' else '>='
                used = db.execute(f"SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE {column}{op}?", (value,)).fetchone()[0]
                pending = db.execute(f"SELECT COALESCE(SUM(tokens),0) FROM reservations WHERE {column}{op}?", (value,)).fetchone()[0]
                available.append(max(0,limit-used-pending))
        return min(available) if available else None
    def remaining_tokens(self, config):
        with self._db() as db:
            return self._remaining(db,config)
    def reserve_request(self, config, estimated_input, desired_output=4096, *, minimum_output=1):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            remaining = self._remaining(db,config)
            output = desired_output if remaining is None else min(desired_output,remaining-estimated_input)
            if output < max(1, int(minimum_output)):
                raise TokenBudgetExceeded(f'剩余 Token 额度不足：当前上下文预估 {estimated_input}，此推理步骤至少需要 {minimum_output} 生成预算；请降低推理强度、减少上下文或等待额度恢复。')
            key = uuid4().hex
            db.execute('INSERT INTO reservations(id,created,day,tokens,owner,session,model,request_id) VALUES(?,?,?,?,?,?,?,?)', (key,time.time(),datetime.now().date().isoformat(),estimated_input+output,int(config.get('reservation_owner',0)),str(config.get('reservation_session','')),str(config.get('reservation_model','')),str(config.get('reservation_request',''))))
            return key,output
    def renew_request(self, reservation):
        """Keep a live request reserved without moving its accounting time/day."""
        with self._db() as db:
            updated = db.execute("UPDATE reservations SET heartbeat=? WHERE id=?",
                                 (time.time(), reservation))
            return updated.rowcount == 1
    def settle_request(self, reservation, session, model, input_tokens, output_tokens, *, estimated=False,
                       cached_input_tokens=None, cache_reporting_supported=None,
                       cache_creation_input_tokens=None):
        def count(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9_223_372_036_854_775_807 else None
        cached = count(cached_input_tokens) if not estimated else None
        created = count(cache_creation_input_tokens) if not estimated else None
        # No fields means unreported. An explicit zero is a supported measurement.
        reported = 1 if cached is not None or created is not None else None
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT created,day,request_id FROM reservations WHERE id=?', (reservation,)).fetchone()
            if row:
                db.execute('INSERT OR IGNORE INTO usage(id,session,model,created,day,input_tokens,output_tokens,estimated,request_id,cached_input_tokens,cache_creation_input_tokens,cache_reporting_supported) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (reservation,session,model,row[0],row[1],max(0,int(input_tokens)),max(0,int(output_tokens)),int(estimated),row[2],cached,created,reported))
                db.execute('DELETE FROM reservations WHERE id=?', (reservation,))
    def abandon_request(self, reservation, *, charge_estimate=False, session='', model=''):
        with self._db() as db:
            row = db.execute('SELECT tokens FROM reservations WHERE id=?', (reservation,)).fetchone()
        if charge_estimate and row:
            self.settle_request(reservation,session,model,row[0],0,estimated=True)
        else:
            with self._db() as db:
                db.execute('DELETE FROM reservations WHERE id=?', (reservation,))
    def settle_owner(self, owner):
        """Charge unknown in-flight usage after an owned worker is killed."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT * FROM reservations WHERE owner=?', (owner,)).fetchall():
                db.execute('INSERT OR IGNORE INTO usage(id,session,model,created,day,input_tokens,output_tokens,estimated,request_id) VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['id'],row['session'],row['model'],row['created'],row['day'],row['tokens'],0,1,row['request_id']))
            db.execute('DELETE FROM reservations WHERE owner=?', (owner,))
    def usage_summary(self, config):
        _,_,minutes = self._limits(config)
        with self._db() as db:
            remaining = self._remaining(db,config)
            today = self._usage_totals(db, " WHERE day=?", (datetime.now().date().isoformat(),))
            period = db.execute('SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE created>=?', (time.time()-minutes*60,)).fetchone()[0]
            models = [dict(r) for r in db.execute('SELECT model,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(estimated) AS estimated_requests FROM usage GROUP BY model')]
        return {'today_input':today['input_tokens'],'today_output':today['output_tokens'],
                'period_tokens':period,'period_minutes':minutes,'remaining':remaining,'models':models,
                **{key: value for key, value in today.items() if key.startswith('cache')},
                'requests':today['requests']}

    def record_event(self, session, request, event, payload):
        """Persist sanitized task metadata; streamed text uses display messages."""
        self.record_events([(session,request,event,payload,time.time())])
    def record_events(self, events):
        """Write a worker batch in one transaction, retaining every control event."""
        if not events:
            return
        with self._db() as db:
            rows = [(session,request,event,json.dumps(payload,ensure_ascii=False,default=str),created)
                    for session,request,event,payload,created in events]
            db.executemany("INSERT INTO task_events(session,request_id,event,payload,created) VALUES(?,?,?,?,?)", rows)
            for session,request,event,payload,created in events:
                if event == "context" and payload.get("source", "main") == "main":
                    db.execute("""INSERT INTO context_state(session,payload,updated) VALUES(?,?,?)
                        ON CONFLICT(session) DO UPDATE SET payload=excluded.payload,updated=excluded.updated""",
                        (session,json.dumps(payload,ensure_ascii=False,default=str),created))
    def context_state(self, session):
        """Last main context reading survives compaction and conversation switches."""
        with self._db() as db:
            row = db.execute("SELECT payload FROM context_state WHERE session=?", (session,)).fetchone()
            payload = json.loads(row[0]) if row else {}
            return {key: value for key, value in payload.items() if key not in ("event", "request_id")}
    def task_events(self, session, request_id=None, limit=100):
        bounded = max(1,min(1000,int(limit)))
        query = "SELECT request_id,event,payload,created AS created_at FROM task_events WHERE session=?"
        parameters = [session]
        if request_id is not None:
            query += " AND request_id=?"
            parameters.append(request_id)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(bounded)
        with self._db() as db:
            rows = [dict(row) for row in db.execute(query,parameters)]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return list(reversed(rows))
