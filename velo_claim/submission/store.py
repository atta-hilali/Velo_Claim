from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from threading import RLock


class MemorySubmissionStore:
    """Single-process development store; not suitable for external delivery."""
    durable = False

    def __init__(self):
        self.records = {}
        self._lock = RLock()

    @contextmanager
    def lock(self, key):
        with self._lock:
            yield

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self.records.get(key))

    def put(self, key, document):
        with self._lock:
            self.records[key] = copy.deepcopy(document)

    def list(self, record_type):
        with self._lock:
            return [copy.deepcopy(v) for v in self.records.values() if v.get('record_type') == record_type]


class PostgresSubmissionStore:
    """Durable journal with cross-process session advisory locks.

    Apply migration 005 first. Locks serialize a target's approval/delivery and
    a response file's processing across workers. A crash leaves SUBMITTING
    recorded; it is reconciled, never automatically re-sent.
    """
    durable = True

    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def lock(self, key):
        lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
        # Lock waiters must not exhaust the pool needed by the lock holder.
        with self.repository._psycopg.connect(self.repository.dsn, autocommit=True) as conn:
            conn.execute('SELECT pg_advisory_lock(%s)', (lock_id,))
            try:
                yield
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))

    def get(self, key):
        with self.repository._connect() as conn:
            row = conn.execute('SELECT document FROM payer_submission_journal WHERE key = %s', (key,)).fetchone()
        return row['document'] if row else None

    def put(self, key, document):
        with self.repository._connect() as conn:
            conn.execute('''INSERT INTO payer_submission_journal (key, document)
                VALUES (%s, %s::jsonb) ON CONFLICT (key) DO UPDATE
                SET document = EXCLUDED.document, updated_at = now()''',
                (key, json.dumps(document, default=str)))

    def list(self, record_type):
        with self.repository._connect() as conn:
            rows = conn.execute("SELECT document FROM payer_submission_journal WHERE document->>'record_type' = %s", (record_type,)).fetchall()
        return [r['document'] for r in rows]
