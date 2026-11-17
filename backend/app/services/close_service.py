from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.seed import PENDING_DISPOSITION, RESOLVABLE_DISPOSITIONS


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def gate_status() -> dict[str, Any]:
    conn = connect()
    try:
        return close_repo.pending_summary(conn)
    finally:
        conn.close()


def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        total, count = line_repo.day_total(conn, day)
        return {
            "business_day": day,
            "system_total": total,
            "line_count": count,
            "sealed": close_repo.is_sealed(conn, day),
            "lines": line_repo.list_lines(conn, day),
            # 未决闸状态与封账台、总览同源：同一个 pending_summary 查询
            "pending_gate": close_repo.pending_summary(conn),
        }
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    # 全局唯一写锁：与 resolve_variance 共用 BEGIN IMMEDIATE，并发交错时全序串行，
    # 「有 pending 却新封成功」与「已清仍拦」都不可能从竞态中漏出。
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未指定营业日")
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账")
        # 全局互斥闸（策略 A）：存在任意 pending 长短款时，禁止对任何营业日新封账。
        # 与预览/总览/清账回执共用 pending_summary 单一数据源、同一事务内读取，
        # 读到 blocked 就拒绝，读到 open 才允许落 sealed，不存在第二套口径。
        gate = close_repo.pending_summary(conn)
        if gate["blocked"]:
            raise HTTPException(
                409,
                f"未决长短款闸拦截：存在 {gate['pending_count']} 笔 pending "
                f"（未决合计 {gate['pending_total']:.2f}），必须先清为认损/认溢/核销终态后才能封账",
            )
        declared = float(body["declared_cash"])
        total, count = line_repo.day_total(conn, day)
        variance = round(declared - total, 2)
        sealed_at = _now()
        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            system_total=total,
            declared_cash=declared,
            variance=variance,
            line_count=count,
            status="sealed",
            sealed_at=sealed_at,
            note=body.get("note"),
        )
        disposition = PENDING_DISPOSITION if abs(variance) > 1e-9 else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "system_total": total,
            "declared_cash": declared,
            "variance": variance,
            "line_count": count,
            "status": "sealed",
            "disposition": disposition,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_variance(variance_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """清 pending 的唯一写入口：pending -> 认损/认溢/核销（终态）。

    封账闸读的是同一张 variance_rows 表的同一个 pending 判定，
    本事务提交后闸状态立即翻转，不存在「回执 success 而行态未改」或「页面已清仍拒封」。
    """
    disposition = (body.get("disposition") or "").strip()
    if disposition not in RESOLVABLE_DISPOSITIONS:
        raise HTTPException(
            400,
            f"非法处置终态 {disposition!r}，可选：{', '.join(RESOLVABLE_DISPOSITIONS)}",
        )
    reason = body.get("reason") or None
    conn = connect(immediate=True)
    try:
        row = close_repo.get_variance(conn, variance_id)
        if not row:
            raise HTTPException(404, "长短款记录不存在")
        if row["disposition"] != PENDING_DISPOSITION:
            raise HTTPException(
                409,
                f"长短款 {variance_id} 已处于终态 {row['disposition']}，不可重复处置",
            )
        changed = close_repo.resolve_variance(
            conn,
            variance_id,
            disposition=disposition,
            resolved_at=_now(),
            reason=reason,
        )
        if changed == 0:
            # 并发下被另一笔处置抢先：行仍是/已是终态，拒绝并回滚，不发假 success。
            raise HTTPException(
                409,
                f"长短款 {variance_id} 已被并发处置为终态，本次处置未生效",
            )
        conn.commit()
        resolved = close_repo.get_variance(conn, variance_id)
        # 回执与行态、闸状态同一事务产出，三处必然一致
        gate = close_repo.pending_summary(conn)
        return {"variance": resolved, "pending_gate": gate}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_batches(conn)
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_variances(conn)
    finally:
        conn.close()
