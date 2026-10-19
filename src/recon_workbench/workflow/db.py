"""SQLite 连接、建表与事务。

约定：
- 所有写操作显式包在 ``transaction(conn)`` 里（BEGIN IMMEDIATE），避免并发写互相覆盖；
- 表结构用 ``IF NOT EXISTS`` + ``PRAGMA user_version`` 管理版本，阶段 3 只有 v1；
- 开关外键与 WAL，保证本地多进程（CLI/测试/后续 API）同时读写不至于锁死。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    ledger_path       TEXT NOT NULL,
    statement_path    TEXT NOT NULL,
    ledger_filename   TEXT NOT NULL,
    statement_filename TEXT NOT NULL,
    ledger_sha256     TEXT NOT NULL,
    statement_sha256  TEXT NOT NULL,
    policy_json       TEXT NOT NULL,
    review_json       TEXT NOT NULL,
    reconcile_digest  TEXT,
    review_digest     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_stages (
    job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (job_id, stage)
);

CREATE TABLE IF NOT EXISTS recon_items (
    job_id           TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    item_id          TEXT NOT NULL,
    status           TEXT NOT NULL,
    invoice_no       TEXT NOT NULL,
    counterparty     TEXT NOT NULL,
    entry_date       TEXT NOT NULL,
    currency         TEXT NOT NULL,
    ledger_amount    TEXT,
    statement_amount TEXT,
    delta            TEXT,
    matched_by       TEXT,
    reason           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_status ON recon_items (job_id, status);

CREATE TABLE IF NOT EXISTS review_suggestions (
    job_id            TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    item_id           TEXT NOT NULL,
    invoice_no        TEXT NOT NULL DEFAULT '',
    counterparty      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    verdict           TEXT,
    category          TEXT,
    confidence        REAL,
    explanation       TEXT,
    recommended_action TEXT,
    evidence          TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    error_code        TEXT,
    error_detail      TEXT,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, item_id)
);

CREATE TABLE IF NOT EXISTS human_decisions (
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    item_id    TEXT NOT NULL,
    decision   TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    note       TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (job_id, item_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    at      TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_job ON job_events (job_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证 schema 就绪。"""

    db_path = str(path)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
