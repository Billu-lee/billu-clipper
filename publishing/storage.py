"""Private local tokens and a durable upload ledger (never served by Flask)."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / 'publishing.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    platform TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    account TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    data TEXT NOT NULL,
                    UNIQUE(platform, account, fingerprint));
            ''')
            # Do not re-submit ambiguous requests after a restart: they might
            # already have been accepted remotely. The user must check first.
            for row in db.execute('SELECT upload_id, data FROM uploads').fetchall():
                data = json.loads(row['data'])
                if data['status'] in ('queued', 'uploading'):
                    data.update(status='uncertain', message='Upload interrupted by a server restart. Check the platform before uploading again.', error='Upload outcome is unknown.')
                    db.execute('UPDATE uploads SET data=? WHERE upload_id=?', (json.dumps(data), row['upload_id']))
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def account(self, platform):
        with self.db() as db:
            row = db.execute('SELECT data FROM accounts WHERE platform=?', (platform,)).fetchone()
            return json.loads(row['data']) if row else None

    def save_account(self, platform, data):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?)', (platform, json.dumps(data)))

    def disconnect(self, platform):
        with self.db() as db:
            db.execute('DELETE FROM accounts WHERE platform=?', (platform,))

    def create(self, platform, account, fingerprint, filename, retry=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM uploads WHERE platform=? AND account=? AND fingerprint=?',
                             (platform, account, fingerprint)).fetchone()
            if row:
                data = json.loads(row['data'])
                if retry and data['status'] == 'failed':
                    data.update(status='queued', progress=0, error=None, message='Retrying upload...')
                    db.execute('UPDATE uploads SET data=? WHERE upload_id=?',(json.dumps(data),data['upload_id']))
                    return data, True
                return data, False
            data = dict(upload_id=uuid4().hex, platform=platform, account=account,
                        filename=filename, status='queued', progress=0, message='Waiting to upload...',
                        error=None, url=None, remote_id=None, created_at=time.time())
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?)',
                       (data['upload_id'], platform, account, fingerprint, json.dumps(data)))
            return data, True

    def get(self, upload_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM uploads WHERE upload_id=?', (upload_id,)).fetchone()
            return json.loads(row['data']) if row else None

    def update(self, upload_id, **changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM uploads WHERE upload_id=?', (upload_id,)).fetchone()
            data = json.loads(row['data'])
            if 'progress' in changes:
                changes['progress'] = round(max(data['progress'], min(100, changes['progress'])), 1)
            data.update(changes, updated_at=time.time())
            db.execute('UPDATE uploads SET data=? WHERE upload_id=?', (json.dumps(data), upload_id))
