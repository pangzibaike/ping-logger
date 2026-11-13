"""Additive, transactional SQLite migrations and bounded maintenance."""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import sqlite3
import time

LOG = logging.getLogger('ping_logger.database')
COLUMNS = 'ts,ms,status,reason,sent,received,min_ms,max_ms,rtts,interval'


@contextmanager
def connect(db):
    conn = sqlite3.connect(str(db), timeout=5)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA synchronous=FULL')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(db):
    with connect(db) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            raise ValueError('Database schema is newer than this program')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='samples'").fetchone()
        if exists and version < 2:
            backup = Path(str(db) + '.pre-v2-' + str(time.time_ns()) + '.sqlite')
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
            LOG.info('database_migration_backup path=%s', backup)
        conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY AUTOINCREMENT,address TEXT NOT NULL UNIQUE,name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)')
        conn.execute('CREATE TABLE IF NOT EXISTS samples (target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,ts INTEGER NOT NULL,ms REAL,status TEXT NOT NULL,PRIMARY KEY(target_id,ts)) WITHOUT ROWID')
        target_columns = {r[1] for r in conn.execute('PRAGMA table_info(targets)')}
        if 'active' not in target_columns:
            conn.execute('ALTER TABLE targets ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(samples)')}
        additions = {'reason': "TEXT NOT NULL DEFAULT 'legacy'", 'sent': 'INTEGER NOT NULL DEFAULT 0',
                     'received': 'INTEGER NOT NULL DEFAULT 0', 'min_ms': 'REAL', 'max_ms': 'REAL',
                     'rtts': "TEXT NOT NULL DEFAULT '[]'", 'interval': 'INTEGER NOT NULL DEFAULT 60'}
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE samples ADD COLUMN {name} {declaration}')
        if version < 2:
            conn.execute("UPDATE samples SET sent=CASE WHEN status='error' THEN 0 ELSE 1 END,received=CASE WHEN status='ok' AND ms IS NOT NULL THEN 1 ELSE 0 END,min_ms=ms,max_ms=ms,rtts=CASE WHEN status='ok' AND ms IS NOT NULL THEN '['||ms||']' ELSE '[]' END WHERE reason='legacy'")
        conn.execute('CREATE INDEX IF NOT EXISTS samples_expiry ON samples(ts)')
        conn.execute('PRAGMA user_version=2')


def store_results(db, ts, targets, results, settings):
    for attempt in range(3):
        try:
            with connect(db) as conn:
                conn.execute('BEGIN IMMEDIATE')
                for (target_id, _), result in zip(targets, results):
                    if result is None:
                        continue
                    conn.execute('''INSERT OR REPLACE INTO samples
                    (target_id,ts,ms,status,reason,sent,received,min_ms,max_ms,rtts,interval)
                    SELECT ?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM targets WHERE id=? AND active=1)''',
                                 (target_id, ts, result.ms, result.status, result.reason, result.sent, result.received,
                                  min(result.rtts) if result.rtts else None, max(result.rtts) if result.rtts else None,
                                  json.dumps(result.rtts), settings.interval, target_id))
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            LOG.warning('database_busy attempt=%s', attempt+1)
            if attempt == 2:
                raise
            time.sleep(.1*(attempt+1))


def maintenance(db, settings, now):
    if not settings.enable_cleanup:
        return
    with connect(db) as conn:
        # Indexed and bounded: large backlogs are removed over successive cycles.
        conn.execute('DELETE FROM samples WHERE (target_id,ts) IN (SELECT target_id,ts FROM samples WHERE ts < ? ORDER BY ts LIMIT 5000)',
                     (now-settings.retention_days*86400,))
    with connect(db) as conn:
        conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
        conn.execute('PRAGMA incremental_vacuum(64)')
