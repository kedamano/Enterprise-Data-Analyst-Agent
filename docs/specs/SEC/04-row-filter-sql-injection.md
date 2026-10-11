# SEC/04 行级过滤 SQL 注入防护 — 规格 v1.0（D70 定稿）

> 动因：`data_guard.py:68` 的 `apply_row_filters` 用字符串拼接注入 WHERE 子句，
> 在子查询/CTE/复杂 JOIN 场景下可能注入到错误位置，存在 SQL 注入风险。
> 现状：仅通过 `validate_filter_fragment` 验证过滤片段，但注入位置不安全。
> 核心判断：使用 `sqlglot` 解析 SQL 并安全地注入 WHERE 子句。

## 1. 契约（Contract）

### 参数契约

| 参数 | 必需 | 说明 |
|---|---|---|
| `sql` | 是 | 原始 SQL 查询 |
| `principal` | 是 | 当前用户身份 |

### 输出契约

```json
{
  "sql": "str",
  "applied_tables": "list[str]"
}
```

### 判定规则

1. 使用 `sqlglot` 解析 SQL 为 AST
2. 遍历 AST 找到所有表引用
3. 对有行级过滤的表，在正确的 SELECT 节点注入 WHERE 子句
4. 如果 SQL 无法解析，回退到原有字符串拼接方式（但记录警告）

### 边界与失败

| 情形 | 行为 |
|---|---|
| SQL 无法解析 | 回退到字符串拼接，记录警告 |
| 子查询/CTE | 在正确的 SELECT 节点注入 |
| 复杂 JOIN | 每个表独立注入 |

### 可观测

- 解析失败时记录警告日志

## 2. TDD（红→绿）

| 用例 | 断言 |
|---|---|
| 正路径：简单查询 | WHERE 正确注入 |
| 负路径：子查询 | 在正确的 SELECT 节点注入 |
| 负路径：CTE | 在正确的 SELECT 节点注入 |
| 兼容性：既有测试 | 全部通过 |

## 3. 验收（Acceptance）

| 项 | 判据 |
|---|---|
| 功能点 | 复杂 SQL 场景下 WHERE 注入位置正确 |
| 兼容性 | 既有测试全部通过 |
| 性能 | 无显著退化 |

## 4. 明确不做（Out of Scope）

- 不改变 `validate_filter_fragment` 的验证逻辑
- 不改变 `Principal` 模型
- 不引入新的依赖（如果 sqlglot 不可用则回退）
