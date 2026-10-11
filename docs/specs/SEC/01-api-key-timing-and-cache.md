# SEC/01 API Key 时序攻击防护与 parse_keys 缓存 — 规格 v1.0（D70 定稿）

> 动因：`auth.py:131` 的 `resolve_key` 用 `==` 比较 API key，存在时序攻击风险；
> `auth.py:92` 的 `parse_keys` 在每次认证检查时都重新解析 JSON 字符串，高并发下是性能瓶颈。
> 现状：每次请求都重新解析 + 线性扫描 + 非恒定时间比较。
> 核心判断：用 `hmac.compare_digest` 防时序攻击，用 `lru_cache` 缓存解析结果。

## 1. 契约（Contract）

### 参数契约

| 参数 | 必需 | 说明 |
|---|---|---|
| `api_key` | 是 | 客户端提供的 API key |
| `auth_keys` | 是 | 配置中的 JSON 字符串 |

### 输出契约

```json
{
  "principal": "Principal | null",
  "cache_hit": "bool"
}
```

### 判定规则

1. `resolve_key` 使用 `hmac.compare_digest` 进行恒定时间比较
2. `parse_keys` 结果被缓存，相同 `auth_keys` 字符串不重复解析
3. 缓存有上限（最多 16 个条目），LRU 淘汰
4. 未知 key 返回 `None`，不回显差异

### 边界与失败

| 情形 | 行为 |
|---|---|
| `auth_keys` 为空 | 返回空列表，不缓存 |
| `auth_keys` 非法 JSON | 抛 `ValueError`，不缓存 |
| 缓存满 | LRU 淘汰最旧条目 |

### 可观测

- 缓存命中率指标（可选）

## 2. TDD（红→绿）

| 用例 | 断言 |
|---|---|
| 正路径：正确 key | 返回对应 Principal |
| 负路径：错误 key | 返回 None |
| 负路径：空 key | 返回 None |
| 缓存命中：相同 auth_keys 第二次调用 | 不重新解析（通过 mock 验证） |
| 时序安全：比较使用 hmac.compare_digest | 通过 mock 验证调用 |

## 3. 验收（Acceptance）

| 项 | 判据 |
|---|---|
| 功能点 | `resolve_key` 使用恒定时间比较 |
| 性能 | `parse_keys` 结果被缓存 |
| 兼容性 | 既有测试全部通过 |

## 4. 明确不做（Out of Scope）

- 不改变 `Principal` 模型
- 不改变 `AUTH_KEYS` 配置格式
- 不引入外部缓存（Redis 等）
