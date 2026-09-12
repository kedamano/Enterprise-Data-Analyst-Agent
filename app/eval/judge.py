"""LLM-as-judge：评判「答案对不对」，而不只是「字段在不在」。

现状缺口（见 docs/对标企业级Gap.md §七）：`app/eval` 的全部断言都是**结构性**的
（`must_find` 子串命中、`expected_tools` 过程断言、E1 溯源），无法评判语义层面的
「答案是否正确 / 忠实 / 完整」。

本模块补这一环：
- `judge_case()` 返回 0~1 的 `score` + `rationale`。
- **有 LLM key 且开启 `JUDGE_USE_LLM=1`** → 走 OpenRouter（OpenAI 兼容）语义评分，
  真正评判「对不对」。
- **离线 / 无 key** → 走确定性 rubric 兜底（结构性代理指标），保证测试可离线跑、
  不依赖网络；但这只是代理，不能替代真 judge（显式标注 `method="rubric-offline"`）。

铁律：离线 rubric 永远可用、永远不抛异常；LLM 路径失败自动降级到 rubric，绝不假绿。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..config import get_settings
from .golden import GoldenCase


@dataclass
class JudgeResult:
    score: float            # 0.0 ~ 1.0
    method: str             # "llm" | "rubric-offline"
    rationale: str


_NUM_RE = re.compile(r"\d[\d,\.]*\s*%?")
_REFUSAL_RE = re.compile(r"(无法确保|不能保证|拒绝|不建议|缺乏数据|数据不足|样本量|请注意.*局限)", re.IGNORECASE)
_LIMIT_RE = re.compile(r"(局限|限制|前提|假设|仅供参考|口径|说明：|注意：)", re.IGNORECASE)
_HEADING_RE = re.compile(r"(^|\n)#{1,6}\s|^\s*[-*]\s", re.MULTILINE)


def _build_llm_prompt(case: GoldenCase, report_text: str, findings_text: str) -> str:
    return (
        "你是严格的数据分析评测裁判。请评判下面这份分析回答对给定问题的"
        "「正确性、忠实度、完整性、口径严谨度」。\n\n"
        f"【用户问题】\n{case.query}\n\n"
        f"【模型回答】\n{report_text}\n\n"
        f"【关键发现】\n{findings_text}\n\n"
        "请给出 0.0~1.0 的分值（1.0=完全正确且严谨，0.0=答非所问或严重错误），"
        "并一句话说明理由。\n严格按 JSON 输出："
        '{"score": <float>, "rationale": "<string>"}'
    )


def _parse_judge_json(content: str) -> tuple[float, str]:
    """从模型输出里稳健抽取 ``{"score": float, "rationale": str}``。

    免费推理模型常在 JSON 前后夹带 ``<think>...</think>``、markdown 代码围栏、
    或把 JSON 塞进自然语言里——直接 ``json.loads`` 会失败。这里：
    1) 剥 ``<think>`` 标签与 ```json 围栏；2) 用括号配对切出第一个合法 JSON 对象；
    3) 取 score / rationale。完全无法解析时抛 ``ValueError``，由调用方降级 rubric。
    """
    import json as _json

    text = content or ""
    # 1) 剥推理模型的 <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 2) 剥 markdown 代码围栏
    text = re.sub(r"```(?:json)?\s*", "", text).strip().strip("`").strip()
    # 3) 试整段直解
    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            return float(data.get("score", 0.0)), str(data.get("rationale", ""))
    except _json.JSONDecodeError:
        pass
    # 4) 括号配对切出第一个平衡 JSON 对象
    depth = 0
    start = -1
    in_str = False
    esc = False
    candidate = None
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start:i + 1]
                    break
    if candidate:
        try:
            data = _json.loads(candidate)
            if isinstance(data, dict):
                return float(data.get("score", 0.0)), str(data.get("rationale", ""))
        except _json.JSONDecodeError:
            pass
    raise ValueError(f"无法从 judge 输出解析 JSON：{content[:120]!r}")


def judge_with_llm(case: GoldenCase, report_text: str, findings_text: str) -> JudgeResult:
    """走 app 已验证的 LLM 网关（OpenRouter 兼容端点）做语义评分。

    复用 ``OpenAILLM._call_model`` —— 该路径已在主编排链路中跑通（含
    ``response_format=json_object`` 与重试/熔断），避免 judge 另起一套脆弱的
    裸 ``OpenAI()`` 调用导致 ``choices`` 为空。失败**抛异常**由 ``judge_case``
    降级到 rubric，绝不假绿。
    """
    st = get_settings()
    from ..infrastructure.llm.router import OpenAILLM

    llm = OpenAILLM(st)
    model = os.getenv("JUDGE_MODEL") or st.llm_model
    content = llm._call_model(
        model=model,
        system="你是严格的数据分析评测裁判，只输出 JSON。",
        content=_build_llm_prompt(case, report_text, findings_text),
        stage="judge",
        json_mode=True,
        temperature=0.0,
    )
    score, rationale = _parse_judge_json(content)
    return JudgeResult(score=max(0.0, min(1.0, score)),
                      method="llm",
                      rationale=str(rationale)[:300])


def judge_offline_rubric(case: GoldenCase, report_text: str, findings_text: str) -> JudgeResult:
    """确定性 rubric：结构性代理指标。离线可跑、可微分（judge_min_score 可卡阈值）。

    注意：这是「结构质量」代理，不是「语义正确性」。真语义 judge 需 LLM。
    """
    text = f"{report_text}\n{findings_text}"
    low = text.lower()
    score = 0.0
    notes: list[str] = []

    # 1) 关键片段覆盖（must_find）：每命中一个 +0.5/总数
    if case.must_find:
        hits = sum(1 for t in case.must_find if t.lower() in low)
        cov = hits / len(case.must_find)
        score += 0.5 * cov
        notes.append(f"must_find 覆盖 {hits}/{len(case.must_find)}")
    else:
        score += 0.5  # 无关键片段要求时给满分该项
        notes.append("无 must_find 要求")

    # 2) 数值证据：报告含数字/百分比 → 分析有数据支撑
    if _NUM_RE.search(text):
        score += 0.15
        notes.append("含数值证据")
    else:
        notes.append("缺数值证据")

    # 3) 口径/局限声明：must_have_limitations 时必须满足
    has_limit = bool(_LIMIT_RE.search(text))
    if case.must_have_limitations:
        if has_limit:
            score += 0.15
            notes.append("含局限/口径声明")
        else:
            notes.append("应含局限声明但缺失")
    else:
        score += 0.1 if has_limit else 0.05
        notes.append("局限声明 +0.1" if has_limit else "无局限声明 +0.05")

    # 4) 拒答纪律：expect_refusal 时报告需有拒答/谨慎措辞
    if case.expect_refusal:
        if _REFUSAL_RE.search(text):
            score += 0.1
            notes.append("含拒答/谨慎措辞")
        else:
            notes.append("应拒答但无谨慎措辞")
    else:
        score += 0.1
        notes.append("非拒答场景 +0.1")

    # 5) 结构：有 markdown 标题/列表
    if _HEADING_RE.search(report_text):
        score += 0.1
        notes.append("结构良好(md)")
    else:
        notes.append("结构偏弱")

    score = max(0.0, min(1.0, score))
    return JudgeResult(score=round(score, 3), method="rubric-offline",
                      rationale="; ".join(notes))


def judge_case(case: GoldenCase, report_text: str, findings_text: str) -> JudgeResult:
    """统一入口：可用 LLM 则语义评分，否则确定性 rubric 兜底。

    `JUDGE_USE_LLM=1` 且配置了 `LLM_API_KEY` 才走 LLM；任何异常都降级到 rubric，
    绝不因 judge 失败而让评测整体崩溃或假绿。
    """
    use_llm = os.getenv("JUDGE_USE_LLM") == "1" and bool(get_settings().llm_api_key)
    if use_llm:
        try:
            return judge_with_llm(case, report_text, findings_text)
        except Exception as exc:  # 降级而非崩溃
            rb = judge_offline_rubric(case, report_text, findings_text)
            return JudgeResult(score=rb.score, method="rubric-offline",
                              rationale=f"LLM judge 不可用({type(exc).__name__})，降级 rubric：{rb.rationale}")
    return judge_offline_rubric(case, report_text, findings_text)
