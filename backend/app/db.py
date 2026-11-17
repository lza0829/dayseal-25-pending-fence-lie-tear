from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH


def connect(*, immediate: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None if not immediate else "EXCLUSIVE", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发清 pending 与封账交错时，后来的写事务排队等锁，而不是直接抛 locked，
    # 配合 BEGIN IMMEDIATE 把所有写路径串成全序，杜绝半截成功。
    conn.execute("PRAGMA busy_timeout=5000")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn
