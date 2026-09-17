"""推理预算（`LLM_REASONING_EFFORT`）接线：决定"推理模型能不能用"的开关。

动因（2026-09-12 实测 glm-5.3 / TokenRouter）：
- 8k prompt 下**不给约束** → 15352/16384 token 花在 reasoning、正文截断、单次 504s；
- `max_tokens=4096` → reasoning 吃光全部预算，`content` 为**空字符串**
  → `_llm_model` 报"输出结构合法但内容不可用"，planner 整阶段失败；
- `thinking: {"type":"disabled"}` → **400**（"GLM-5.3 does not support disabling thinking"）；
- `reasoning_effort=none` → 端点**接受**该字段（小提示词下 reasoning 873→41）。

注意一个**反直觉的实测结论**（写进规格，避免后人重踩）：
`reasoning_effort` 只在**小**提示词上显著降低 reasoning；8k 提示词下它仍会把
16384 的预算全用在思考上（`reasoning=16384, len=0`）——即该参数**不能**让
重推理模型胜任本项目的大提示词调用，它只是"接线可用"。
"""
from __future__ import annotations

from app.config import Settings
from app.infrastructure.llm.router import _extra_body, provider_routing_body


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_no_extra_body_when_nothing_configured():
    # ``llm_reasoning_effort=""`` 必须**显式**传：``_env_file=None`` 只挡住了 .env 文件，
    # 挡不住 ``load_dotenv`` 已经写进 os.environ 的值（本项目 .env 配了 low）。
    # 不显式置空，本用例会读到环境值而误判成"注入了 extra_body"。
    assert _extra_body(_s(llm_base_url="https://api.tokenrouter.com/v1",
                          llm_reasoning_effort="")) is None


def test_reasoning_effort_is_injected():
    # 用非 OpenRouter 端点，隔离出 reasoning_effort 一个字段
    body = _extra_body(_s(llm_base_url="https://api.tokenrouter.com/v1",
                          llm_reasoning_effort="none"))
    assert body == {"reasoning_effort": "none"}


def test_reasoning_effort_is_normalized():
    """大小写/空白应被归一（`LOW` / ` low ` 都该发 `low`）。"""
    body = _extra_body(_s(llm_base_url="https://api.tokenrouter.com/v1",
                          llm_reasoning_effort="  LOW  "))
    assert body["reasoning_effort"] == "low"


def test_empty_reasoning_effort_not_injected():
    """留空 = 不注入该字段：非推理模型/自建端点不该收到未知参数。"""
    for val in ("", "   "):
        assert _extra_body(_s(llm_base_url="https://api.tokenrouter.com/v1",
                              llm_reasoning_effort=val)) is None


def test_provider_routing_only_for_openrouter():
    """`provider` 字段是 OpenRouter 专有——换端点后不得再注入（否则可能 400）。"""
    assert provider_routing_body(_s(llm_base_url="https://api.tokenrouter.com/v1")) is None
    assert provider_routing_body(_s(llm_base_url="https://openrouter.ai/api/v1")) is not None


def test_reasoning_effort_and_routing_coexist():
    """两个字段来源不同，必须能同时生效（不能一个把另一个挤掉）。"""
    body = _extra_body(_s(llm_base_url="https://openrouter.ai/api/v1",
                          llm_reasoning_effort="low"))
    assert body["reasoning_effort"] == "low"
    assert "provider" in body


def test_call_model_passes_extra_body_to_sdk():
    """接线断言：`_call_model` 必须把 `extra_body` 真的传给 SDK（否则配置是死的）。"""
    import inspect

    from app.infrastructure.llm.router import OpenAILLM

    src = inspect.getsource(OpenAILLM._call_model)
    assert 'kwargs["extra_body"] = extra_body' in src
