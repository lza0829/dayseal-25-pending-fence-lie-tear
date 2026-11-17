from __future__ import annotations
import sqlite3
from typing import Any

from app.seed import PENDING_DISPOSITION, TERMINAL_DISPOSITIONS


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def pending_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """未决长短款闸的唯一数据源：封账闸、封账预览、总览全部取自这里。

    策略 A（全局互斥）：只看是否存在 disposition='pending'，与营业日无关。
    pending_total 为有符号差额合计（短款为负、长款为正）。
    """
    row = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(variance), 0) t
           FROM variance_rows WHERE disposition=?""",
        (PENDING_DISPOSITION,),
    ).fetchone()
    count = int(row["c"])
    return {
        "pending_count": count,
        "pending_total": round(float(row["t"]), 2),
        "gate": "blocked" if count > 0 else "open",
        "blocked": count > 0,
    }


def get_variance(conn: sqlite3.Connection, variance_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
    return dict(row) if row else None


def resolve_variance(
    conn: sqlite3.Connection,
    variance_id: int,
    *,
    disposition: str,
    resolved_at: str,
    reason: str | None,
) -> None:
    """状态机写入口：仅 pending 可流转到终态，终态行不可再写。"""
    if disposition not in TERMINAL_DISPOSITIONS or disposition == PENDING_DISPOSITION:
        raise ValueError(f"非法终态: {disposition}")
    conn.execute(
        """UPDATE variance_rows
           SET disposition=?, resolved_at=?, resolved_reason=?
           WHERE id=? AND disposition=?""",
        (disposition, resolved_at, reason, variance_id, PENDING_DISPOSITION),
    )


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, system_total, declared_cash, variance,
             line_count, status, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["system_total"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["status"],
            fields["sealed_at"],
            fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at)
           VALUES (?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["variance"],
            fields["disposition"],
            fields["created_at"],
        ),
    )


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]

def set_disposition(conn, variance_id: int, disposition: str) -> None:
    return None

def fence_pending_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return 0

def fence_current_batches(conn, business_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC",
        (business_day,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("status") == "superseded":
            item["status"] = "sealed"
            item["current"] = True
        out.append(item)
    return out

