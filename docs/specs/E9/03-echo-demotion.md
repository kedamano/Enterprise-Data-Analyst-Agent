# E9/03 — query-echo 检测与置信降级（字面重合同语义误判）

> 状态：DRAFT → DOING（D62）  
> 对标：`docs/对标企业级Gap.md` §四「知识」行残余项「字面重合同语义误判」  

---

## 一、断在哪

E9/01（多跳检索）和 E9/02（query 改写）解决"查不到"。  
D62 要解决另一种失败：**查到了，但查到的那段话就是问题本身**。

### 典型负例

```
query  = "华东区域的年会在哪里办"
chunk  = "华东区域的年会在哪里办"          ← KB 里就住着这道 FAQ 的"问"
#      或 "用户问：华东区域的年会在哪里办？"
#      或 "华东区域的年会在哪里办（待回复）"
```

短语亲和 = 1.0（chunk token 集 == query token 集）→ 按 D53 判 `high`。  
但这段话**没有语义内容**——它只是把问题复述了一遍，模型拿走去当知识，等于拿问题答问题。

> 字面重合 ≠ 语义相关。D53 的短语亲和只看"沾不沾边"，D62 再看一句"沾的到底是答案还是问题自己"。

> 扩展类比与 D53 同根同源，但 D53 只管"沾不沾边 + fail-closed"。D62 的 echo 检测
> 是"沾的是本体还是像（the substance vs its reflection）"——同一类失败（字面重合的
> 假阳性），更进一层的识别。

---

## 二、判据

### 基本原理：不包含实质性新信息的"问答同文"

```python
_QUESTION_END = re.compile(r"[吗呢？?\s]+$")

def _clean(s: str) -> str:
    return _QUESTION_END.sub("", s).strip()

def _is_echo_chunk(query: str, text: str, *, ratio_max: float, min_chars: int) -> bool:
    q, t = _clean(query), _clean(text)
    if len(q) < min_chars:          # 极短 query 退避（避免误伤）
        return False
    if not q or not t:
        return False
    shorter, longer = (q, t) if len(q) <= len(t) else (t, q)
    if shorter not in longer:
        return False                # chunk 并不以 query 为骨干
    return len(longer) <= ratio_max * len(shorter)   # chunk 没有多出实质信息
```

直觉：

1. **查询本身是 chunk 的骨干**（clean_query 是 clean_chunk 的子串）← 不是"沾边"，是"整个query完整出现在chunk里"。
2. **chunk 没有多出实质信息**（`len(chunk) <= ratio_max × len(query)`）← 回答了"尺寸上是答案还是回声"。  
   echo 的定义就是放大率 ≤ ratio_max，**ratio_max 就是 demotion 判据的刚性参数**。

### 两个可控旋钮（`app/config.py`）

| 字段 | 默认 | 含义 |
|---|---|---|
| `rag_echo_demotion_enabled` | `True` | 开关 |
| `rag_echo_ratio_max` | `1.5` | chunk 长度 ≤ 1.5× query 才判 echo |
| `rag_echo_min_chars` | `8` | query 至少这么长才进场（极短 query 避误伤） |

实测校准依据：

- echo case `"华东区域的年会在哪里办"` vs `"用户问：华东区域的年会在哪里办？"`：  
  clean 后 shorter=`华东区域的年会在哪里办`(10)，longer=`用户问：华东区域的年会在哪里办`(14) → ratio=1.4 ≤ 1.5 → echo ✓
- 真实答案 `"华东区域年会安排在上海国际会议中心，时间是 2026-03-15。"`：  
  clean_query 不是 clean_chunk 的子串（`"华东区域的年会在哪里办"` vs `"华东区域年会安排在上海…"`，第 4 个字符 `"的"≠"年"`) → 不触发 echo → 保留 `high` ✓  
  E9/03 不会误伤正确答案。

---

## 三、在 `confidence_of`中的接线

```python
def confidence_of(query, chunks):
    if not chunks:                    → Confidence("none", 0.0, "no_chunks")
    text = chunks[0].get("text")      → strip; 空  → Confidence("low", 0.0, "no_signal")
    if _is_echo_chunk(query, text):   → Confidence("low", phrase_affinity, "echo_question")
    score = phrase_affinity(query,text)
    return high/low by score vs threshold
```

- 只管 `chunks[0]`（D53 的同一条纪律：首条就不相关，后面的更不相关）。
- `basis` 用 `echo_question`（区别于 `phrase_affinity`/`no_signal`/`no_chunks`）。
- 降级后的 score 仍保留实际亲和分数，便于排查——这与 D53 "分数本身不该被阈值改写"
  的哲学一致。

---

## 四、Metric

`app/core/tools/knowledge_tool.py` 在消费 `confidence_of` 结果时：

- 当 `basis == "echo_question"`：额外增加 `rag_echo_demotion_total`（单独计数，不与
  `rag_low_confidence_total` 合并，避免两套失败定义被同一条曲线遮住）。
- 主 low-confidence 路径（清空 chunks、`low_confidence=True`、`note`）照常执行——
  echo 降级是 `low` 的子集，降级方式与 D53 零差别。

`/metrics` 渲染块追加 `rag_echo_demotion_total`（counter，type echo）。

---

## 五、测试矩阵

### 5.1 单元：`tests/test_e9_echo.py`

| ID | 用例 | 期望 |
|---|---|---|
| T1 | `confidence_of("华东区域的年会在哪里办",  [{"text":"用户问：华东区域的年会在哪里办？", ...}])` | level=`low`, basis=`echo_question` |
| T2 | query 是 chunk 的精确子串，ratio ≤ 1.5 → echo | `low` |
| T3 | chunk 以 query 为骨干但 ratio > 1.5（真实答案很长） | 保留 `high`（不误伤） |
| T4 | clean_query 不是 clean_chunk 子串的正常正例（营收口径） | `high`（echo 检测不影响既有正例） |
| T5 | 极短 query（长度 < echo_min_chars）不触发 echo（避免误伤短问） | `high`（按亲和分正常判） |
| T6 | 脏文本（前后空白 + 引号包围）仍能 echo 检测 | `low` |
| T7 | `rag_echo_demotion_enabled=false` 关闭 echo → 退化回 D53（亲和高 = `high`） | `high`, basis=`phrase_affinity` |
| T8 | echo 降级后 chunks 清空 + low_confidence=True + note | 结构性保证 |

### 5.2 升级 D53 黄金集

在 `tests/test_rag_confidence.py` 追加一条"echo 负例"：

```python
ECHO_QUERY = "华东区域的年会在哪里办"
ECHO_TEXT  = "用户问：华东区域的年会在哪里办？"   ← 就是问题，没答案

def test_echo_chunk_is_demoted_to_low(seeded_store):
    conf = confidence_of(ECHO_QUERY, [{"id":1,"source":"faq.md","text":ECHO_TEXT}])
    assert conf.level == "low"            # 亲和分虽 ≈1.0 但必须降级
    assert conf.basis == "echo_question"
```

golden 正例不动，echo 新增一条，两侧标定外加一侧 echo。

---

## 六、DoD

- [ ] `_is_echo_chunk` 在 `confidence_of` 中短路，返回 `basis="echo_question"` 的 `low`；
- [ ] E9/03 的 8 条用例全绿；
- [ ] D53 黄金集（含 echo 追加）全绿，正例零误伤；
- [ ] 配置旋钮 `rag_echo_ratio_max` / `echo_min_chars` / `rag_echo_demotion_enabled`
      可从 `Settings` 走，不抛不崩；
- [ ] `rag_echo_demotion_total` 进入 `/metrics`；
- [ ] `app/eval` mock pipeline echo 端到端通过；
- [ ] DailyLog Day 62 记录；`docs/对标企业级Gap.md` 残余项「字面重合同语义误判」标 ✅ D62。

---

## 七、SDD 边界（明确不做什么）

- 不做"问答意图识别"／"FAQ 抽取"——echo 检测不需要知道 query 是不是问句。
- 不做"多 chunk 语义聚合"——只管首条，与 D53 纪律一致。
- 不做"跨 chunk 问答对匹配"——那是跨 chunk 推理，本项目明确不做。

## 八、LLM-mode 说明

本模块不依赖 LLM——判定纯字符串/正则/数值比较，绝对确定性。确定性规则
是"可以被代码审查直接审计"的：查错不需要重看一组生成结果，这是 E9/03 选
择规则化甚至比 E9/02 query 改写更彻底的原因。
