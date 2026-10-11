# ARCH/06 SQLAlchemy Engine 缓存 — 规格 v1.0（D70 定稿）

> 动因：`sql_tool.py:137` 的 `_build_engine` 在每次查询时都创建新的 SQLAlchemy engine，
> engine 创建开销大（连接池初始化、方言设置），高并发下是性能瓶颈。
> 现状：每次 `sql_query` 调用都新建 engine + connection。
> 核心判断：按 DSN 缓存 engine，复用连接池。

## 1. 契约（Contract）

### 参数契约

| 参数 | 必需 | 说明 |
|---|---|---|
| `dsn` | 是 | 数据库连接字符串 |

### 输出契约

```json
{
  "engine": "sqlalchemy.Engine",
  "cache_hit": "bool"
}
```

### 判定规则

1. 相同 DSN 的 engine 被缓存
2. 缓存有上限（最多 8 个 engine），LRU 淘汰
3. engine 创建失败时不缓存
4. 线程安全（使用锁保护缓存）

### 边界与失败

| 情形 | 行为 |
|---|---|
| 缓存满 | LRU 淘汰最旧 engine |
| engine 创建失败 | 抛异常，不缓存 |
| DSN 变化 | 创建新 engine |

### 可观测

- 缓存命中率指标（可选）

## 2. TDD（红→绿）

| 用例 | 断言 |
|---|---|
| 正路径：相同 DSN 第二次调用 | 返回相同 engine 实例 |
| 负路径：不同 DSN | 返回不同 engine 实例 |
| 兼容性：既有测试 | 全部通过 |

## 3. 验收（Acceptance）

| 项 | 判据 |
|---|---|
| 功能点 | engine 按 DSN 缓存 |
| 性能 | 重复查询不新建 engine |
| 兼容性 | 既有测试全部通过 |

## 4. 明确不做（Out of Scope）

- 不改变 `sql_query` 的 API
- 不改变连接池配置
- 不引入外部缓存
