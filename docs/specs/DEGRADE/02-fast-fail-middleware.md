# DEGRADE/02 中间件快速失败（Redis 连接超时） — 规格 v1.0

> 来源：D19 定位"测试为什么慢"时实测。
> 现象：`.env` 配了 `REDIS_URL=redis://localhost:6379/0` 但 Redis **没启动**，
> 每次 Redis 操作都要等满 OS 级 TCP 超时（Windows 本机 ~2s）→ **每个流水线节点 ~4s**
> （`run_context` 4.4s、每步 executor 4.4s、reporter 12.3s），本地跑一次全链 ~29s、全量回归 3.5 分钟。
>
> 这不是"降级不可见"（DEGRADE/01 已解决），而是**降级太慢**：
> README 声称中间件"可选、可降级"，但可降级的代价被拖成了每次数秒。

## 1. 目标
「配了但没起」的中间件应当**快速失败**并立刻走内存兜底，而不是把超时成本平摊到每个节点。

## 2. 契约

### 2.1 配置（`app/config.py`）
| 配置 | 默认 | 说明 |
|---|---|---|
| `redis_connect_timeout_s` | `0.2` | 建连超时（秒）。本地/同可用区足够；跨机房部署应调大 |
| `redis_socket_timeout_s` | `0.5` | 读写超时（秒） |

### 2.2 客户端工厂（单一收口）
- 新增 `app/infrastructure/cache/redis.py::get_client()` 统一构造：
  `redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=…, socket_timeout=…)`。
- `app/core/memory/short_term.py::_redis()` 改为**委托**该工厂（消除两处重复；此前两处各自 `from_url`，无超时）。
- 保持既有降级语义不变：`redis_url` 为空 → `None`；构造异常 → `None`；**操作失败 → 内存兜底，绝不打断主流程**。
- 不启用 `retry_on_timeout`（降级要快，不在超时上再退避重试）。

## 3. 边界
- 超时值**不影响正确性**：Redis 真在跑时，0.2s 建连对本地/同可用区足够；
  若部署在跨机房链路，运维需上调 `REDIS_CONNECT_TIMEOUT_S`（写进部署文档）。
- 只在**建连/读写**层加超时，不做"探活再决定"（探活本身也要超时，反而多一次往返）。
- Milvus 客户端存在同类问题（`MilvusClient(uri=…)` 无 timeout），
  但本环境未安装 `pymilvus`，无法验证其 `timeout=` 参数签名，**本次不动**，另行处理。

## 4. TDD
| 用例 | 断言 |
|---|---|
| 超时被真正传递 | `get_client()` 的 `connection_pool.connection_kwargs` 含配置的两个超时值 |
| 快速失败 | Redis 不可达时，一次 `short_term.get()` 耗时 < 1.0s（修复前 ~2s/次） |
| 降级不变 | 不可达时 `put` 后 `get` 仍能读回（内存兜底） |
| 空配置 | `redis_url=""` → `get_client() is None`，且不导入 redis |
