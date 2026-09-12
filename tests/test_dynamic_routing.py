"""P2-3 动态路由：OpenRouter provider 排序 + 免费模型回退链 + 限流感知退避。

背景：`google/gemma-4-31b-it:free` 在 OpenRouter 免费共享池被上游 429，
整链路在第一个 context 调用就挂。修复分三层：

1. **provider 路由**：往请求体注入 ``provider={"sort":"throughput", ...}``，
   让 OpenRouter 自动挑当前不拥堵的 provider（仅 openrouter 端点注入）。
2. **模型回退链**：LLM_FALLBACK_MODELS 里第二个免费模型在第一个被限流时顶上。
3. **限流退避**：429/503 尊重 Retry-After，退避下限抬高，而不是 1.5s 猛冲。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.infrastructure.llm import router as R


# --------------------------------------------------------------------------- #
# provider 路由参数
# --------------------------------------------------------------------------- #
def _settings(**kw: Any) -> Settings:
    base = dict(
        llm_base_url="https://openrouter.ai/api/v1",
        llm_model="google/gemma-4-31b-it:free",
        llm_api_key="sk-or-test",
        mock_llm=False,
    )
    base.update(kw)
    return Settings(**base)


def test_provider_routing_injected_for_openrouter():
    body = R.provider_routing_body(_settings())
    assert body == {"allow_fallbacks": True, "sort": "throughput"}


def test_provider_routing_skipped_for_non_openrouter():
    """官方/自建端点不认识 provider 字段 → 绝不能注入，否则 400。"""
    assert R.provider_routing_body(_settings(llm_base_url="https://api.openai.com/v1")) is None
    assert R.provider_routing_body(_settings(llm_base_url="http://localhost:8000/v1")) is None


def test_provider_routing_only_ignore_lists():
    body = R.provider_routing_body(
        _settings(llm_provider_only=["Google AI Studio"], llm_provider_ignore=["DeepInfra"])
    )
    assert body["only"] == ["Google AI Studio"]
    assert body["ignore"] == ["DeepInfra"]


def test_provider_routing_invalid_sort_ignored():
    body = R.provider_routing_body(_settings(provider_sort="nonsense"))
    assert "sort" not in body
    assert body["allow_fallbacks"] is True


def test_extra_body_shape():
    assert R._extra_body(_settings()) == {"provider": {"allow_fallbacks": True, "sort": "throughput"}}
    assert R._extra_body(_settings(llm_base_url="https://api.deepseek.com/v1")) is None


# --------------------------------------------------------------------------- #
# 模型回退链
# --------------------------------------------------------------------------- #
def test_model_chain_dedup_and_order():
    llm = R.OpenAILLM(_settings(llm_fallback_models=["google/gemma-4-26b-a4b-it:free",
                                                     "nvidia/nemotron-nano-12b-v2-vl:free"]))
    chain = llm._model_chain("google/gemma-4-31b-it:free")
    assert chain == [
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-nano-12b-v2-vl:free",
    ]


def test_model_chain_skips_primary_duplicate():
    llm = R.OpenAILLM(_settings(llm_fallback_models=["google/gemma-4-31b-it:free", "x/y"]))
    assert llm._model_chain("google/gemma-4-31b-it:free") == ["google/gemma-4-31b-it:free", "x/y"]


def test_model_chain_accepts_dict_form():
    llm = R.OpenAILLM(_settings(llm_fallback_models=[{"model": "a/b"}, {"model": "c/d"}]))
    assert llm._model_chain("p/q") == ["p/q", "a/b", "c/d"]


def test_fallback_chain_switches_on_first_failure(monkeypatch):
    """主模型抛 429 → 自动换第二个模型并成功返回；不再一路重试到死。"""
    llm = R.OpenAILLM(_settings(llm_fallback_models=["backup/model:free"]))
    calls: list[str] = []

    class Boom(Exception):
        status_code = 429

    def fake_call_model(self, *, model, **kw):
        calls.append(model)
        if model == self._settings.llm_model:
            raise Boom("429 upstream shared pool")
        return "OK-FROM-BACKUP"

    monkeypatch.setattr(R.OpenAILLM, "_call_model", fake_call_model, raising=True)
    out = llm.complete("sys", "user", stage="context")
    assert out == "OK-FROM-BACKUP"
    assert calls == ["google/gemma-4-31b-it:free", "backup/model:free"]


# --------------------------------------------------------------------------- #
# 下架模型的永久跳过（免费池 slug 随时消失）
# --------------------------------------------------------------------------- #
class _NotFound(Exception):
    status_code = 404


def test_is_model_gone_detects_404_and_messages():
    assert R._is_model_gone(_NotFound("x"))
    assert R._is_model_gone(RuntimeError("No endpoints found for a/b:free"))
    assert R._is_model_gone(
        RuntimeError("This model is unavailable for free. The paid version is available now"))
    assert not R._is_model_gone(RuntimeError("429 upstream shared pool"))


def test_gone_model_is_skipped_and_remembered(monkeypatch):
    """真实场景：回退链末尾是已下架的 minimax/minimax-m3:free（404）。

    第一次撞到 404 后应把它记入已知下架集合，并在**后续调用**里直接跳过，
    不再浪费一次网络往返。
    """
    llm = R.OpenAILLM(_settings(
        llm_fallback_models=["dead/model:free", "alive/model:free"],
    ))
    calls: list[str] = []

    def fake_call_model(self, *, model, **kw):
        calls.append(model)
        if model == "dead/model:free":
            raise _NotFound("This model is unavailable for free")
        if model == "google/gemma-4-31b-it:free":   # 主模型也被限流，逼它走到回退链
            raise RuntimeError("429 upstream shared pool")
        return "OK"

    monkeypatch.setattr(R.OpenAILLM, "_call_model", fake_call_model, raising=True)

    # 第一次：主模型 429（假定）→ 撞 dead → 撞 alive 成功
    first = llm._complete_chain(system="s", content="u", stage="context",
                                json_mode=False, temperature=None)
    assert first == "OK"
    assert "dead/model:free" in calls
    assert "dead/model:free" in llm._gone_models

    # 第二次：dead 应被直接跳过，调用序列里不再出现
    calls.clear()
    llm._complete_chain(system="s", content="u", stage="context",
                        json_mode=False, temperature=None)
    assert "dead/model:free" not in calls


def test_fallback_chain_raises_when_all_fail(monkeypatch):
    llm = R.OpenAILLM(_settings(llm_fallback_models=["backup/model:free"]))

    def fake_call_model(self, *, model, **kw):
        raise RuntimeError(f"boom {model}")

    monkeypatch.setattr(R.OpenAILLM, "_call_model", fake_call_model, raising=True)
    with pytest.raises(RuntimeError, match="boom"):
        llm._complete_chain(system="s", content="u", stage="context",
                            json_mode=False, temperature=None)


def test_fallback_chain_mock_when_no_fallback_allowed(monkeypatch):
    """LLM_NO_FALLBACK=False 时，全部模型失败 → 降级 Mock（不是抛错）。"""
    llm = R.OpenAILLM(_settings(llm_fallback_models=["backup/model:free"],
                                llm_no_fallback=False))

    def fake_call_model(self, *, model, **kw):
        raise RuntimeError("all down")

    monkeypatch.setattr(R.OpenAILLM, "_call_model", fake_call_model, raising=True)
    out = llm.complete("sys", "user", stage="context")
    assert isinstance(out, str) and out  # MockLLM 产出


# --------------------------------------------------------------------------- #
# 限流感知退避
# --------------------------------------------------------------------------- #
def test_backoff_returns_float_seconds():
    """``wait=`` 返回的**必须是秒数**。

    回归：tenacity 的 ``DoSleep`` 是 ``float`` 子类，曾把 ``wait_exponential``
    对象当秒数返回，触发
    ``float() argument must be a string or a real number, not 'wait_exponential'``。
    """
    class Boom(Exception):
        status_code = 429
        response = type("Resp", (), {"headers": {"retry-after": "12"}})()

    strategy = R._backoff_for(_settings())

    class RS:
        attempt_number = 1

        def outcome(self_inner):
            class O:
                @staticmethod
                def exception():
                    return Boom()
            return O()

    seconds = strategy(RS())
    assert isinstance(seconds, float) and not isinstance(seconds, bool)
    from tenacity import DoSleep
    DoSleep(seconds)  # 必须能被 float() 构造 DoSleep（回归点）
    assert seconds >= 12  # 不早于上游要求的 12s


def test_backoff_rate_limited_honours_retry_after():
    class Boom(Exception):
        status_code = 429
        response = type("Resp", (), {"headers": {"retry-after": "12"}})()

    assert R._rate_limit_wait_s(_settings(), Boom(), attempt=1) >= 12


def test_backoff_plain_error_is_light():
    class Boom(Exception):
        status_code = 500

    strategy = R._backoff_for(_settings())

    class RS:
        attempt_number = 1

        def outcome(self_inner):
            class O:
                @staticmethod
                def exception():
                    return Boom()
            return O()

    assert 2.0 <= strategy(RS()) <= 20.0  # 非限流仍走轻量退避


def test_backoff_grows_with_attempts_but_capped():
    class Boom(Exception):
        status_code = 429
        response = type("Resp", (), {"headers": {"retry-after": "5"}})()

    a1 = R._rate_limit_wait_s(_settings(), Boom(), attempt=1)
    a4 = R._rate_limit_wait_s(_settings(), Boom(), attempt=99)
    assert a1 >= 5
    assert a4 <= 90  # 封顶


def test_backoff_integration_with_real_tenacity():
    """端到端跑一次真正的 tenacity retry，确认 wait 返回值能被 DoSleep 接受。

    这是最有价值的一条：前面几条单测都是「直接调 wait 函数」，
    无法捕捉「返回值不是 float」这类只有 tenacity 内部才会暴露的错误。
    """
    from tenacity import retry, retry_if_exception, stop_after_attempt

    class Boom(Exception):
        status_code = 429
        response = type("Resp", (), {"headers": {"retry-after": "0"}})()

    settings = _settings(llm_rate_limit_wait_s=0.01)
    attempts: list[int] = []

    @retry(
        stop=stop_after_attempt(3),
        wait=R._backoff_for(settings),
        retry=retry_if_exception(lambda exc: not R._non_retryable(exc)),
        reraise=True,
    )
    def _flaky():
        attempts.append(1)
        raise Boom("429 rate limited")

    with pytest.raises(Boom):
        _flaky()
    assert len(attempts) == 3  # 重试确实发生了，且没有 float() 崩溃


def test_backoff_for_survives_broken_retry_state():
    """retry_state 结构异常时也不能炸，退化为轻量固定退避。"""
    strategy = R._backoff_for(_settings())
    assert strategy(12345) == 2.0  # 不是 RetryCallState 也不应抛


def test_is_rate_limited_detects_429_and_503():
    class E429(Exception):
        status_code = 429

    class E503(Exception):
        status_code = 503

    class E500(Exception):
        status_code = 500

    assert R._is_rate_limited(E429())
    assert R._is_rate_limited(E503())
    assert not R._is_rate_limited(E500())


def test_retry_after_parsed_from_message_text():
    exc = RuntimeError("Provider returned error 429, retry after 7 seconds recommended")
    assert R._retry_after_s(exc) == pytest.approx(7.0)


def test_retry_after_prefers_header_over_text():
    class Boom(Exception):
        status_code = 429
        response = type("Resp", (), {"headers": {"retry-after": "30"}})()

    assert R._retry_after_s(Boom("retry after 3 seconds")) == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def test_fallback_models_parsed_from_csv_string():
    s = Settings(llm_fallback_models="a/b:free, c/d:free")  # type: ignore[arg-type]
    assert s.llm_fallback_models == ["a/b:free", "c/d:free"]


def test_provider_only_parsed_from_csv_string():
    s = Settings(llm_provider_only="Google AI Studio,DeepInfra")  # type: ignore[arg-type]
    assert s.llm_provider_only == ["Google AI Studio", "DeepInfra"]


def test_provider_defaults_present(monkeypatch):
    """默认值（不读 .env）：provider 排序/回退开关/空列表。"""
    for k in ("PROVIDER_SORT", "PROVIDER_ALLOW_FALLBACKS", "LLM_FALLBACK_MODELS",
              "LLM_PROVIDER_ONLY", "LLM_PROVIDER_IGNORE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.provider_sort == "throughput"
    assert s.provider_allow_fallbacks is True
    assert s.llm_provider_only == []
    assert s.llm_provider_ignore == []
    assert s.llm_fallback_models == []


def test_vision_uses_chain_too(monkeypatch):
    """视觉调用同样走回退链：主视觉模型 429 → 备用模型顶上。"""
    import base64
    import tempfile
    from pathlib import Path

    llm = R.OpenAILLM(_settings(llm_vision_model="vision/primary:free",
                                llm_fallback_models=["vision/backup:free"]))
    seen: list[str] = []

    def fake_call_model(self, *, model, system, content, **kw):
        seen.append(model)
        assert any(c.get("type") == "image_url" for c in content if isinstance(c, dict))
        if model == "vision/primary:free":
            class Boom(Exception):
                status_code = 429
            raise Boom("rate limited")
        return json.dumps({"summary": "chart with 3 series"})

    monkeypatch.setattr(R.OpenAILLM, "_call_model", fake_call_model, raising=True)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.png"
        p.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
        ))
        out = llm.vision("sys", "describe", [str(p)])
    assert "3 series" in out
    assert seen == ["vision/primary:free", "vision/backup:free"]
