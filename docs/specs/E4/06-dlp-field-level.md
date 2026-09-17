# E4/06 DLP 细粒度脱敏 — 规格 v1.0（D49 定稿）

> 前情：E4/02 的脱敏是**全局一档**——`mask_level` 一个值管所有人，且只约束"进 LLM 上下文"
> 的那一份。**导出物全有全无**：要么带原始值（`export_raw` 走 D45 HITL），要么不给。
>
> D49 补三件（都围绕**导出**这个数据出口）：
> 1. **按角色/字段级策略**：不同角色对同一列看到不同的脱敏级别；
> 2. **水印**：导出的每一份都带**可验证**的签发凭据（谁、何时、哪个会话）；
> 3. **导出审批流**：脱敏版是安全默认，原始版走审批（复用 D45 HITL）。

---

## 1. 角色/字段级策略（`dlp.py`）

### 1.1 配置 `DLP_POLICY`（JSON）
```jsonc
{
  "analyst":  {"default_level": "sample",
               "column_levels": {"bank_card": "strict", "salary": "strict"}},
  "intern":   {"default_level": "strict",
               "column_levels": {"phone": "strict"}},
  "data_owner": {"default_level": "none"}        // 数据负责人可见原始
}
```

### 1.2 解析优先级 `resolve_level(principal, column)`
从高到低，**先命中先返回**：
1. `Principal.denied_columns` 命中 → `strict`（**权威**：数据权限已声明不可见，
   高于任何角色策略——策略不应比权限模型宽松）；
2. 该角色 `column_levels` 命中列名（**最长 key 优先**，如 `customer_phone` 胜过 `phone`）；
3. 该角色 `default_level`；
4. 角色未在策略里 / `DLP_POLICY` 为空 → 回退全局 `mask_level`（**零影响**）。

`level` 只接受 `none | sample | strict`；未知值 → 回退全局 `mask_level`（不猜）。

### 1.3 按列脱敏
`mask_csv_text(text, resolve)` → 按每列独立级别处理：
- `none` → 原样；
- `sample` → 复用 `mask_rows` 的值掩码（电话/邮箱/身份证保留可识别性最低形式）；
- `strict` → **整列剔除**（连表头都不出现，与 E4/02 strict 同语义）。

---

## 2. 水印（`watermark.py`）

### 2.1 可验证 token
`issue_watermark(user_id, session_id, exported_at, secret)` → `v1.<base64(payload)>.<hex hmac>`：
- payload 含 `user_id / session_id / exported_at`；
- 签名 `HMAC-SHA256(secret, payload)`——**持有 secret 才签得出**，故可验证不可伪造。

`verify_watermark(token, secret)` → `dict`（解析出的 payload）或 `None`（签名不对 / 篡改 / 格式错）。

### 2.2 应用点
- zip 包内 `WATERMARK.txt`（人可读说明 + token）；
- 响应头 `X-Dlp-Watermark`（携带 token，便于接收方即时校验）。

### 2.3 纪律
- `DLP_WATERMARK_SECRET` 为空 → **不出水印**（默认零影响，与 D45"默认关"同范式）；
- secret 不进 token 本身、不落审计明文；
- 水印是**附加**，不改动 `report.md` / `queries.sql` / `data/*.csv` 的内容字节
  （避免破坏既有断言与可复现性）。

---

## 3. 导出审批流（`export.py`）

`GET /analyze/export/{session_id}?format=...&masked=1`：

| `masked` | 行为 | 审批 |
|---|---|---|
| `1`（**策略激活时的默认**） | 按调用方角色的 DLP 策略脱敏后导出 | **无需审批**（安全路径） |
| `0`（原始） | 导出原始值 | **需 HITL `export_raw`**（D45，428 + token） |

- **策略激活** = `DLP_POLICY` 非空；激活时 `masked` 缺省取 `1`，否则取 `0`（与现状一致）。
- 原始导出若被 HITL 拦，428 体里带 `howto` 指向 `POST /analyze/confirm`（沿用 D45）。
- 审批放行后**一次性**（`take_grant`，取走即失效）——确认的是"这一次"，不是永久授权。
- 水印无论 masked/raw 都发（策略激活 + secret 已配时）。

---

## 4. 边界与诚实纪律

- **权限高于策略**：`Principal.denied_columns` 永远 `strict`，角色策略不能把权限模型放宽。
- **未知/畸形一律回退全局**，不猜、不 fail-open；解析异常在 `resolve_level` 内捕获并记 warning。
- **默认零影响**：`DLP_POLICY` 空 + `DLP_WATERMARK_SECRET` 空 → 导出行为与 D48 收口时**一字不变**
  （既有导出用例、E2E、eval 全不感知）。
- **水印不改内容**：只新增文件与响应头，不动既有产物字节。
- **水印 secret 不进日志/审计明文**；token 本身可公开（不含 secret）。
- `[待真实验证]`：水印在**真实接收方**校验链路（邮件网关/文档平台）未验；策略的列名模式
  需按客户数据字典定制（`DLP_POLICY` 可追加）。

---

## 5. TDD（先红后绿）

`tests/test_dlp.py`：
- 策略：列级覆盖胜 default；最长 key 胜短 key；`deny_columns`→strict；
  `Principal.denied_columns` 权威（高于角色策略）；未知角色→回退全局；`DLP_POLICY` 空→回退全局；
  畸形配置不抛。
- CSV：三列分别 none/sample/strict → 原样/掩码/整列剔除（表头也不在）。
- 水印：签发→校验回解；篡改 payload→None；错 secret→None；坏格式→None；无 secret→不签发。
- 导出：策略激活 + `masked` 缺省→按策略脱敏、zip 内含 `WATERMARK.txt`、响应头带 token；
  `masked=0` + HITL 开→428 带 `howto`；`masked=0` + 已放行→200 原始；
  `DLP_POLICY` 空→`masked` 缺省=0 且无水印（零影响）。
