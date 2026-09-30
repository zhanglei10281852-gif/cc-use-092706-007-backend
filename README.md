# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 飞羽公益观鸟导赏（birding）

`app/birding` 提供活动场次、报名、确认期限与候补队列的完整流转：

- **场次管理**：`POST /api/birding/events`（需 `birding.write`）、`GET /api/birding/events`、
  `GET /api/birding/events/{id}`、`GET /api/birding/events/{id}/roster`（需 `birding.read`）、
  `POST /api/birding/events/{id}/close`。关闭后保留已确认参与者，未确认/候补报名清退为 `rejected`。
- **报名流转**（公开接口，按 `applicant` 归一身份）：
  `POST /api/birding/events/{id}/registrations` → 有名额为 `offered`（带 `offer_expires_at` 确认期限），
  满员进入 `registered` 候补；`POST /api/birding/registrations/{id}/confirm`、`.../cancel`。
  报名人通过 `?applicant=` 证明身份，工作人员凭 Bearer 令牌代操作；两者之外一律 403。
- **幂等**：同一 `idempotency_key` 重放返回首次响应（同键不同体 409）；同一身份的并发重复请求
  只返回同一报名单（`duplicate=true`），绝不重复占位。
- **候补转正**：`offered` 取消释放名额，或 `POST /api/birding/sweep/expired` 回收超时未确认名额时，
  按 `queued_at` 原始顺序只提升状态为 `registered` 的候补；被工作人员暂停资格（`blocked`）的人
  保留排队位置但不晋升，恢复后（`unblock`）按原位置参与。
- **通知重试**：转正产生唯一的 `offer` 通知；`POST /api/birding/notifications/dispatch` 外呼失败仅退避重试
  （30/60/120/300/600 秒），名额释放后未发送通知立即作废，人工重试
  `POST /api/birding/notifications/{id}/retry` 不新建记录、不重复占位。
- **审计还原**：每次状态迁移写入 `birding_status_history`（from/to/座位号/原因/操作者），
  同时写入全局 `audit_events`（before/after 快照），可按资源类型检索并完整回放。

```bash
python -m app.cli birding-demo   # 建场→报名→取消→候补转正 的接口级演示
python -m pytest tests/test_birding.py -v
```

测试通过 HTTP 接口模拟 18 人并发报名、确认超时释放、候补跳过 blocked 转正、活动关闭、
通知失败重试与权限隔离。
