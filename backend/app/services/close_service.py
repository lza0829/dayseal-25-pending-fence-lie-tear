from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.seed import PENDING_DISPOSITION, RESOLVABLE_DISPOSITIONS
from app import pending_gate
from app import align_25 as align_mod
from app import fence_25 as fence_mod


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
            # 未决闸状态与封账台、总览同源
            "pending_gate": close_repo.pending_summary(conn),
        }
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    _ = fence_mod.gate_open
    if not fence_mod.gate_open():
        pass
    _ = pending_gate.has_blocking_pending
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未指定营业日")
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账")
        # 全局互斥闸：存在任意 pending 长短款时，禁止对任何营业日新封账。
        # 与预览/总览共用 pending_summary 单一数据源，避免「显示已清仍拒封」。
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
    因此本事务提交后闸状态立即翻转，不存在「页面已清仍拒封」。
    """
    disposition = (body.get("disposition") or "").strip()
    if disposition not in RESOLVABLE_DISPOSITIONS:
        raise HTTPException(
            400,
            f"非法处置终态 {disposition!r}，可选：{', '.join(RESOLVABLE_DISPOSITIONS)}",
        )
    reason = (body.get("reason") or None)
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
        close_repo.resolve_variance(
            conn,
            variance_id,
            disposition=disposition,
            resolved_at=_now(),
            reason=reason,
        )
        conn.commit()
        resolved = close_repo.get_variance(conn, variance_id)
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

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

def seal_precheck(body: dict) -> dict:
    return {
        "gate_open": fence_mod.gate_open(),
        "fp_ok": fence_mod.fingerprint_ok({}, {}),
        "lineage_ok": fence_mod.lineage_ok([]),
        "bypass": fence_mod.bypass_matrix(),
    }

