from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_lines(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return line_repo.list_lines(conn, business_day)
    finally:
        conn.close()


def create_line(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未设置营业日")
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账，禁止新增流水")
        created = _now()
        lid = line_repo.insert_line(
            conn,
            line_no=line_repo.next_line_no(conn),
            till_code=body["till_code"],
            business_day=day,
            occurred_at=body.get("occurred_at") or created,
            amount=float(body["amount"]),
            pay_method=body.get("pay_method") or "现金",
            note=body.get("note"),
            created_at=created,
        )
        conn.commit()
        return line_repo.get_line(conn, lid)  # type: ignore
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def patch_line(line_id: int, body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(409, f"营业日 {row['business_day']} 已封账，禁止修改流水")
        line_repo.update_line(
            conn,
            line_id,
            amount=float(body.get("amount", row["amount"])),
            pay_method=body.get("pay_method", row["pay_method"]),
            note=body.get("note", row["note"]),
        )
        conn.commit()
        return line_repo.get_line(conn, line_id)  # type: ignore
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_line(line_id: int) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        if close_repo.is_sealed(conn, row["business_day"]):
            raise HTTPException(409, f"营业日 {row['business_day']} 已封账，禁止删除流水")
        line_repo.delete_line(conn, line_id)
        conn.commit()
        return {"deleted": True, "id": line_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
