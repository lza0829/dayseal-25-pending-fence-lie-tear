# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 未决长短款闸（全局互斥）

封账时实盘与系统合计不一致即产生一笔 `disposition=pending` 的长短款。闸策略只贯彻一种：

- **拦一切新封**：存在任意 pending 时，对任何营业日（同源日/他日/无流水日）执行 `sealed` 封账一律 `409`，必须先把 pending 清为终态（`loss` 认损 / `gain` 认溢 / `writeoff` 核销；零差额自动记 `balanced`）。
- 清 pending 的写入口（`POST /api/close/variances/{id}/resolve`）、封账闸、封账预览、总览计数共用同一数据源（`variance_rows` 上的 `pending_summary` 单查询），同一写事务内判定，不会出现「页面已清仍拒封」或「仍有 pending 却新封成功」。
- 闸只拦新封账；未封日流水的增改删仍按原封账锁定规则执行，不扩大语义。
- 闸状态可核：`GET /api/close/gate` 返回 `{pending_count, pending_total, gate: open|blocked, blocked}`；总览与封账台暴露同样的未决笔数、未决合计与闸状态，清 pending 的响应直接回传翻转后的闸状态。
- 终态不可重复处置（再写返回 `409`），非法终态返回 `400`。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```

## 测试

```bash
cd backend
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -v
```

用例覆盖：无 pending 基线、balanced 不触发闸、pending 拦截同源日与他日、多笔未决合计、三种终态清账、终态重复处置拒绝、清后放行再封、清后再产生 pending 重新拦截、旧库结构迁移。

`tests/test_pending_coherence.py` 为 pending 事实 / 封账闸 / 总览计数三处同源的互证夹具：

- 「有 pending 仍封」：同源日、他日、无流水日新封全部 409，被拒不产生任何 sealed 批；
- 「已清仍拒」：清 pending 后此前被拦的营业日立即放行并真实落 sealed；
- 「计数≠行」：`/close/gate`、总览、封账预览的未决笔数/合计与 `variance_rows` 表扫描逐值相等，直插表的历史 pending 同样被识别（表扫描即权威，无第二缓存）；
- 「清回执行未改」：resolve 返回 success 时行态必为终态，终态重复处置 409 且行态不被二次改写；
- 并发：清 pending 与封账交错、双 resolve 抢同一行、多笔 pending 并发清账与抢封，全部经 `BEGIN IMMEDIATE` + `busy_timeout` 串行化，只允许自洽结局，无半截成功。

