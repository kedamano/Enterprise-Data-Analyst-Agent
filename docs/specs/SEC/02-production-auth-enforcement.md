# SEC/02 生产环境鉴权强制 — 规格 v1.0（D70 定稿）

> 动因：`auth_enabled` 默认 `False`，生产环境部署时容易忘记开启，导致 API 对外零鉴权。
> 现状：已有启动告警（`main.py:55`），但仅记录日志，不阻止启动。
> 核心判断：添加 `auth_required_in_production` 配置项，生产环境下未开鉴权则**拒绝启动**。

## 1. 契约（Contract）

### 参数契约

| 参数 | 必需 | 说明 |
|---|---|---|
| `auth_enabled` | 是 | 是否启用 API Key 鉴权 |
| `auth_required_in_production` | 是 | 生产环境是否强制要求鉴权（默认 True） |
| `environment` | 是 | 运行环境 |

### 输出契约

```json
{
  "startup_allowed": "bool",
  "error_message": "str | null"
}
```

### 判定规则

1. `environment` 为 `production`/`prod` 且 `auth_enabled=False` 且 `auth_required_in_production=True` → 拒绝启动
2. 其他环境（development/dev/local/test）→ 允许启动，保持现有行为
3. `auth_enabled=True` → 允许启动

### 边界与失败

| 情形 | 行为 |
|---|---|
| 生产 + 未开鉴权 + 强制开启 | 抛 `RuntimeError`，阻止启动 |
| 生产 + 未开鉴权 + 强制关闭 | 允许启动，记录 CRITICAL 日志 |
| 开发 + 未开鉴权 | 允许启动，无告警 |

### 可观测

- 启动时记录安全决策日志

## 2. TDD（红→绿）

| 用例 | 断言 |
|---|---|
| 正路径：生产 + 鉴权开启 | 允许启动 |
| 负路径：生产 + 未开鉴权 + 强制 | 抛 RuntimeError |
| 负路径：生产 + 未开鉴权 + 不强制 | 允许启动 |
| 兼容性：开发 + 未开鉴权 | 允许启动 |

## 3. 验收（Acceptance）

| 项 | 判据 |
|---|---|
| 功能点 | 生产环境强制鉴权可配置 |
| 兼容性 | 开发/测试环境行为不变 |
| 既有测试 | 全部通过 |

## 4. 明确不做（Out of Scope）

- 不改变 `auth_enabled` 默认值（保持 False，开发友好）
- 不改变 `AUTH_KEYS` 配置格式
- 不添加交互式确认
