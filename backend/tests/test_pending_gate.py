"""未决长短款全局互斥闸（策略 A）端到端测试。

覆盖：
- 无 pending：任意营业日可封；balanced 封账不产生 pending
- 制造 pending：同源日/他日的新封全部 409（拦一切）
- 清 pending 前后 /gate、/dashboard、/close/preview 三处闸状态同源可核
- 状态机：pending -> 认损/认溢/核销；终态不可再写；非法入参 400
- 清成终态后他日、同源日依次可封，不出现「仍有 pending 却已新封成功」
- 闸期内未封日流水仍可写（闸只拦新封，不扩大语义）
"""
from __future__ import annotations


# -- 工具函数 --------------------------------------------------------------

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


def seal_day(client, day, declared):
    return client.post(
        "/api/close/seal",
        json={"business_day": day, "declared_cash": declared},
    )


def seal_ok(client, day, declared):
    r = seal_day(client, day, declared)
    assert r.status_code == 200, r.text
    return r.json()


def list_variances(client):
    return client.get("/api/close/variances").json()


def pending_rows(client):
    return [v for v in list_variances(client) if v["disposition"] == "pending"]


def gate(client):
    return client.get("/api/close/gate").json()


def dashboard(client):
    return client.get("/api/dashboard").json()


def preview(client, day):
    return client.get("/api/close/preview", params={"business_day": day}).json()


def resolve(client, variance_id, disposition, reason=None):
    return client.post(
        f"/api/close/variances/{variance_id}/resolve",
        json={"disposition": disposition, "reason": reason},
    )


# -- 基线：闸开 ------------------------------------------------------------

def test_gate_open_when_no_pending(client):
    g = gate(client)
    assert g == {"pending_count": 0, "pending_total": 0.0, "gate": "open", "blocked": False}
    d = dashboard(client)
    assert d["open_variance_count"] == 0
    assert d["open_variance_total"] == 0.0
    assert d["pending_gate"]["gate"] == "open"


def test_balanced_seal_does_not_open_gate(client):
    # seed 中 2026-11-16 流水合计 489.40；实盘与系统一致 -> balanced
    set_day(client, "2026-11-16")
    total = preview(client, "2026-11-16")["system_total"]
    out = seal_ok(client, "2026-11-16", total)
    assert out["disposition"] == "balanced"
    assert gate(client)["gate"] == "open"
    assert pending_rows(client) == []


# -- 拦一切：pending 阻断所有营业日的新封 -----------------------------------

def _seed_one_pending(client) -> int:
    """造一笔 pending（短款 10.00）并核完拦截边界，返回该长短款 id。"""
    set_day(client, "2026-11-16")
    total = preview(client, "2026-11-16")["system_total"]

    # 1) 制造 pending（短款 10.00）
    out = seal_ok(client, "2026-11-16", round(total - 10.0, 2))
    assert out["disposition"] == "pending"
    vid = pending_rows(client)[0]["id"]

    # 2) 闸状态三处同源，均为 blocked，未决合计 = -10.00
    g = gate(client)
    assert g["blocked"] is True and g["gate"] == "blocked"
    assert g["pending_count"] == 1 and g["pending_total"] == -10.0
    assert dashboard(client)["pending_gate"] == g
    assert preview(client, "2026-11-16")["pending_gate"] == g
    assert dashboard(client)["open_variance_count"] == 1
    assert dashboard(client)["open_variance_total"] == -10.0

    # 3) 同源日再封 -> 409（当日已封，先撞已封判定）
    r = seal_day(client, "2026-11-16", total)
    assert r.status_code == 409 and "已封账" in r.json()["detail"]

    # 4) 他日（多日夹具：2026-11-15 有流水且未封）新封 -> 同样 409，被未决闸拦
    r = seal_day(client, "2026-11-15", 88.0)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "未决长短款闸拦截" in detail and "1 笔" in detail and "-10.00" in detail

    # 5) 一个完全没流水的新营业日也不许封
    r = seal_day(client, "2026-11-17", 0)
    assert r.status_code == 409 and "未决长短款闸拦截" in r.json()["detail"]

    # 6) 闸只拦新封：未封日的流水写操作不受影响
    set_day(client, "2026-11-15")
    r = add_line(client, "2026-11-15", 5.0)
    assert r["business_day"] == "2026-11-15"
    # 已封日的流水仍按原规则锁定
    r = client.post(
        "/api/lines",
        json={"till_code": "TILL-A", "business_day": "2026-11-16", "amount": 1.0},
    )
    assert r.status_code == 409
    return vid


def test_pending_blocks_seal_on_any_business_day(client):
    _seed_one_pending(client)


def test_two_pending_aggregate_count_and_total(client):
    # 第一日封出 pending（长款 +10）
    set_day(client, "2026-11-16")
    total = preview(client, "2026-11-16")["system_total"]
    seal_ok(client, "2026-11-16", round(total + 10.0, 2))
    assert gate(client)["pending_total"] == 10.0

    # 闸已 blocked，他日无法再封 -> 库里只能有 1 笔 pending
    r = seal_day(client, "2026-11-15", 1.0)
    assert r.status_code == 409
    assert len(pending_rows(client)) == 1


# -- 清 pending：状态机 + 闸翻转 --------------------------------------------

def test_resolve_to_terminal_opens_gate_then_other_day_can_seal(client):
    vid = _seed_one_pending(client)

    # 非法终态 / 空入参 -> 400
    r = resolve(client, vid, "approved")
    assert r.status_code == 400 and "非法处置终态" in r.json()["detail"]
    r = client.post(f"/api/close/variances/{vid}/resolve", json={})
    assert r.status_code == 400

    # 不存在的记录 -> 404
    assert resolve(client, 9999, "loss").status_code == 404

    # 清为认损（终态），响应直接回传翻转后的闸状态
    r = resolve(client, vid, "loss", reason="盘亏认损")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variance"]["disposition"] == "loss"
    assert body["variance"]["resolved_at"]
    assert body["variance"]["resolved_reason"] == "盘亏认损"
    assert body["pending_gate"]["gate"] == "open"
    assert body["pending_gate"]["pending_count"] == 0

    # 三处口径一致：没有「页面显示已清但仍拒封」
    assert gate(client) == body["pending_gate"]
    assert dashboard(client)["pending_gate"] == body["pending_gate"]
    assert preview(client, "2026-11-15")["pending_gate"] == body["pending_gate"]
    assert pending_rows(client) == []

    # 终态不可重复处置 -> 409
    r = resolve(client, vid, "writeoff")
    assert r.status_code == 409 and "终态" in r.json()["detail"]

    # 闸开后：他日先封（2026-11-15 现有流水 88 + 刚追加的 5 = 93）
    set_day(client, "2026-11-15")
    other_total = preview(client, "2026-11-15")["system_total"]
    out = seal_ok(client, "2026-11-15", other_total)
    assert out["disposition"] == "balanced"

    # 同源日已封不能重封；新营业日可正常封账
    r = seal_day(client, "2026-11-16", 1.0)
    assert r.status_code == 409 and "已封账" in r.json()["detail"]
    out = seal_ok(client, "2026-11-17", 0)
    assert out["status"] == "sealed" and out["line_count"] == 0


def test_writeoff_resolution_opens_gate(client):
    set_day(client, "2026-11-16")
    total = preview(client, "2026-11-16")["system_total"]
    seal_ok(client, "2026-11-16", round(total - 3.5, 2))
    vid = pending_rows(client)[0]["id"]
    assert gate(client)["gate"] == "blocked"

    r = resolve(client, vid, "writeoff")
    assert r.status_code == 200
    assert r.json()["variance"]["disposition"] == "writeoff"
    assert gate(client)["gate"] == "open"


def test_gate_reblocks_on_new_pending_after_resolution(client):
    # 清 pending 后若再封出 pending（新的未封日），闸必须重新 blocked
    set_day(client, "2026-11-16")
    total = preview(client, "2026-11-16")["system_total"]
    seal_ok(client, "2026-11-16", round(total + 2.0, 2))
    vid = pending_rows(client)[0]["id"]
    resolve(client, vid, "gain")
    assert gate(client)["gate"] == "open"

    set_day(client, "2026-11-15")
    other_total = preview(client, "2026-11-15")["system_total"]
    seal_ok(client, "2026-11-15", round(other_total - 7.0, 2))
    g = gate(client)
    assert g["gate"] == "blocked"
    assert g["pending_count"] == 1 and g["pending_total"] == -7.0

    # blocked 期间任何新封都进不来：不会出现「仍有 pending 却已新封成功」
    r = seal_day(client, "2026-11-17", 0)
    assert r.status_code == 409
