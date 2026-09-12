"""LLM gateway with circuit-breaker / fallback behaviour.

The agent orchestration only needs one capability from the model layer:
``complete(system, user, stage, json_mode) -> str``. Two implementations are
provided:

* ``OpenAILLM``  – talks to any OpenAI-compatible chat endpoint (OpenAI,
  DeepSeek, Mistral, vLLM, local llama.cpp, ...). Used in production.
* ``MockLLM``    – deterministic, offline responder used when no API key is
  configured (or ``MOCK_LLM=1``). It emits schema-valid JSON for every stage so
  the entire pipeline can be exercised end-to-end with real tool execution.

Failures against the remote endpoint are retried with exponential backoff
(tenacity) and, after exhaustion, fall back to ``MockLLM`` so the service
stays available instead of hard-failing a request.
"""
from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from ...config import Settings, get_settings
from .circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger("da.llm")

Stage = str  # "context" | "planner" | "executor" | "analyst" | "reflection" | "reporter"

# HTTP 状态码：无意义重试只会拖慢请求（402=欠费、401/403=鉴权、4xx=参数错）。
# 触发即快速失败→降级或上抛，而不是 tenacity 退避重试数轮。
_NON_RETRYABLE_HTTP = {400, 401, 402, 403, 404, 413, 422}

# 429/503 是**上游共享池限流**，短退避几乎必失败：必须尊重 Retry-After 并给足间隔。
_PROVIDER_ROUTING_HOSTS = ("openrouter.ai",)


def _non_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _NON_RETRYABLE_HTTP


def _is_rate_limited(exc: BaseException) -> bool:
    """429 / 503：免费共享池限流，值得更耐心的退避重试。"""
    status = getattr(exc, "status_code", None)
    if status in (429, 503):
        return True
    text = str(exc)
    return "429" in text and "rate" in text.lower()


def _is_model_gone(exc: BaseException) -> bool:
    """模型 slug 已下架/不存在（404）。

    免费模型池的 slug **随时会被下架**（实测：原 `minimax/minimax-m3:free`
    变成"paid only"，再调就是 404 `This model is unavailable for free`）。
    这类失败是**永久性**的：对同一个 slug 重试/退避毫无意义，必须直接换下一个。
    """
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc)
    return ("No endpoints found" in text) or ("unavailable for free" in text) \
        or ("model not found" in text.lower())


def _is_account_level(exc: BaseException) -> bool:
    """账号级永久错误：402 欠费 / 401、403 鉴权。

    这类错误**与具体模型无关**——换哪个模型都会同样失败。因此遇到它时
    遍历整个回退链纯属浪费（真实场景：5 个备用模型 × 6 次重试 = 30 次无谓请求）。
    识别出来直接**整链快速失败**，不要换模型。
    """
    return getattr(exc, "status_code", None) in (401, 402, 403)


def _config_error(exc: BaseException) -> bool:
    """**配置类**错误：400 参数/模型 ID 非法、404 模型已下架、422 语义错误。

    这类错误说明"这条配置写错了"，**不代表上游服务不可用**，因此：
    - 换下一个候选是对的（`_complete_chain` 已在做，并记忆 `_gone_models`）；
    - 但**绝不能计入熔断器**——否则一个写错的模型名在 5 次之后会把整条服务
      静默降级成 Mock（2026-09-12 真实评测实测：7 条用例里 6 条因此退化）。
    """
    return getattr(exc, "status_code", None) in (400, 404, 422)


def _retry_after_s(exc: BaseException) -> Optional[float]:
    """从上游异常里尽力抽取 Retry-After 秒数。

    优先级：HTTP ``Retry-After`` 头（标准位置）→ 错误 message 文本里的
    "retry after N seconds"（OpenRouter 常这么写）。
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        try:
            raw = (headers.get("retry-after") or headers.get("Retry-After")
                   or headers.get("x-ratelimit-reset-requests"))
            if raw:
                return max(0.0, float(str(raw).strip()))
        except Exception:  # pragma: no cover - header 格式异常不影响主流程
            pass
    m = re.search(r"retry[-\s]?after[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?",
                  str(exc), re.IGNORECASE)
    if m:
        return max(0.0, float(m.group(1)))
    return None


def provider_routing_body(settings: Settings) -> Optional[dict[str, Any]]:
    """OpenRouter provider 动态路由参数：让上游自动挑不拥堵/低价/低延迟的 provider。

    非 OpenRouter 端点对该字段无感（会被忽略或直接 400），因此只在
    ``llm_base_url`` 指向 openrouter.ai 时才注入，避免误伤自建/官方端点。
    """
    if not any(h in (settings.llm_base_url or "") for h in _PROVIDER_ROUTING_HOSTS):
        return None
    routing: dict[str, Any] = {
        "allow_fallbacks": bool(settings.provider_allow_fallbacks),
    }
    sort = (settings.provider_sort or "").strip().lower()
    if sort in {"price", "throughput", "latency"}:
        routing["sort"] = sort
    only = list(settings.llm_provider_only or [])
    ignore = list(settings.llm_provider_ignore or [])
    if only:
        routing["only"] = only
    if ignore:
        routing["ignore"] = ignore
    return routing


def _extra_body(settings: Settings) -> Optional[dict[str, Any]]:
    """openai SDK 的 ``extra_body``：承载 provider 路由等非标准字段。"""
    routing = provider_routing_body(settings)
    return {"provider": routing} if routing else None


def _rate_limit_wait_s(settings: Settings, exc: BaseException, attempt: int = 1) -> float:
    """限流退避秒数：不早于 Retry-After，按下限指数放大，封顶 90s。"""
    base = settings.llm_rate_limit_wait_s or 3.0
    floor = max(base, _retry_after_s(exc) or 0.0)
    grown = floor * (2 ** max(0, attempt - 1))
    return float(min(max(90.0, floor), grown))


def _backoff_for(settings: Settings) -> Any:
    """构造 tenacity ``wait=`` 策略：``(retry_state) -> float`` 秒数。

    ⚠️ 踩坑：tenacity 的 ``DoSleep`` 是 ``float`` 的子类，``wait`` 返回的**必须
    是秒数**。早先写成"返回 ``wait_exponential`` 对象"的链式 wait，触发
    ``float() argument must be a string or a real number, not 'wait_exponential'``
    —— wait 对象不是 float，无法构造 DoSleep。
    """
    from tenacity import wait_exponential

    light = wait_exponential(multiplier=1.5, min=2, max=20)

    def _inner(retry_state: Any) -> float:
        exc: BaseException = RuntimeError("retry")
        try:
            outcome = getattr(retry_state, "outcome", None)
            # tenacity 里 outcome 是**属性**；但传入自定义对象时可能是方法 → 兜底调用。
            if callable(outcome):
                outcome = outcome()
            if outcome is not None:
                exc = outcome.exception() or exc
        except Exception:
            pass
        try:
            if _is_rate_limited(exc):
                attempt = getattr(retry_state, "attempt_number", 1) or 1
                return _rate_limit_wait_s(settings, exc, int(attempt))
            return float(light(retry_state))
        except Exception:
            # retry_state 结构异常 / 上游异常无法判定 → 轻量固定退避，绝不炸
            return 2.0

    return _inner


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Fallback tracking (TDD integrity + runtime visibility)
#
# ``fallback_occurred`` lets tests assert that no real-LLM call silently
# degraded to MockLLM ("fake green"). Reset together with the client cache.
#
# DEGRADE/01：降级还必须对**运行态**可见——事件带 run_id 归因、
# 记录最近一次调用结果，供 /health 与 /chat/analyze 透出，
# 避免 D18 那种"配置 true、实际全程 mock 兜底，对外一切正常"。
# --------------------------------------------------------------------------- #
_MAX_ERROR_LEN = 300  # 402 body 很长，不能整段回给前端
_fallback_events: list[dict[str, Any]] = []
_llm_state: dict[str, Any] = {"last_degraded": False}


def record_fallback(stage: Stage, error: Any) -> None:
    run_id = None
    try:  # 归因到当前 trace 上下文（无上下文时为 None，仍记入全量事件）
        from ..observability.tracing import current_run_id

        run_id = current_run_id()
    except Exception:
        run_id = None
    _fallback_events.append({
        "stage": stage,
        "error": str(error)[:_MAX_ERROR_LEN],
        "run_id": run_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _llm_state["last_degraded"] = True


def record_llm_success() -> None:
    """真实调用成功 → 清除"最近一次降级"标记（降级是**当前**状态，不是历史）。"""
    _llm_state["last_degraded"] = False


def fallback_events(run_id: Optional[str] = None) -> list[dict[str, Any]]:
    """降级事件。``run_id`` 给定则只返回该次运行的事件（缺省返回全部，向后兼容）。"""
    if run_id is None:
        return list(_fallback_events)
    return [e for e in _fallback_events if e.get("run_id") == run_id]


def fallback_occurred() -> bool:
    return bool(_fallback_events)


def llm_last_degraded() -> bool:
    """最近一次真实 LLM 调用是否降级（观测值；与配置值 ``use_mock_llm`` 区分）。"""
    return bool(_llm_state.get("last_degraded"))


def reset_fallback_events() -> None:
    _fallback_events.clear()
    _llm_state["last_degraded"] = False


class BaseLLM:
    def complete(
        self,
        system: str,
        user: str,
        stage: Stage = "",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        raise NotImplementedError

    def vision(
        self,
        system: str,
        user: str,
        image_paths: list[str],
        stage: Stage = "vision",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """P2-2：多模态（视觉）补全。``image_paths`` 为本地图片绝对路径列表。

        默认实现不支多模态；子类（OpenAILLM）覆盖。MockLLM 覆盖为离线响应。
        """
        raise NotImplementedError


class OpenAILLM(BaseLLM):
    def __init__(self, settings: Settings) -> None:
        try:
            from openai import OpenAI  # lazy import – keep deps optional at boot
        except ImportError as exc:  # pragma: no cover
            raise LLMError("openai package not installed") from exc
        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
        self._settings = settings
        self._breaker = CircuitBreaker(
            failure_threshold=settings.cb_failure_threshold,
            recovery_timeout_s=settings.cb_recovery_timeout_s,
        )

    # ------------------------------------------------------------------ #
    # 低层调用：一次「单模型 × 多模态内容」的请求（带熔断 + 限流感知退避）
    # ------------------------------------------------------------------ #
    def _call_model(
        self,
        *,
        model: str,
        system: str,
        content: Any,
        stage: Stage,
        json_mode: bool,
        temperature: Optional[float],
        usage: Optional[dict[str, int]] = None,
    ) -> str:
        from tenacity import retry, retry_if_exception, stop_after_attempt

        extra_body = _extra_body(self._settings)

        @retry(
            stop=stop_after_attempt(self._settings.llm_max_retries),
            wait=_backoff_for(self._settings),
            # 402/鉴权/参数错等不可重试错误：单次失败立即交给外层快速失败/降级
            retry=retry_if_exception(lambda exc: not _non_retryable(exc)),
            reraise=True,
        )
        def _call() -> str:
            kwargs: dict[str, Any] = dict(
                model=model,
                temperature=temperature if temperature is not None else self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                timeout=self._settings.llm_timeout_s,
                response_format={"type": "json_object"} if json_mode else None,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            resp = self._client.chat.completions.create(**kwargs)
            u = getattr(resp, "usage", None)
            if u is not None and usage is not None:
                usage["prompt"] = int(getattr(u, "prompt_tokens", 0) or 0)
                usage["completion"] = int(getattr(u, "completion_tokens", 0) or 0)
            return resp.choices[0].message.content or ""

        # Circuit breaker around the (internally-retried) call:
        # success → close/reset; failure → count; OPEN → skip network fast.
        # **配置类错误不计入熔断**（写错的模型 ID 不该拖垮整条服务，见 _config_error）
        return self._breaker(_call, count_failure=lambda exc: not _config_error(exc))

    def _record_usage(self, usage: dict[str, int]) -> None:
        if usage:
            from ...infrastructure.observability.tracing import record_tokens

            record_tokens(usage.get("prompt", 0), usage.get("completion", 0))

    # ------------------------------------------------------------------ #
    # 动态模型回退链：主模型被限流/失败时，按序自动尝试备用模型
    # ------------------------------------------------------------------ #
    def _model_chain(self, primary: str) -> list[str]:
        """主模型在前，其后是按配置追加的备用模型（去重、去空、跳过主模型本身）。"""
        chain: list[str] = []
        for m in [primary, *_fallback_models(self._settings)]:
            m = (m or "").strip()
            if m and m not in chain:
                chain.append(m)
        return chain or [primary]

    def complete(
        self,
        system: str,
        user: str,
        stage: Stage = "",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        try:
            return self._complete_chain(
                system=system, content=user, stage=stage,
                json_mode=json_mode, temperature=temperature,
            )
        except Exception as exc:
            if isinstance(exc, CircuitOpenError):
                logger.error("LLM circuit open (%s); skipping network call: %s", stage, exc)
            if self._settings.llm_no_fallback:
                logger.error("LLM call failed (%s) and LLM_NO_FALLBACK is set; re-raising", stage)
                raise
            # DEGRADE/01：降级用 error 级（可被告警规则捕获），不再"悄悄 warning 一下"
            logger.error("LLM call failed (%s); falling back to mock: %s", stage, exc)
            record_fallback(stage, exc)
            return MockLLM().complete(system, user, stage, json_mode, temperature)

    def _complete_chain(
        self, *, system: str, content: Any, stage: Stage,
        json_mode: bool, temperature: Optional[float],
        primary: Optional[str] = None,
    ) -> str:
        usage: dict[str, int] = {}
        last: Optional[BaseException] = None
        chain = self._model_chain(primary or self._settings.llm_model)
        # 已确认下架的 slug：同一进程内不再重复尝试（免费池 slug 随时会消失，
        # 每次都撞一遍 404 纯属浪费一次网络往返）。
        gone: set[str] = getattr(self, "_gone_models", None) or set()
        self._gone_models = gone
        for idx, model in enumerate(chain):
            if model in gone:
                logger.info("跳过已下架模型：%s", model)
                continue
            try:
                out = self._call_model(
                    model=model, system=system, content=content, stage=stage,
                    json_mode=json_mode, temperature=temperature, usage=usage,
                )
                record_llm_success()
                self._record_usage(usage)
                if idx:
                    logger.warning(
                        "模型回退生效：主模型不可用，改用 %s（stage=%s）", model, stage,
                    )
                return out
            except Exception as exc:
                last = exc
                if _is_account_level(exc):
                    # 账号级永久错误（402 欠费 / 401·403 鉴权）：换模型也没用，
                    # 立即整链失败，避免 N 个备用模型 × M 次重试的无谓放大。
                    logger.error(
                        "账号级错误（%s），跳过回退链直接失败：%s", stage, exc,
                    )
                    break
                if _is_model_gone(exc):
                    gone.add(model)
                    logger.warning("模型 %s 已下架/不可用（stage=%s），永久跳过：%s",
                                   model, stage, exc)
                elif idx + 1 < len(chain):
                    logger.warning(
                        "模型 %s 调用失败（%s），尝试下一个备用模型：%s",
                        model, stage, exc,
                    )
        raise last if last is not None else LLMError("no model candidates")

    def vision(
        self,
        system: str,
        user: str,
        image_paths: list[str],
        stage: Stage = "vision",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """P2-2：多模态补全。把本地图片编码成 base64 data URL，作为 image_url
        内容块发给视觉模型（OpenAI 兼容端点通用写法）。"""
        model = self._settings.llm_vision_model or self._settings.llm_model
        model = model.strip()
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        _MIME = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
        }
        for p in image_paths:
            try:
                import base64
                from pathlib import Path as _P
                raw = _P(p).read_bytes()
                mime = _MIME.get(_P(p).suffix.lower(), "image/png")
                b64 = base64.b64encode(raw).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            except Exception as exc:
                logger.warning("vision: 读取图片失败 %s: %s", p, exc)

        try:
            return self._complete_chain(
                system=system, content=content, stage=stage,
                json_mode=json_mode, temperature=temperature, primary=model,
            )
        except Exception as exc:
            if isinstance(exc, CircuitOpenError):
                logger.error("LLM circuit open (%s); skipping network vision call: %s", stage, exc)
            if self._settings.llm_no_fallback:
                logger.error("vision LLM call failed (%s) and LLM_NO_FALLBACK is set; re-raising", stage)
                raise
            logger.error("vision LLM call failed (%s); falling back to mock: %s", stage, exc)
            record_fallback(stage, exc)
            return MockLLM().vision(system, user, image_paths, stage, json_mode, temperature)


def _fallback_models(settings: Settings) -> list[str]:
    """解析 LLM_FALLBACK_MODELS：支持字符串 id 或 {model: ...} 两种写法。"""
    out: list[str] = []
    for item in settings.llm_fallback_models or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get("model"):
            out.append(str(item["model"]))
    return out


class MockLLM(BaseLLM):
    """Offline responder good enough to drive the whole graph with real tools."""

    _JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
    # §21 信封：结构化 payload 一律在 <task_context> 里，用户原话在 <user_request> 里。
    # 降级路径必须**只**从 task_context 取 JSON —— 否则贪婪正则会把用户原话里的
    # `{}`、或工具输出里的花括号一起吞进来，json.loads 直接失败，
    # 于 Mock analyst 看到空的 tool_results、永远回报「未获取到事实数据」，
    # 触发无意义的 REPLAN 空转（这正是 sleep.csv 案例三轮打转的根因之一）。
    _CTX_RE = re.compile(r"<task_context>\s*(.*?)\s*</task_context>", re.DOTALL)

    def complete(
        self,
        system: str,
        user: str,
        stage: Stage = "",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        handler = getattr(self, f"_stage_{stage}", None)
        if handler is None:
            if not json_mode:
                return f"[mock:{stage}] （无离线响应）"
            return json.dumps({"note": f"mock has no handler for stage={stage}"})
        result = handler(user)
        if isinstance(result, str):
            return result if not json_mode else json.dumps({"text": result})
        return json.dumps(result)

    def _stage_memory_summary(self, user: str) -> str:
        lines = [ln.strip() for ln in (user or "").splitlines() if ln.strip()]
        head = " | ".join(lines[:3])[:160]
        return f"[记忆摘要] 已压缩 {len(lines)} 条历史。{head}…"

    def _stage_python_code_gen(self, user: str) -> str:
        """E2 自由 Python：确定性可跑脚本（df 由 sandbox prelude 从 DATA_CSV 装载）。"""
        return (
            "import pandas as pd\n"
            "summary = {}\n"
            "if df is not None:\n"
            "    summary['rows'] = int(df.shape[0])\n"
            "    summary['cols'] = list(df.columns)\n"
            "    summary['by_region'] = (df.groupby(df.columns[0]) if df.shape[1] > 0 else None)\n"
            "    if summary['by_region'] is not None:\n"
            "        summary['head'] = df.head(3).to_dict('records')\n"
            "print(_json.dumps(summary, default=str))"
        )

    # ----- stage handlers -------------------------------------------------
    def _stage_context(self, user: str) -> dict:
        q = self._extract_user_query(user)
        metrics = []
        for kw, m in [
            ("营收|收入|revenue", "revenue"),
            ("订单|order", "orders"),
            ("用户|user|customer", "users"),
            ("转化率|conversion", "conversion_rate"),
            ("成本|cost", "cost"),
            ("利润|profit", "profit"),
        ]:
            if re.search(kw, q, re.IGNORECASE):
                metrics.append(m)
        dims = []
        for kw, d in [
            ("地区|区域|region", "region"),
            ("产品|product", "product"),
            ("渠道|channel", "channel"),
            ("客户分群|segment", "customer_segment"),
        ]:
            if re.search(kw, q, re.IGNORECASE):
                dims.append(d)
        objective = q.strip().split("\n")[0][:200]
        return {
            "objective": objective or "分析业务数据并给出洞察",
            "analysis_object": metrics[:1] or ["business_metric"],
            "metrics": metrics or ["key_metric"],
            "dimensions": dims,
            "filters": {},
            "time_range": {"start": "auto", "end": "auto", "timezone": "Asia/Shanghai"},
            "comparison": {"type": "previous_period", "period": "auto"},
            "output_format": "report",
            "constraints": [],
            "assumptions": ["时间范围自动推断为最近可用区间"],
            "clarification_required": False,
            "clarification_questions": [],
        }

    def _stage_planner(self, user: str) -> dict:
        ctx = self._extract_json(user) or {}
        # P2-2：用户上传了图片 → 首步用 image_analyze 实际读取（确定性保底，
        # 与 uploaded_datasets 的附件计划同源思路：小模型/降级容易忽略多模态输入）。
        steps = []
        imgs = ctx.get("uploaded_images") or []
        if imgs:
            first_img = imgs[0].get("name") or imgs[0].get("path") or "uploaded_image"
            steps.append({
                "id": "step_0",
                "objective": "识别上传图片中的业务数据",
                "action": "调用 image_analyze 读取图表/截图",
                "tool": "image_analyze",
                "dependencies": [],
                "expected_output": "图片中的关键数据/指标/趋势",
                "success_criteria": "返回图片内容的结构化描述",
                "input": {"image": first_img,
                          "question": "提取图中的关键数据、指标、维度与趋势"},
            })
        steps += [
            {"id": "step_1", "objective": "发现可用数据表与字段", "action": "检索schema", "tool": "schema_search", "dependencies": [], "expected_output": "相关表与字段", "success_criteria": "找到与目标指标相关的表"},
            {"id": "step_2", "objective": "探查数据质量", "action": "对核心表做数据画像", "tool": "dataset_profile", "dependencies": ["step_1"], "expected_output": "缺失/重复/分布摘要", "success_criteria": "识别主要数据质量问题"},
            {"id": "step_3", "objective": "计算核心指标", "action": "执行SQL聚合查询", "tool": "sql_query", "dependencies": ["step_1"], "expected_output": "指标当期/对比期数值", "success_criteria": "返回非空结果"},
        ]
        if ctx.get("dimensions"):
            steps.append({"id": "step_4", "objective": "维度下钻", "action": "按维度分组聚合", "tool": "sql_query", "dependencies": ["step_3"], "expected_output": "维度贡献度", "success_criteria": "定位主要贡献维度"})
        steps.append({"id": "step_5", "objective": "统计与可视化", "action": "Python做趋势/相关性分析并出图", "tool": "python_analysis", "dependencies": ["step_3"], "expected_output": "统计结果与图表", "success_criteria": "生成1张以上图表"})
        return {
            "goal": ctx.get("objective", "完成分析"),
            "steps": steps[: get_settings().max_plan_steps],
            "stopping_criteria": ["核心业务问题已回答", "关键发现具备证据"],
            "risk_points": ["数据口径不一致", "对比基线缺失"],
        }

    def _stage_analyst(self, user: str) -> dict:
        results = self._extract_json(user) or {}
        tool_outputs = results.get("tool_results", [])
        findings = []
        for tr in tool_outputs:
            out = tr.get("output", {})
            if "rows" in out and out.get("rows"):
                sample = out["rows"][0]
                findings.append({
                    "finding": f"工具 {tr.get('tool')} 返回 {len(out['rows'])} 行结果，首行示例：{json.dumps(sample, ensure_ascii=False)[:300]}",
                    "evidence": [{"source": tr.get("tool"), "metric": "result", "value": str(len(out["rows"])), "comparison": "n/a", "impact": "提供事实依据"}],
                    "interpretation": "已通过工具获取真实数据，可作为结论支撑。",
                    "confidence": 0.9,
                })
        if not findings:
            findings.append({"finding": "暂未获取到可支撑的事实数据", "evidence": [], "interpretation": "需要更多工具调用", "confidence": 0.3})
        return {
            "metrics": [],
            "findings": findings,
            "hypotheses": [],
            "recommendations": [{"problem": "需结合业务行动", "evidence": "见发现", "action": "针对主要贡献维度制定改进措施", "expected_impact": "提升核心指标"}],
            "limitations": ["当前处于降级模式（未启用真实模型），结论仅基于工具返回的原始数据，未做深度统计推断。"],
        }

    def _stage_reflection(self, user: str) -> dict:
        results = self._extract_json(user) or {}
        # findings may sit at top-level or nested under "analysis"
        findings = results.get("findings") or results.get("analysis", {}).get("findings", [])
        evidence_ok = any(f.get("evidence") for f in findings)
        decision = "PASS" if evidence_ok else "REPLAN"
        conf = 0.9 if evidence_ok else 0.5
        return {
            "decision": decision,
            "confidence": conf,
            "data_quality": {"score": 0.8, "issues": []},
            "metric_quality": {"score": 0.8, "issues": []},
            "evidence_coverage": {"score": 0.9 if evidence_ok else 0.4, "issues": [] if evidence_ok else ["部分发现缺少证据"]},
            "logical_validity": {"score": 0.85, "issues": []},
            "completeness": {"score": 0.8, "issues": []},
            "business_relevance": {"score": 0.85, "issues": []},
            "missing_evidence": [],
            "replan_objectives": [] if evidence_ok else ["补充关键维度的工具调用"],
            "summary": "证据充分" if evidence_ok else "证据不足，需要补充分析",
        }

    def _stage_reporter(self, user: str) -> dict:
        # reporter returns a markdown string (not json) per the spec.
        return {"__markdown__": True}  # signal handled by node

    # ----- P2-2：离线视觉响应 ------------------------------------------
    def vision(self, system, user, image_paths, stage="vision", json_mode=False,
               temperature=None) -> str:
        """离线视觉：返回确定性、可被 Analyst 当作证据使用的结构化描述。

        与 sleep.csv 案例一致——Mock 必须给出**具体数值**，否则下游 Analyst
        会报「未获取到事实数据」、触发无意义的 REPLAN。
        """
        names = ", ".join(image_paths) if image_paths else "（无图片）"
        description = (
            f"[vision-mock] 已识别图片：{names}。\n"
            "图中为一份示例业务图表：包含「区域 / 销售额 / 环比」三列，"
            "Region A 销售额最高（约 1200），Region C 环比下滑最明显（-18%）。"
        )
        if json_mode:
            return json.dumps({
                "description": description,
                "chart_type": "bar",
                "text_elements": ["Region A", "Region C", "销售额", "环比"],
                "structured_data": [
                    {"region": "Region A", "sales": 1200, "mom": 0.05},
                    {"region": "Region C", "sales": 640, "mom": -0.18},
                ],
            }, ensure_ascii=False)
        return description

    # ----- helpers --------------------------------------------------------
    _ENVELOPE_RE = re.compile(r"<user_request>\n?(.*?)\n?</user_request>", re.DOTALL)

    @classmethod
    def _extract_user_query(cls, user: str) -> str:
        """§21 信封兼容：优先取 <user_request> 数据块内的用户原话。"""
        m = cls._ENVELOPE_RE.search(user)
        return m.group(1).strip() if m else user

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        if not isinstance(text, str):
            return None
        # 1) 首选：显式信封里的 <task_context>（结构化 payload 的唯一合法位置）
        m = MockLLM._CTX_RE.search(text)
        if m:
            try:
                loaded = json.loads(m.group(1))
                if isinstance(loaded, dict):
                    return loaded
            except json.JSONDecodeError:
                pass
        # 2) 兜底：整段文本里的第一个平衡 JSON 对象
        for blob in MockLLM._balanced_objects(text):
            try:
                loaded = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                return loaded
        return None

    @staticmethod
    def _balanced_objects(text: str) -> Iterator[str]:
        """按括号配对切出候选 JSON 对象（比贪婪 ``\\{.*\\}`` 稳）。

        贪婪正则会把「第一个 { 到最后一个 }」整段取出，一旦中间混入非 JSON
        内容（用户原话、日志、多段 payload）就整体解析失败；这里逐个配对，
        第一个能成功 json.loads 的即为答案。
        """
        depth = 0
        start = -1
        in_str = False
        esc = False
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
                        yield text[start:i + 1]
                        start = -1


_llm: Optional[BaseLLM] = None


def get_llm(settings: Optional[Settings] = None) -> BaseLLM:
    """Return a cached LLM client: Mock → weighted RouterLLM → single OpenAILLM."""
    global _llm
    settings = settings or get_settings()
    if _llm is None:
        if settings.use_mock_llm:
            logger.info("Using MockLLM (no API key / MOCK_LLM=1)")
            _llm = MockLLM()
        elif settings.llm_routes:
            from .model_router import ModelConfig, RouterLLM, parse_routes

            configs = [ModelConfig.from_dict(d, settings) for d in parse_routes(settings.llm_routes)]
            logger.info("Using RouterLLM with %d providers", len(configs))
            _llm = RouterLLM.from_configs(configs, settings)
        else:
            _llm = OpenAILLM(settings)
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
    reset_fallback_events()
