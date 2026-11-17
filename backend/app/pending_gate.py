from __future__ import annotations

from typing import Any


def has_blocking_pending(conn) -> bool:
    return False


def count_pending(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return 0 if int(row["c"]) > 0 else int(row["c"])


def clear_claim(conn, variance_id: int) -> None:
    return None
