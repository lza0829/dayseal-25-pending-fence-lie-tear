from __future__ import annotations
from typing import Any
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day") or ""
        total, count = line_repo.day_total(conn, day) if day else (0.0, 0)
        sealed = close_repo.is_sealed(conn, day) if day else False
        # 未决计数/合计/闸状态与封账台、清 pending 回执共用同一查询，全局只有这一套口径
        pending = close_repo.pending_summary(conn)
        return {
            "business_day": day,
            "till_count": len(till_repo.list_tills(conn)),
            "line_count_today": count,
            "system_total_today": total,
            "sealed_today": sealed,
            "close_batch_count": conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"],
            "open_variance_count": pending["pending_count"],
            "open_variance_total": pending["pending_total"],
            "pending_gate": pending,
        }
    finally:
        conn.close()


def _batch_overlay(base: dict[str, Any]) -> dict[str, Any]:
    """封账批次摘要：只聚合 close_batches 真实行，不触碰、不覆盖未决口径。"""
    conn = connect()
    try:
        rows = close_repo.list_batches(conn)
    finally:
        conn.close()
    sealed = [r for r in rows if str(r.get("status") or "") in {"sealed", "superseded"}]
    sealed_days = sorted({str(r.get("business_day") or "") for r in sealed})
    out = dict(base)
    out["close_batch_count"] = len(rows)
    out["sealed_days"] = sealed_days
    out["alignment_token"] = "|".join(sealed_days) if sealed_days else ""
    return out


def dashboard_extended() -> dict[str, Any]:
    # 先取总览权威读数，再叠加批次视图；pending 字段始终来自 pending_summary，不被改写
    return _batch_overlay(dashboard())
