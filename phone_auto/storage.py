"""SQLite 저장소 — 작업(run)·계정별 작업(job)·결제 기록(payment)·이벤트 로그·설정.

프로그램이 꺼지거나 컴퓨터가 재부팅돼도 database/app.db 에 상태가 남는다.
스레드마다 연결을 따로 쓰고(WAL), 쓰기는 락 하나로 직렬화한다.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "database" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  link TEXT NOT NULL, card TEXT NOT NULL, option_no INTEGER, quantity INTEGER NOT NULL DEFAULT 1,
  accounts TEXT NOT NULL, mode TEXT NOT NULL, device TEXT NOT NULL DEFAULT '',
  max_amount_per_account INTEGER NOT NULL DEFAULT 0, max_total_amount INTEGER NOT NULL DEFAULT 0,
  address_keyword TEXT NOT NULL DEFAULT '', pin_mode TEXT NOT NULL DEFAULT 'auto',
  stop_on_failure INTEGER NOT NULL DEFAULT 1, allow_repeat INTEGER NOT NULL DEFAULT 0,
  account_delay INTEGER NOT NULL DEFAULT 30,
  status TEXT NOT NULL DEFAULT 'registered', error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, account_no TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', state TEXT NOT NULL DEFAULT 'IDLE', detail TEXT NOT NULL DEFAULT '',
  order_no TEXT NOT NULL DEFAULT '', amount INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
  retries INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, run_id INTEGER, account_no TEXT NOT NULL, link TEXT NOT NULL DEFAULT '',
  amount INTEGER NOT NULL DEFAULT 0, card TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  order_no TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', simulated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, day TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id INTEGER, job_id INTEGER,
  level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_pay_acct ON payments(account_no, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""

DEFAULT_SETTINGS = {
    "cf_user_wait": 300,             # Cloudflare 사람 인증 대기(초)
}

_local = threading.local()
_write_lock = threading.RLock()
_db_path = DB_PATH


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def use_db(path: Path) -> None:
    """테스트용 — 다른 DB 파일을 쓴다."""
    global _db_path
    _db_path = Path(path)
    _local.__dict__.clear()
    init()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _db_path:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = c, _db_path
    return c


def init() -> None:
    with _write_lock:
        c = conn()
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
        c.commit()


def execute(sql: str, params: tuple = ()) -> int:
    with _write_lock:
        c = conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in query("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def set_setting(key: str, value) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


def add_event(level: str, kind: str, message: str, run_id: int | None = None, job_id: int | None = None) -> int:
    return execute("INSERT INTO events(ts, run_id, job_id, level, kind, message) VALUES (?,?,?,?,?,?)",
                   (now(), run_id, job_id, level, kind, message[:2000]))


def events_after(last_id: int, limit: int = 200) -> list[dict]:
    return query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit))
