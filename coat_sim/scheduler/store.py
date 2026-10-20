"""The existing shared SQLite queue is the single source of job state."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import re
from uuid import uuid4
from coat_sim.core.compute_tasks import COMPUTE_POLICY_BLOCKERS, ComputeTaskStore, _now
from .jobs import JobSpec, canonical

FINAL = {'succeeded', 'failed', 'cancelled'}

class SchedulerStore:
    def __init__(self, root):
        self.queue = ComputeTaskStore(root)
        with self.queue.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS scheduler_specs(
                job_id TEXT PRIMARY KEY, spec TEXT NOT NULL, fingerprint TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
                worker_start TEXT, lease_owner TEXT);
                CREATE TABLE IF NOT EXISTS scheduler_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scheduler_artifacts(
                artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, metadata TEXT NOT NULL);''')

    def submit(self, spec, job_id=None, metadata=None):
        parsed = JobSpec.parse_frozen(spec)
        job_id = job_id or uuid4().hex
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
            raise ValueError('invalid_job_id')
        metadata = metadata or {}
        estimate_id = metadata.get("estimate_id")
        authority_scope = self.queue.submission_authority(estimate_id) if estimate_id else nullcontext(None)
        try:
            with authority_scope as authority, self.queue.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT fingerprint FROM scheduler_specs WHERE job_id=?', (job_id,)).fetchone()
                if prior:
                    if prior['fingerprint'] != parsed.fingerprint:
                        raise ValueError('request_id_conflict: same ID has different frozen inputs')
                    return self._read(db, job_id)
                if estimate_id:
                    receipt_row = db.execute("SELECT payload,state,job_id FROM estimates WHERE estimate_id=?",
                                             (estimate_id,)).fetchone()
                    if receipt_row is None or receipt_row["state"] != "approved":
                        raise ValueError("compute_confirmation_required: release is not currently approved")
                    receipt = {**json.loads(receipt_row["payload"]), "state": receipt_row["state"],
                               "job_id": receipt_row["job_id"]}
                    self.queue._validate_pending(receipt, authority_connection=authority)
                    if metadata.get("origin", {}) != receipt.get("origin", {}):
                        raise ValueError("compute_origin_conflict: queue metadata belongs to another origin")
                value = {key: metadata[key] for key in ('summary', 'estimated_s', 'origin', 'estimate_id',
                         'memory_estimate_label', 'memory_warning', 'input_summary',
                         'execution_project_root') if key in metadata}
                value.update(job_id=job_id, kind=parsed.executor, state='queued', created_at=_now(),
                             started_at=None, finished_at=None, result=None, error=None,
                             cancel_requested=False, scheduler_managed=True,
                             input_digest=parsed.fingerprint, protocol_version=1,
                             input_snapshot_file=str(self.queue.root / 'artifacts/runtime/scheduler/jobs' / job_id / 'spec.json'),
                             progress={'fraction': 0, 'stage': 'queued'})
                db.execute('INSERT INTO scheduler_specs(job_id,spec,fingerprint) VALUES(?,?,?)',
                           (job_id, parsed.canonical_json, parsed.fingerprint))
                cursor = db.execute('INSERT INTO queue_jobs VALUES(?,?,?,?)', (job_id, canonical(value), 'queued', value['created_at']))
                value['queue_order'] = cursor.lastrowid
                db.execute('UPDATE queue_jobs SET payload=? WHERE job_id=?', (canonical(value), job_id))
                if metadata.get('estimate_id'):
                    db.execute("UPDATE estimates SET state='submitted',job_id=? WHERE estimate_id=?",
                               (job_id, metadata['estimate_id']))
                self.queue._event(db, value, 'enqueued')
                self._event(db, value, 'enqueued')
            return value
        except ValueError as exc:
            if estimate_id and str(exc).startswith(COMPUTE_POLICY_BLOCKERS):
                self.queue.block_release(estimate_id, str(exc))
            raise

    def _read(self, db, job_id):
        row = db.execute('SELECT payload,state FROM queue_jobs WHERE job_id=?', (job_id,)).fetchone()
        if not row: raise ValueError('job_not_found')
        return {**json.loads(row['payload']), 'state': row['state']}

    def status(self, job_id):
        with self.queue.connection() as db: return self._read(db, job_id)

    def _event(self, db, value, kind):
        cursor = db.execute('INSERT INTO scheduler_events(job_id,type,payload,created_at) VALUES(?,?,?,?)',
                            (value['job_id'], kind, canonical(value), _now()))
        self.last_sequence = cursor.lastrowid

    def transition(self, job_id, **changes):
        with self.queue.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            value = self._read(db, job_id)
            if value['state'] in FINAL: return value
            value.update(changes)
            for item in (value.get('result') or {}).get('result_artifacts', []):
                db.execute('INSERT OR IGNORE INTO scheduler_artifacts VALUES(?,?,?)',
                           (item['artifact_id'], job_id, canonical(item)))
            if value['state'] in FINAL:
                value['finished_at'] = _now()
                self.queue._event(db, value, 'completed')
                db.execute('UPDATE execution_lease SET owner=NULL,expires_at=0 WHERE owner=?', (job_id,))
            db.execute('UPDATE queue_jobs SET payload=?,state=? WHERE job_id=?', (canonical(value), value['state'], job_id))
            self._event(db, value, changes.get('state', 'progress'))
            return value

    def cancel(self, job_id):
        with self.queue.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            value = self._read(db, job_id)
            if value['state'] in FINAL or value.get('cancel_requested'): return value
            value['cancel_requested'] = True
            db.execute('UPDATE scheduler_specs SET cancel_requested=1 WHERE job_id=?', (job_id,))
            if value['state'] == 'queued':
                value.update(state='cancelled', finished_at=_now(), error_code='cancelled')
                self.queue._event(db, value, 'completed')
                db.execute('UPDATE execution_lease SET owner=NULL,expires_at=0 '
                           'WHERE singleton=1 AND owner=?', (job_id,))
            db.execute('UPDATE queue_jobs SET payload=?,state=? WHERE job_id=?', (canonical(value), value['state'], job_id))
            self._event(db, value, 'cancel_requested')
        return value

    def claim(self, owner):
        with self.queue.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT q.job_id,s.spec FROM queue_jobs q JOIN scheduler_specs s USING(job_id) "
                             "WHERE q.state='queued' ORDER BY q.rowid LIMIT 1").fetchone()
            if not row: return None
            # Never compete with an older live executor in an upgraded workspace.
            lease = db.execute('SELECT owner FROM execution_lease WHERE singleton=1').fetchone()['owner']
            if lease:
                state = db.execute('SELECT state FROM queue_jobs WHERE job_id=?', (lease,)).fetchone()
                if state and state['state'] in ('queued', 'running'): return None
            value = self._read(db, row['job_id'])
            value.update(state='running', started_at=_now(), scheduler_owner=owner)
            db.execute("UPDATE queue_jobs SET payload=?,state='running' WHERE job_id=?", (canonical(value), row['job_id']))
            db.execute('UPDATE scheduler_specs SET lease_owner=? WHERE job_id=?', (owner, row['job_id']))
            db.execute('UPDATE execution_lease SET owner=?,expires_at=? WHERE singleton=1', (row['job_id'], 0))
            self._event(db, value, 'running')
            return value, json.loads(row['spec'])

    def set_worker(self, job_id, pid, start):
        with self.queue.connection() as db:
            db.execute('UPDATE scheduler_specs SET worker_pid=?,worker_start=? WHERE job_id=?', (pid, start, job_id))

    def running(self):
        with self.queue.connection() as db:
            return [dict(row) for row in db.execute("SELECT s.* FROM scheduler_specs s JOIN queue_jobs q USING(job_id) WHERE q.state='running'")]
