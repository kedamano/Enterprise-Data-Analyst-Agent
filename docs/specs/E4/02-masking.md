# E4/02 输出脱敏分级 — 规格 v1.0（D19 定稿，D21 实现）

> 痛点：企业数据里手机号/邮箱/身份证/姓名/地址一旦进 LLM 上下文，就等于**出境且不可撤回**。
> 目标：**默认开**的脱敏，把"能不能进上下文"变成一条可配置、可测、可审计的边界。

## 1. 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `mask_pii_enabled` | **true** | 总开关（默认开；关闭需显式配置，且审计留痕） |
| `mask_level` | `sample` | `none` / `sample` / `strict` |
| `mask_columns` | 内置模式表 | 列名模式：`phone/mobile/tel/email/mail/id_card/idcard/name/address/birth/身份证/手机/电话/邮箱/姓名/地址/生日` |

## 2. 分级语义

| 级别 | 行为 |
|---|---|
| `none` | 不脱敏（仅用于本地调试；启动时打 WARNING） |
| `sample`（默认） | **样本行里**敏感列值替换为掩码；`null_ratio`/`distinct` 等**统计量**照常计算（统计不泄值） |
| `strict` | 敏感列**完全不出现在上下文**：样本行里整列剔除；profile 输出里也不给 `distinct`（防止"1 个不同值"反推） |

## 3. 应用点（单一收口，避免漏网）
- **上下文侧**：所有工具结果在进入 LLM prompt 前统一过滤——`_to_result()` 产出 `rows` 后、
  写入 `state.tool_results` 的同一处（`sql_query` / `freeform` / `dataset_profile` / `python_analysis` 共用）。
- **落盘/回传侧**：`csv_path` 与 `artifacts` **不脱敏**（分析师本机产物需可用），但
  `GET /analyze/artifacts` 与报告渲染中的**行样本**按同级别处理。
- **审计**：每次脱敏记一条 `{session, tool, step_id, columns_masked[], level}` 进审计日志。

## 4. 掩码算法（确定性、可测）
- 保留可识别性最低的形式：`phone` → `138****1234`（留前 3 后 4）；`email` → `a***@example.com`；
  其它 → `***`（等长星号，避免长度泄漏）。
- 不做哈希：哈希在低基数下可反推（如性别/地区），且对模型无信息增益。

## 5. 边界
- 列名未命中模式表但值形如 PII（如 18 位身份证正则）→ `sample` 级别下**按值**兜底掩码，并计入审计。
- 数值列不掩码（除非显式列入 `mask_columns`）。
- 脱敏**不改变**工具返回的行数与列数语义（除 `strict` 剔除列外），避免下游断言错位。

## 6. TDD（D21）
- 正：`sql_query` 返回含手机号的行 → 进 LLM 的 prompt 中**不含**原始号码、含掩码串；统计量不变。
- 负：`mask_level=strict` → 敏感列既不在样本行、也无 `distinct`。
- 负：`mask_pii_enabled=false` → 原始值进上下文 + 审计留痕（该路径必须被测试显式固定，防止默认被悄悄关掉）。
- 审计：一次调用产生 1 条脱敏审计记录，含被掩列名。


---

## 7. 实现说明（v1.1 · E4/02 落地）

- 模块：`app/core/security/masking.py`（新建包）。**单一收口**在 `tools._to_result()`——
  所有工具（sql_query/freeform/dataset_profile/python_analysis…）共用一条路径，不会漏网。
- **顺序**：先按完整数据落盘 CSV（`full_rows`），再对"进上下文"的那份脱敏。
  两者不能混为一谈（S1 修 `_truncated` 哨兵时顺手把这两件事拆开了，本规格正好用上）。
- **审计**：`data/audit/masking.jsonl`，每条 `{ts, session_id, tool, step_id, level, columns_masked[]}`；
  **关闭脱敏（`level=none`）同样留痕**——"默认开"的边界不能被悄悄绕过。
- **失败即关闭**：脱敏自身异常时 `rows=[]` + `masking_error`，**绝不 fail-open** 放行原始数据
  （隐私控制与其它降级不同，不能"退化为可用"）。
- **模式表统一**：`semantics.is_pii_column` 委托到 `security.masking.is_sensitive_column`，
  两处规则不再漂移（SEMANTIC/01 里留的 TODO 已清）。
- **覆盖范围**：`rows`（行样本）、`enums`（维表枚举取值——E4/01/SEMANTIC 的新数据出口）、
  `columns[].distinct`（仅 strict，防"只有 1 个不同值"反推）。
- **未覆盖**：`csv_path`/`artifacts`（按规格**故意不脱敏**，分析师本机产物需可用）；
  `/analyze/artifacts` 返回的是产物**路径**而非行样本，无需处理。
- **留待真实环境**：敏感列模式表按行业/客户定制（`MASK_PII_COLUMNS` 可追加）；
  "值形态兜底"只认电话/邮箱/身份证/银行卡四类，其它证件类型标 `[待真实验证]`。
