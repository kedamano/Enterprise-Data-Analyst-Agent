"""Retry policy: 402/auth/4xx must fail fast, transient errors retry."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.infrastructure.llm.router import OpenAILLM, reset_fallback_events


class _E402(Exception):
    status_code = 402


class _E500(Exception):
    status_code = 500


class _Stub:
    def __init__(self, exc) -> None:
        self.exc = exc
        self.calls = 0

    def create(self, *a, **k):
        self.calls += 1
        raise self.exc


def _llm(exc_cls) -> tuple[OpenAILLM, _Stub]:
    reset_fallback_events()
    s = Settings(llm_api_key="sk-d", llm_base_url="http://127.0.0.1:9/v1",
                 llm_model="m", llm_max_retries=5, llm_timeout_s=3,
                 llm_no_fallback=True, mock_llm=False,
                 cb_failure_threshold=10)  # 高阈值避免熔断干扰重试统计
    # 本文件只测「单模型的重试策略」，必须隔离模型回退链：
    # 否则 Settings 会从 .env 读入 LLM_FALLBACK_MODELS，把调用数放大成
    # (1+N 个备用模型) × 重试次数，断言失去意义。
    s.llm_fallback_models = []
    llm = OpenAILLM(s)
    stub = _Stub(exc_cls)
    llm._client = type("Stub", (), {"chat": type("Chat", (), {"completions": stub})()})()
    return llm, stub


def test_402_fails_fast_without_retries():
    llm, stub = _llm(_E402)
    with pytest.raises(Exception):
        llm.complete("s", "u", stage="context", json_mode=True)
    assert stub.calls == 1, f"402 不应重试，实际 {stub.calls} 次"


def test_402_skips_whole_fallback_chain():
    """账号级错误（402 欠费）换模型也没用 —— 必须整链快速失败。

    回归背景：回退链上线后，402 会逐个试完所有备用模型（5 模型 × 6 次重试
    = 30 次无谓请求）。修法：识别账号级错误后 break 出链。
    """
    reset_fallback_events()
    from app.infrastructure.llm.router import OpenAILLM as _L
    s = Settings(llm_api_key="sk-d", llm_base_url="http://127.0.0.1:9/v1",
                 llm_model="m", llm_max_retries=2, llm_timeout_s=3,
                 llm_no_fallback=True, mock_llm=False, cb_failure_threshold=10)
    s.llm_fallback_models = ["backup1/x", "backup2/y", "backup3/z"]

    tried: list[str] = []

    def fake_call_model(self, *, model, **kw):
        tried.append(model)
        raise _E402("insufficient credits")

    from app.infrastructure.llm import router as R
    orig = R.OpenAILLM._call_model
    R.OpenAILLM._call_model = fake_call_model
    try:
        llm = _L(s)
        with pytest.raises(Exception):
            llm.complete("s", "u", stage="context", json_mode=True)
    finally:
        R.OpenAILLM._call_model = orig

    assert tried == ["m"], f"402 应只试主模型即整链失败，实际试了 {tried}"


def test_transient_500_still_retries():
    llm, stub = _llm(_E500)
    with pytest.raises(Exception):
        llm.complete("s", "u", stage="context", json_mode=True)
    assert stub.calls == 5, f"5xx 应重试到 llm_max_retries，实际 {stub.calls}"


def test_connection_error_still_retries():
    class _Conn(Exception):
        pass

    llm, stub = _llm(_Conn)
    with pytest.raises(Exception):
        llm.complete("s", "u", stage="context", json_mode=True)
    assert stub.calls == 5
