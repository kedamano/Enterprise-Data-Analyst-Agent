# AUTH-02 · 空 `session_id` 不再共用一个桶

> 2026-09-15（D55）· 动因：D54 真实基线复盘的顺带发现（**已发现、未修**）

---

## 1. 断在哪

`sidecar_path()` 把空/`None` 的 session 归一成字面量 **`default`**
（`app/core/attachments.py:214`）：

```python
sid = re.sub(r"[^A-Za-z0-9_-]", "_", (session_id or "default"))[:60] or "default"
```

而**上传接口的默认值就是 `"default"`**（`app/api/routes/attachments.py:48`，
`session_id: str = Form(default="default")`），`chat.py:61` 则用 `req.session_id or ""`。

实测：`attached_tables(None)` 与 `attached_tables("")` 都返回同一批表，
`attached_tables("no_such_session")` 返回 `[]`。

→ **两个都不带 `session_id` 的调用方会互相看见对方上传的表**，
且这条路径**可达**（`""` 是合法请求值）。在 RBAC 部署下这是**跨调用方的数据可见性**问题。

**影响面有界但真实**：前端（`web/src/lib/api.ts:275`）**一直显式回传** `session_id`，
故只有**直接调 API**的调用方会走到这里。

---

## 2. 契约

### 2.1 在 API 边界把空 session 补齐，而不是在核心层归一

新增纯函数（放 `app/core/attachments.py`，与 `sidecar_path` 同层）：

```python
def resolve_session_id(session_id: str | None) -> str:
    """空/纯空白/非法 → 新生成 `s_<uuid4hex[:12]>`；否则返回原值（原样，不做清洗）。"""
```

**服务端生成、随响应返回**，客户端拿得到就能继续用：

| 端点 | 空 session 的行为 |
|---|---|
| `POST /attachments/upload` | 生成 `s_xxx` → 落到该桶；`UploadResponse.session_id` 返回它（**字段已存在，无 schema 变更**） |
| `GET /attachments/list` | 生成一个**空桶**的 id 并返回 `[]`（不返回别人的东西） |
| `DELETE /attachments/clear` | 同上：只清一个空桶 |
| `POST /chat/analyze`（含 SSE） | 生成 → `state.session_id` 即该 id，随响应返回 |

**为什么是"生成"而不是"拒绝"**：`session_id` 在语义上是**客户端自己声明的分组键**。
没声明 = "我没有要和别人共享的上下文"，那就给它一个**只有它知道**的键——
这与"缺 key → 拒绝"不同：拒绝会让一个**本来正确**的调用（不带 session 的单轮问答）
变成 400，而它想问的问题没有任何问题。**补齐把默认行为从"共享"改成"隔离"，
两个调用方都不会因此少拿到东西。**

### 2.2 生成值必须与用户提供的值**不可区分地隔离**

- 形如 `s_<12 hex>`：与 `sidecar_path` 的字符白名单（`[A-Za-z0-9_-]`）兼容，**不需要清洗**；
- **不校验用户传的值**（保持现状）：传 `"alice"` 就是 `alice` 的桶——
  桶的**所有权**由既有的 `record_session_owner`/`owns_session`（AUTH/01）管，
  本卡**不引入第二套鉴权**。

### 2.3 `sidecar_path` 的 `default` 兜底保留

它仍是**最后一道归一**（内部调用方可能传 `None`），但**从 API 边界起就不会再有空值到达**——
即：兜底不再是**可达路径**，而是一条防御性分支。为此补一条测试：
**从 API 上传（不带 session）不会写进 `default` 桶**。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| 生成格式 | `resolve_session_id(None)` / `("")` / `("   ")` 都匹配 `^s_[0-9a-f]{12}$` |
| 不变量 | 同一输入两次调用**得到不同的 id**（不是固定值） |
| 透传 | `resolve_session_id("alice") == "alice"`（**不做任何清洗/变形**） |
| 上传隔离 | 两次不带 session 的上传（不同文件）→ 各自 `attached_tables` **只看到自己那张** |
| 上传仍可用 | 带 session 的第二次上传能看见第一次（既有行为不变） |
| 列表隔离 | 不带 session 的 `GET /attachments/list` **不返回**别人上传的表 |
| 不写死桶 | 不带 session 上传后，`sidecar_path("default")` 指向的桶**仍为空** |
| 对话隔离 | 不带 session 的 `/chat/analyze` 拿到的 `session_id` 是 `s_xxx`（非 `""`/`default`） |
| 不回归 | 既有上传/附件用例（显式传 session）全绿 |

## 4. 明确不做

- **不改前端**（它已经每次显式传 id，无缺陷）。
- **不给附件加所有权鉴权**：桶的所有权是 AUTH/01 的 `owns_session` 的事，
  本卡只解决"两个陌生调用方被塞进同一个桶"。
- **不删 `default` 桶里的历史数据**（`data/uploads/default/`）：那是别人跑出来的产物，
  清理是运维动作，不是本卡的正确性前提。

---

## 5. 回填（2026-09-15 实施）

- **用例 15 条全绿**；全量离线回归 **1217 passed / 32 skipped / 0 failed**。
- **`chat.py` 的三处（`/analyze` 与 SSE 的 `_record_owner` / `stream_analysis`）也一并接线**：
  统一走 `_session_id_for(req)`，避免"三条路径里只改了两条"。
  生成值经 `state.session_id` 与 `AnalyzeResponse.session_id` / FINISH 帧回传。
- **一处有意不变**：`list` / `clear` 与 `upload` 同规则（空 → 生成空桶的 id 并回声），
  **不在空值上直接调 `store.get("")`** —— 空字符串会被 `AttachmentStore` 归一成 `default`，
  那正是本卡要断掉的路径。
- **一处代价（显式记录）**：不带 session 的调用方现在**每轮拿到新 id**，
  于是"上传时没带 session、分析时也没带"会**看不见自己刚传的表**。
  这是有意的——上传响应里就把 id 给了它。前端一直显式回传，不受影响。
- **未做**：给附件加所有权鉴权（那是 AUTH/01 的 `owns_session`）。
