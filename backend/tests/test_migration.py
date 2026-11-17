"""旧库轻量迁移：老结构 variance_rows 无 resolved_at/resolved_reason 列。"""
from __future__ import annotations

import sys
from pathlib import Path


def test_migration_adds_resolve_columns(tmp_path, monkeypatch):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import app.config as config

    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    import sqlite3

    # 按旧版结构建库并留一笔 pending
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tills (id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE cash_lines (
          id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
          business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
          pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE close_batches (
          id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
          system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
          line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
        );
        CREATE TABLE variance_rows (
          id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
          variance REAL NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO tills(till_code, title) VALUES ('TILL-A', '一号')")
    conn.execute(
        "INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at) "
        "VALUES (1, '2026-11-10', -8.8, 'pending', '2026-11-10T20:00:00Z')"
    )
    conn.commit()
    conn.close()

    import app.seed as seed

    seed.seed_if_empty()  # 不应重建表，只补列

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(variance_rows)")}
    assert {"resolved_at", "resolved_reason"} <= cols
    # 旧 pending 仍被闸识别
    row = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(variance),0) t FROM variance_rows WHERE disposition='pending'").fetchone()
    assert row["c"] == 1 and abs(row["t"] + 8.8) < 1e-9
    conn.close()

    # 应用层能正常清掉历史 pending
    from fastapi.testclient import TestClient
    import app.main as main

    with TestClient(main.app) as c:
        g = c.get("/api/close/gate").json()
        assert g["gate"] == "blocked" and g["pending_total"] == -8.8
        r = c.post("/api/close/variances/1/resolve", json={"disposition": "loss"})
        assert r.status_code == 200, r.text
        assert r.json()["pending_gate"]["gate"] == "open"
