"""Platform settings and endpoint reservations; SQLite transactions own state."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from .config import default_platform, validate_platform



def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (endpoint TEXT PRIMARY KEY, job TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('platform', json.dumps(default_platform(), ensure_ascii=False)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / 'console.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def platform(self):
        with self.connect() as db:
            return json.loads(db.execute('SELECT value FROM settings WHERE key=?', ('platform',)).fetchone()[0])

    def save_platform(self, value):
        value = validate_platform(value)
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM reservations LIMIT 1').fetchone():
                raise ValueError('有任务或环境准备占用平台；先结束并清理，再修改档案')
            db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value, ensure_ascii=False), 'platform'))
        return value
