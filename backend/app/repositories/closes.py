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
    """未决长短款的唯一权威查询：封账闸、封账预览、总览、清 pending 回执全部取自这里。

    策略 A（闸随表 / 全局互斥）：只看 variance_rows 是否存在 disposition='pending'，
    与营业日无关。pending_total 为有符号差额合计（短款为负、长款为正）。
    不存在第二份缓存或旁路计数，任何处置状态变化只改这张表，三处读数随之一起翻转。
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
) -> int:
    """状态机唯一写入口：仅 pending 可流转到终态，终态行不可再写。

    WHERE 同时钉住 id 与 disposition='pending'，并发重复处置时最多一笔生效，
    第二笔影响行数为 0 -> 调用方按 409 拒绝，回执 success 与行态不可能背离。
    返回实际改动行数（0 或 1）。
    """
    if disposition not in TERMINAL_DISPOSITIONS or disposition == PENDING_DISPOSITION:
        raise ValueError(f"非法终态: {disposition}")
    cur = conn.execute(
        """UPDATE variance_rows
           SET disposition=?, resolved_at=?, resolved_reason=?
           WHERE id=? AND disposition=?""",
        (disposition, resolved_at, reason, variance_id, PENDING_DISPOSITION),
    )
    return int(cur.rowcount or 0)


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
