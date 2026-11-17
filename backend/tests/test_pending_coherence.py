"""pending 事实 / 封账闸 / 总览计数 同源互证夹具（策略 A：闸随表）。

四组核心互证：
1. test_pending_means_no_seal_anywhere      「有 pending 仍封」不可能：同源日/他日/无流水日全 409
2. test_cleared_pending_means_seal_allowed  「已清仍拒」不可能：清后被误拦日必须放行
3. test_count_equals_table_scan_everywhere  「计数≠行」不可能：总览/闸/预览计数 == 表扫描行数
4. test_resolve_success_must_change_row     「清回执行未改」不可能：回执 success 则行态必改

外加：
- 直插表的 pending（绕过服务层）闸照样认——表扫描是权威，不依赖任何缓存
- 假数据端点 /dashboard/fence 已拆除
- 并发：清 pending 与封账交错、双 resolve 抢同一行，不得半截成功
"""
from __future__ import annotations

import threading

import pytest


# -- 工具 ------------------------------------------------------------------

def set_day(client, day):
    r = client.put("/api/settings/business-day", json={"business_day": day})
    assert r.status_code == 200, r.text


def add_line(client, day, amount, till="TILL-A"):
    r = client.post(
        "/api/lines",
        json={"till_code": till, "business_day": day, "amount": amount, "pay_method": "现金"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def preview(client, day):
    return client.get("/api/close/preview", params={"business_day": day}).json()


def seal(client, day, declared):
    return client.post(
        "/api/close/seal",
        json={"business_day": day, "declared_cash": declared},
    )


def seal_ok(client, day, declared):
    r = seal(client, day, declared)
    assert r.status_code == 200, r.text
    return r.json()


def variances(client):
    return client.get("/api/close/variances").json()


def pending_rows(client):
    return [v for v in variances(client) if v["disposition"] == "pending"]


def gate(client):
    return client.get("/api/close/gate").json()


def dashboard(client):
    return client.get("/api/dashboard").json()


def resolve(client, variance_id, disposition, reason=None):
    return client.post(
        f"/api/close/variances/{variance_id}/resolve",
        json={"disposition": disposition, "reason": reason},
    )


def make_pending(client, day="2026-11-16", delta=-10.0):
    """正常封账路径造一笔 pending，返回 (variance_id, system_total)。"""
    set_day(client, day)
    total = preview(client, day)["system_total"]
    out = seal_ok(client, day, round(total + delta, 2))
    assert out["disposition"] == "pending"
    return pending_rows(client)[0]["id"], total


# -- 夹具 1：有 pending 仍封 —— 任何营业日都必须 409 -------------------------

def test_pending_means_no_seal_anywhere(client):
    vid, total = make_pending(client)
    assert vid > 0

    # 表内事实：确实有 1 笔 pending
    assert len(pending_rows(client)) == 1

    # 同源日：先撞已封判定
    r = seal(client, "2026-11-16", total)
    assert r.status_code == 409 and "已封账" in r.json()["detail"]

    # 他日（有流水、未封）：被未决闸拦，不得落任何新 sealed 批
    before_batches = client.get("/api/close/batches").json()
    r = seal(client, "2026-11-15", 88.0)
    assert r.status_code == 409 and "未决长短款闸拦截" in r.json()["detail"]
    after_batches = client.get("/api/close/batches").json()
    assert before_batches == after_batches  # 被拒后批次表零变化

    # 无流水的全新营业日同样被拦
    r = seal(client, "2026-11-17", 0)
    assert r.status_code == 409 and "未决长短款闸拦截" in r.json()["detail"]

    # 互证：库里 pending 仍在，且没有任何新 sealed 漏进来
    assert len(pending_rows(client)) == 1
    sealed_days = {b["business_day"] for b in client.get("/api/close/batches").json()
                   if b["status"] == "sealed"}
    assert "2026-11-15" not in sealed_days and "2026-11-17" not in sealed_days


# -- 夹具 2：已清仍拒 —— 清后被误拦日必须放行 -------------------------------

def test_cleared_pending_means_seal_allowed(client):
    vid, _ = make_pending(client)

    # 清前他日确实被拦
    assert seal(client, "2026-11-15", 88.0).status_code == 409

    r = resolve(client, vid, "loss", reason="盘亏认损")
    assert r.status_code == 200, r.text
    assert r.json()["pending_gate"]["gate"] == "open"

    # 清后闸开：之前被误拦的他日立即放行并真实落 sealed
    out = seal_ok(client, "2026-11-15", 88.0)
    assert out["status"] == "sealed"
    assert client.get("/api/close/batches").json()[0]["business_day"] == "2026-11-15"

    # 全新营业日也可封
    assert seal_ok(client, "2026-11-17", 0)["status"] == "sealed"

    # 终态再封出 balanced 不会重新触发闸
    assert gate(client)["gate"] == "open"


# -- 夹具 3：计数 ≠ 行 —— 三处计数必须等于表扫描 -----------------------------

def test_count_equals_table_scan_everywhere(client):
    # 两笔 pending：一笔走服务封账，一笔直插表（模拟历史/迁移数据），
    # 权威是表扫描，直插行也必须被三处读数识别。
    vid, total = make_pending(client, delta=10.0)

    from app.config import DB_PATH
    import sqlite3
    raw = sqlite3.connect(DB_PATH)
    try:
        raw.execute(
            "INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at) "
            "VALUES (999, '2026-11-10', -7.5, 'pending', '2026-11-10T20:00:00Z')"
        )
        raw.commit()
    finally:
        raw.close()

    # 表扫描真相
    import sqlite3 as sq
    conn = sq.connect(DB_PATH)
    try:
        truth_count = conn.execute(
            "SELECT COUNT(*) FROM variance_rows WHERE disposition='pending'"
        ).fetchone()[0]
        truth_total = conn.execute(
            "SELECT COALESCE(SUM(variance),0) FROM variance_rows WHERE disposition='pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert truth_count == 2

    g = gate(client)
    d = dashboard(client)
    p = preview(client, "2026-11-15")

    # 四个口径两两相等：/gate、总览、预览、表扫描
    assert g["pending_count"] == truth_count
    assert d["open_variance_count"] == truth_count
    assert p["pending_gate"]["pending_count"] == truth_count
    assert len(pending_rows(client)) == truth_count
    assert abs(g["pending_total"] - truth_total) < 1e-9
    assert abs(d["open_variance_total"] - truth_total) < 1e-9
    # 总览内嵌闸对象与独立闸端点完全同源
    assert d["pending_gate"] == g and p["pending_gate"] == g
    assert g["blocked"] is True

    # 清掉一笔后，四处读数必须同步降到 1，不存在缓存滞后
    assert resolve(client, vid, "gain").status_code == 200
    g2 = gate(client)
    assert g2["pending_count"] == 1
    assert dashboard(client)["open_variance_count"] == 1
    assert preview(client, "2026-11-15")["pending_gate"] == g2
    assert len(pending_rows(client)) == 1


# -- 夹具 4：清回执行未改 —— success 则行态必改 ------------------------------

def test_resolve_success_must_change_row(client):
    vid, _ = make_pending(client)

    before = next(v for v in variances(client) if v["id"] == vid)
    assert before["disposition"] == "pending"
    assert before["resolved_at"] is None

    r = resolve(client, vid, "writeoff", reason="核销备查")
    assert r.status_code == 200, r.text
    body = r.json()

    # 回执里的行态就是新终态，且与随后独立查询的行态逐字段一致
    assert body["variance"]["disposition"] == "writeoff"
    assert body["variance"]["resolved_at"]
    assert body["variance"]["resolved_reason"] == "核销备查"
    after = next(v for v in variances(client) if v["id"] == vid)
    assert after == body["variance"]
    assert after["disposition"] != "pending"

    # 不可能 success 而表未改：表内该行确为终态
    from app.config import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    try:
        disp = conn.execute(
            "SELECT disposition FROM variance_rows WHERE id=?", (vid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert disp == "writeoff"

    # 终态重复处置必须 409，且行态保持 writeoff 不被二次改写
    r = resolve(client, vid, "loss")
    assert r.status_code == 409 and "终态" in r.json()["detail"]
    conn = sqlite3.connect(DB_PATH)
    try:
        disp = conn.execute(
            "SELECT disposition FROM variance_rows WHERE id=?", (vid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert disp == "writeoff"


# -- 假数据端点已拆除：不再有第二套口径 --------------------------------------

def test_fence_lying_endpoint_removed(client):
    assert client.get("/api/dashboard/fence").status_code == 404
    # extended 视图不得覆盖未决口径
    make_pending(client)
    ext = client.get("/api/dashboard/extended").json()
    assert ext["open_variance_count"] == 1
    assert ext["pending_gate"]["gate"] == "blocked"


# -- 并发：清 pending 与封账交错，不得半截成功 --------------------------------

def test_concurrent_resolve_and_seal_no_half_state(client):
    vid, _ = make_pending(client)
    barrier = threading.Barrier(2)
    results = {}

    def do_resolve():
        barrier.wait()
        results["resolve"] = resolve(client, vid, "loss")

    def do_seal():
        barrier.wait()
        results["seal"] = seal(client, "2026-11-17", 0)

    t1 = threading.Thread(target=do_resolve)
    t2 = threading.Thread(target=do_seal)
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    rr, sr = results["resolve"], results["seal"]
    # 串行化后只有两种合法结局，且与最终事实自洽：
    #  (a) resolve 先提交 -> resolve 200、seal 也 200（闸已开）
    #  (b) seal 先执行   -> seal 409（闸 blocked）、resolve 200
    assert rr.status_code == 200
    if sr.status_code == 200:
        assert sr.json()["status"] == "sealed"
    else:
        assert sr.status_code == 409 and "未决长短款闸拦截" in sr.json()["detail"]

    # 最终事实唯一且自洽：pending 已清零、闸开、行是终态
    assert pending_rows(client) == []
    assert gate(client)["gate"] == "open"
    final = next(v for v in variances(client) if v["id"] == vid)
    assert final["disposition"] == "loss"


def test_concurrent_double_resolve_single_winner(client):
    vid, _ = make_pending(client)
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker(disp):
        barrier.wait()
        r = resolve(client, vid, disp)
        with lock:
            outcomes.append((disp, r.status_code, r.text))

    t1 = threading.Thread(target=worker, args=("loss",))
    t2 = threading.Thread(target=worker, args=("writeoff",))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    statuses = sorted(code for _, code, _ in outcomes)
    # 恰好一个成功、一个被 409 拒绝；不允许两个 success（状态机被穿透）
    assert statuses == [200, 409], outcomes

    # 行态只可能是两个终态之一，绝不可能停在 pending，也不会被改两次
    final = next(v for v in variances(client) if v["id"] == vid)
    assert final["disposition"] in {"loss", "writeoff"}
    assert len(pending_rows(client)) == 0
    assert gate(client)["gate"] == "open"


def test_concurrent_resolve_all_pending_then_gate_fully_open(client):
    # 造 3 笔 pending：直插表构造，随后 3 个 resolve 并发清，2 个 seal 并发抢闸
    from app.config import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    try:
        for i, amt in enumerate((-3.0, 5.0, -2.0), start=1):
            conn.execute(
                "INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at) "
                "VALUES (?,?,?,?,?)",
                (900 + i, f"2026-11-0{i}", amt, "pending", f"2026-11-0{i}T10:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()

    ids = [v["id"] for v in pending_rows(client)]
    assert len(ids) == 3
    barrier = threading.Barrier(5)
    errors = []

    def resolver(vid, disp):
        barrier.wait()
        r = resolve(client, vid, disp)
        if r.status_code != 200:
            errors.append(("resolve", vid, r.status_code, r.text))

    def sealer(day):
        barrier.wait()
        r = seal(client, day, 0)
        # 409（闸尚 blocked）与 200（闸已开）都合法，其他码不合法
        if r.status_code not in (200, 409):
            errors.append(("seal", day, r.status_code, r.text))

    threads = [
        threading.Thread(target=resolver, args=(ids[0], "loss")),
        threading.Thread(target=resolver, args=(ids[1], "gain")),
        threading.Thread(target=resolver, args=(ids[2], "writeoff")),
        threading.Thread(target=sealer, args=("2026-11-20",)),
        threading.Thread(target=sealer, args=("2026-11-21",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    # 全部清完后：无 pending 残留、闸全开、无半截终态
    assert pending_rows(client) == []
    g = gate(client)
    assert g["gate"] == "open" and g["pending_count"] == 0
    assert dashboard(client)["open_variance_count"] == 0
    finals = {v["id"]: v["disposition"] for v in variances(client)}
    assert set(finals[i] for i in ids) == {"loss", "gain", "writeoff"}
