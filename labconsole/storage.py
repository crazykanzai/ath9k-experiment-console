"""Durable local jobs and endpoint reservations; SQLite transactions own state."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .config import default_platform, validate_platform
from .plans import digest

TERMINAL = {'completed', 'stopped', 'gate_failed', 'worker_failed'}


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
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                plan TEXT NOT NULL, platform TEXT NOT NULL, seal TEXT NOT NULL,
                created REAL NOT NULL, heartbeat REAL NOT NULL, pid INTEGER,
                stop TEXT, stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                error TEXT, fault_at INTEGER, delay REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (endpoint TEXT PRIMARY KEY, job TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, at REAL NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL);
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
                raise ValueError('有离线任务占用平台，结束后再修改档案')
            db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value, ensure_ascii=False), 'platform'))
        return value

    def create(self, plan, request_key, delay=.2, fault_at=None, expected_platform_hash=None):
        if not isinstance(request_key, str) or not 8 <= len(request_key) <= 100:
            raise ValueError('启动请求标识无效')
        if not isinstance(delay, (int, float)) or not .01 <= delay <= 2:
            raise ValueError('离线阶段演练时长须为0.01～2秒')
        if fault_at is not None and (type(fault_at) is not int or not 1 <= fault_at <= len(plan['formal_inputs'])):
            raise ValueError('故障演练轮次无效')
        with self.transaction() as db:
            prior = db.execute('SELECT id FROM jobs WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                return prior['id'], False
            platform = json.loads(db.execute('SELECT value FROM settings WHERE key=?', ('platform',)).fetchone()[0])
            if expected_platform_hash is not None and digest(platform) != expected_platform_hash:
                raise ValueError('平台在审阅后发生变化，请重新生成计划')
            endpoints = sorted(n['address'] + ':' + str(n['port']) for n in platform['nodes'])
            if any(db.execute('SELECT 1 FROM reservations WHERE endpoint=?', (e,)).fetchone() for e in endpoints):
                raise ValueError('本工作区的平台已有未结束任务；不能重复启动或强行抢占')
            job = uuid.uuid4().hex
            seal = digest({'plan': plan, 'platform': platform})
            now = time.time()
            db.execute('INSERT INTO jobs(id,request_key,status,plan,platform,seal,created,heartbeat,stage,fault_at,delay) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (job, request_key, 'queued', json.dumps(plan, ensure_ascii=False), json.dumps(platform, ensure_ascii=False), seal, now, now, 'queued', fault_at, delay))
            for e in endpoints:
                db.execute('INSERT INTO reservations VALUES (?,?)', (e, job))
        folder = self.root / 'runs' / job
        atomic_json(folder / 'plan.json', plan)
        atomic_json(folder / 'platform.json', platform)
        atomic_json(folder / 'seal.json', {'sha256': seal, 'mode': 'offline', 'real_measurement': False})
        self.event(job, 'created', '仅离线演练；不会连接节点或产生实验指标')
        return job, True

    def job(self, job):
        if not isinstance(job, str) or len(job) != 32 or any(c not in '0123456789abcdef' for c in job):
            raise ValueError('任务ID无效')
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('找不到任务')
        result = dict(row)
        for k in ['plan', 'platform']:
            result[k] = json.loads(result[k])
        result['heartbeat_stale'] = result['status'] not in TERMINAL and time.time() - result['heartbeat'] > 15
        return result

    def jobs(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 100')]
        return [self.job(x) for x in ids]

    def claim(self, job):
        with self.transaction() as db:
            return db.execute('UPDATE jobs SET status=?,pid=?,heartbeat=? WHERE id=? AND status=?',
                              ('running', os.getpid(), time.time(), job, 'queued')).rowcount == 1

    def update(self, job, stage, progress=None):
        with self.connect() as db:
            if progress is None:
                db.execute('UPDATE jobs SET stage=?,heartbeat=? WHERE id=?', (stage, time.time(), job))
            else:
                db.execute('UPDATE jobs SET stage=?,progress=?,heartbeat=? WHERE id=?', (stage, progress, time.time(), job))

    def event(self, job, kind, detail):
        with self.connect() as db:
            db.execute('INSERT INTO events(job,at,kind,detail) VALUES (?,?,?,?)', (job, time.time(), kind, detail))

    def events(self, job):
        self.job(job)
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM events WHERE job=? ORDER BY seq DESC LIMIT 100', (job,))][::-1]

    def stop(self, job, mode):
        if mode not in ['immediate', 'after_round']:
            raise ValueError('停止方式无效')
        with self.transaction() as db:
            row = db.execute('SELECT status,stop FROM jobs WHERE id=?', (job,)).fetchone()
            if row is None:
                raise ValueError('找不到任务')
            if row['status'] in TERMINAL:
                return
            if row['stop'] != 'immediate':
                db.execute('UPDATE jobs SET stop=? WHERE id=?', (mode, job))
        self.event(job, 'stop_requested', mode)

    def finish(self, job, status, error=None):
        if status not in TERMINAL:
            raise ValueError('无效终态')
        with self.transaction() as db:
            db.execute('UPDATE jobs SET status=?,stage=?,heartbeat=?,error=? WHERE id=?', (status, status, time.time(), error, job))
            db.execute('DELETE FROM reservations WHERE job=?', (job,))
