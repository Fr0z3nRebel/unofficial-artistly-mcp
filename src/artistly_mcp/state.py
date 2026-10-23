import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings


class ArtistlyError(Exception):
    """An actionable, credential-free error suitable for tool output."""


class SessionExpired(ArtistlyError):
    def __init__(self) -> None:
        super().__init__('Artistly login is missing or expired. Run artistly-mcp login, then retry reads. '
                         'Check the existing job before retrying a generation.')


def private_json(path: Path, data: Any) -> None:
    """Atomic owner-only writes. Never place credentials in MCP output."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, settings: Settings):
        settings.prepare()
        self.session_path = settings.state_dir / 'session.json'
        self.db_path = settings.state_dir / 'jobs.sqlite3'
        # Restrictive mode applies before SQLite opens the file.
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db_path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs '
                       '(id TEXT PRIMARY KEY, active INTEGER NOT NULL, body TEXT NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(active) '
                       'WHERE active = 1')

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5)

    def session(self) -> dict[str, Any]:
        try:
            value = json.loads(self.session_path.read_text())
            if not isinstance(value.get('cookies'), list):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError, AttributeError):
            raise SessionExpired() from None

    def save_session(self, data: dict[str, Any]) -> None:
        private_json(self.session_path, data)

    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, 'job_id': 'job_' + uuid4().hex, 'status': 'pending', 'design_uuids': []}
        try:
            with self.connect() as db:
                db.execute('INSERT INTO jobs VALUES (?, 1, ?)',
                           (body['job_id'], json.dumps(body)))
        except sqlite3.IntegrityError:
            with self.connect() as db:
                row = db.execute('SELECT id FROM jobs WHERE active = 1').fetchone()
            raise ArtistlyError(f'A job is still active ({row[0] if row else "unknown"}). '
                                'Use artistly_get_design to resume it before generating again.') from None
        return body

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT body FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            raise ArtistlyError('Unknown local job ID. Use the reference returned by generation.')
        return json.loads(row[0])

    def save_job(self, body: dict[str, Any]) -> None:
        active = body['status'] not in ('completed', 'failed', 'abandoned')
        with self.connect() as db:
            db.execute('UPDATE jobs SET active = ?, body = ? WHERE id = ? AND active = 1',
                       (int(active), json.dumps(body), body['job_id']))

    def list_jobs(self) -> list[dict[str, str]]:
        with self.connect() as db:
            bodies = [json.loads(row[0]) for row in db.execute('SELECT body FROM jobs ORDER BY rowid DESC LIMIT 20')]
        return [{'job_id': j['job_id'], 'status': j['status']} for j in bodies]
