from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Optional

from pydantic import Json, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _as_list(v: Any) -> Any:
    """Allow list-like settings to be given as a JSON string in ``.env``.

    ``Json`` metadata keeps pydantic-settings from force-parsing the raw env
    string as JSON, so we can accept the friendlier comma-separated form too.
    Empty string → ``[]``; a ``[``/``{`` opener is delegated to ``json.loads``.
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s[0] in "[{":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return [item.strip() for item in s.split(",") if item.strip()]
        return [item.strip() for item in s.split(",") if item.strip()]
    return v


# ``Json()`` (an *instance* — the bare class does NOT match) marks the field
# non-complex to pydantic-settings, so the raw env string reaches our validator
# instead of being force-decoded as JSON. That lets ``.env`` use a friendly
# comma-separated list while still accepting a JSON array.
StrList = Annotated[list[str], Json()]
DictList = Annotated[list[dict[str, Any]], Json()]
AnyList = Annotated[list[Any], Json()]


class Settings(BaseSettings):
    """Application configuration loaded from environment / ``.env`` file.

    Mirrors the ``project-python`` layout: every external system is optional and
    degrades to an in-process fallback when its connection string is absent, so
    the service can boot and be developed locally without a full middleware
    stack.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- App ---
    app_name: str = "Enterprise Data Analyst Agent"
    environment: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    @field_validator(
        "llm_routes", "llm_fallback_models", "llm_provider_only", "llm_provider_ignore",
        mode="plain",
    )
    @classmethod
    def _normalize_list_fields(cls, v: Any) -> Any:
        return _as_list(v)

    # --- LLM gateway (OpenAI-compatible) ---
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    # P2-2：视觉（多模态）模型。空 = 复用 llm_model（多数兼容端点 gpt-4o-mini
    # 本身支持视觉）。若视觉能力在独立模型/端点上，可单独配置。
    llm_vision_model: str = ""
    # 0.0 for reproducible evaluation / production; raise for creative variation.
    llm_temperature: float = 0.0
    # 默认不超过常见免费/低额度上游（如 OpenRouter）的剩余余额，避免 402。
    # 真实环境若额度充足可调高；agent 各阶段 JSON 响应通常在 2k 以内。
    llm_max_tokens: int = 2048
    llm_timeout_s: int = 120
    # **墙钟兜底**（秒）：与传输层 timeout 无关的硬上限。
    #
    # 为什么需要它：httpx 的 read timeout **每收到一块数据就重置**——上游只要周期性
    # 吐 keep-alive 字节（网关/反代常见），`llm_timeout_s` 就**永不触发**。
    # 2026-09-14 实测：进程在 analyst 的 LLM 调用上挂了 **>3 小时**（CPU=0s，等 I/O），
    # 而配置是 timeout=300 + 5 次重试 —— 整轮评测被永久占住，且无任何告警。
    #
    # 必须**大于** llm_timeout_s（否则会误杀正常但慢的调用：实测单次 context 调用可到 253s）。
    # 设为 0 = 关闭兜底（退回旧行为，便于排障）。
    llm_hard_deadline_s: float = 600.0
    llm_max_retries: int = 5
    # D42：**prompt 预算强制**（tokens，按 CJK 感知的字符代理估算）。
    #
    # 此前 `memory/budget.py` 的 `fit_to_budget` **只在测试里被调用过**（app/ 下零命中），
    # 于是 `build_user_message` 直接 `json.dumps(task_context)`——而 analyst 的 payload
    # 带 `tool_results[].output.rows`，8 个工具结果轻易把单次 prompt 推到十几万字符。
    #
    # 超预算时按优先级压缩（先砍行数据 → 再丢最旧工具结果 → 最后只留 context/plan），
    # **压缩说明会写进 prompt 的 `<context_budget>` 块**（不许静默）。
    # 实测正常单次 prompt 约 5–9k tokens，16000 只兜异常大的尾巴、不动正常链路。
    # 设 0 = 关闭（退回旧行为）。
    prompt_budget_tokens: int = 16000
    # D43：**单会话（按 run）token 熔断**。
    #
    # 单次运行的成本此前**没有硬上限**：REPLAN 循环 / 失败重试 / 多模型链回落，
    # 任一环节打滑都会让同一轮不断发请求。实测一次 `--only-real` 全量约 **91 万** tokens
    # （正常），但没有任何东西阻止它变成 9000 万。
    #
    # 默认 **200 万**：明显高于一次正常全量（≈91 万），给重规划留足余量，
    # 又能兜住真正的打滑。**默认值不能打断正常跑**——这条与 D42 同源。
    # 设 0 = 关闭。
    session_token_budget: int = 2_000_000
    # D44：**日志采样比例**（1.0 = 不采样）。高并发下每个节点一条 [span] INFO，
    # 量级随请求线性上涨。**只采"正常"，失败永不采样**——采掉失败日志等于故障自愈。
    # 写成 0/负数 = 不采样（而不是全丢弃：静音日志不是采样，是失明）。
    log_sample_ratio: float = 1.0
    # **推理模型**的思考预算：none | low | medium | high。
    # 留空 = 不注入该字段（默认行为，非推理模型不受影响）。
    # 为什么必须有这个开关（实测 glm-5.3，8k prompt）：
    #   不给约束 → 15352/16384 token 花在 reasoning、正文被截断（finish=length）、单次 504s；
    #   设 none   → reasoning ~41 token、单次 ~10s。
    # 即"推理预算"决定这个模型**能不能用**，不是性能优化。
    llm_reasoning_effort: str = ""
    # token 计费价（USD / 每百万 token）。
    # **未设置 = 单价未知** → eval 报 `cost=None`（"不知道多少钱"）；
    # **显式设 0 = 已知免费** → eval 报 `0.0`（"确实不花钱"）。
    # 这两件事必须能区分：此前用 0.0 兼表"未知"，于是免费模型（真 0）与
    # 没配单价在报告里长得一模一样，成本列**恒为 None**，谁也没法发现。
    cost_input_per_mtok: Optional[float] = None
    cost_output_per_mtok: Optional[float] = None
    # 三态熔断（project-python circuit_breaker 同款）：连续失败超过阈值 → OPEN，
    # Open 态跳过网络直降级；recovery 后进 HALF_OPEN 放行一次探测。
    cb_failure_threshold: int = 5
    cb_recovery_timeout_s: float = 60.0
    # 多供应商路由（可选）：JSON 数组，每项 {model, base_url?, api_key?, priority?, weight?}
    # 例：[{"model":"deepseek/deepseek-chat","base_url":"https://openrouter.ai/api/v1","priority":0,"weight":3},
    #       {"model":"gpt-4o-mini","base_url":"https://api.openai.com/v1","priority":1,"weight":1}]
    # 未配置时退化为单模型 OpenAILLM。
    llm_routes: DictList = []
    # Dynamic provider routing（OpenRouter）：让上游在多个 provider 间挑不拥堵的那个。
    #   provider_sort: "price" | "throughput" | "latency"  → 排序偏好
    #   provider_allow_fallbacks: 允许上游在同模型的其他 provider 间自动故障转移
    #   provider_only/ignore:    白/黑名单（逗号分隔），如 only="Google AI Studio"
    # 非 OpenRouter 端点会忽略这些字段，因此默认开启是安全的。
    provider_sort: str = "throughput"
    provider_allow_fallbacks: bool = True
    llm_provider_only: StrList = []
    llm_provider_ignore: StrList = []
    # LLM_ROUTES_FALLBACK：主模型/主端点失败或被限流时，按序自动尝试的备用模型链。
    # 每项为模型 id 字符串（复用 llm_base_url/llm_api_key），或 {model, base_url?, api_key?}。
    # 典型用途：OpenRouter 免费池某模型 429（上游共享池限流）时自动切到下一免费模型。
    # 例：["google/gemma-4-31b-it:free", "nvidia/nemotron-nano-12b-v2-vl:free"]
    llm_fallback_models: AnyList = []
    # P0-4/RATE：429/5xx 等限流错误即使被标记为"可重试"，也要尊重上游 Retry-After
    # 并给足退避；0 = 使用内置默认（起 3s、封顶 90s、指数 2x、最多 llm_max_retries 次）。
    llm_rate_limit_wait_s: float = 0.0
    # 测试/调试时设为 true：真实 LLM 调用失败直接抛错，禁止静默降级到 Mock（避免假绿）
    llm_no_fallback: bool = False
    # When true (or when no api key is set) the orchestration uses a deterministic
    # mock responder so the full pipeline can run end-to-end without network access.
    mock_llm: bool = False

    # --- Enterprise analytical data source (what the agent queries) ---
    # Demo default points to a bundled SQLite sample; production overrides with a
    # postgres/warehouse DSN. Supports dialects: sqlite, postgresql, mysql.
    data_db_url: str = "sqlite:///./data/sample_enterprise.db"
    data_db_dialect: str = "sqlite"
    # E7/01 命名数据源（可选）：JSON 数组或 `name=url` 逗号列表。
    # 不填 = 只用主源；配置写坏会被忽略并 warning（不阻塞启动）。
    # 明确不做跨源 JOIN —— 需要跨源请在各自源取数后用 python_analysis 合并。
    data_sources: str = ""

    # --- Knowledge base (RAG) ---
    knowledge_enabled: bool = True
    # Milvus 接法（三选一，优先级从上到下）：
    #   1) milvus_lite_path = "./data/milvus_lite"  → **Milvus Lite** 嵌入式本地库
    #      （会创建同名目录；pymilvus 硬要求 URI 以 .db 结尾，适配层会自动补后缀），
    #      无需服务端，便于本机/CI 真实验证。
    #      ⚠️ 必须用**独立**环境变量 MILVUS_LITE_PATH：pymilvus 自身会读 MILVUS_URI 并在
    #      import 时按 http(s):// 解析，若把文件路径塞进 MILVUS_URI 会导致 pymilvus 直接崩。
    #   2) milvus_uri = "http://host:19530"（真实服务端，与 pymilvus 原生变量一致）
    #   3) milvus_host + milvus_port（向后兼容）
    milvus_lite_path: str = ""
    milvus_uri: str = ""
    milvus_host: str = ""
    milvus_port: int = 19530
    milvus_collection: str = "da_knowledge"
    embed_model: str = "all-MiniLM-L6-v2"
    rerank_enabled: bool = True
    # 可选的 cross-encoder 重排模型（空 = 用确定性短语亲和重排，避免依赖外部模型）
    rerank_cross_encoder: str = ""
    # 嵌入模型加载墙钟预算（秒）：超时即禁用嵌入、降级为纯 BM25 检索，避免
    # sentence-transformers 在本机联网校验/首次加载时无限阻塞入库与检索。
    embed_load_timeout_s: float = 25.0

    # --- Memory / cache ---
    redis_url: str = ""
    postgres_dsn: str = ""
    # DEGRADE/02：中间件「配了但没起」时必须快速失败，不能把 OS 级 TCP 超时
    # （本机 ~2s）平摊到每个流水线节点上。跨机房部署请上调这两个值。
    redis_connect_timeout_s: float = 0.2
    redis_socket_timeout_s: float = 0.5

    # --- Multi-tenant (memory & knowledge) ---
    # 空 = 单租户/全局（现有行为不变）；非空时记忆与知识检索强制按该租户隔离。
    default_tenant: str = ""

    # --- AUTH/01 用户级鉴权与数据权限 ---
    # **默认关**：既有本地开发与全部用例行为不变；生产务必打开并配 AUTH_KEYS。
    auth_enabled: bool = False
    # JSON 数组：[{key, user_id, tenant, roles?, allowed_tables?, denied_columns?,
    #             row_filters?, quota_per_min?}]，见 docs/specs/AUTH/01 §5
    auth_keys: str = ""
    auth_anonymous_tenant: str = ""

    # --- D45 两步授权（HITL）：高危动作需二次确认 ---
    # **默认关**：既有 950+ 用例与本地开发行为零影响。
    # 打开后，`export_raw`（导出未脱敏原始值）/ `deliver_python`（交付模型写的脚本）
    # / 以及**任何未登记的动作**都需人工确认后才执行。
    hitl_enabled: bool = False
    # 审计落盘路径（允许与拒绝**都记**——只记拒绝无法复盘，同 AUTH/01 的取舍）
    hitl_audit_log: str = "data/audit/hitl.jsonl"

    # --- D46 审计落库（SQLite / PostgreSQL）---
    # jsonl（默认，既有行为一字不变）| sqlite | postgres
    # 缺口原文："审计为文件非不可篡改库、无 SQL 审计查询"。
    # **不做双写**：写两处会立刻带来"以哪份为准"的新问题；只写一处，由配置决定，
    # 历史 JSONL 用 `audit_store.import_jsonl()` 一次性迁入（内容指纹保证幂等）。
    audit_backend: str = "jsonl"
    # 留空时：sqlite → ./data/audit.db；postgres → 复用 POSTGRES_DSN
    audit_db_url: str = ""
    # --- Safety ---
    sql_max_rows: int = 5000
    # E4/04 质量门禁阈值：join 放大倍数 / 列缺失率（超阈值才提示）
    profile_join_amp_threshold: float = 1.5
    profile_null_high_ratio: float = 0.3
    # dataset_profile 逐列 COUNT(DISTINCT) 的**批量大小**。
    # 原实现每列发一条查询 → N 次全表扫描（1M×9 列实测 PG 3.7s / MySQL 12.6s，
    # 随**列数**线性恶化，100 列外推 ≈22s）。合并为每批一条聚合后
    # N 列 → ceil(N/batch) 次扫描，**结果完全等价**（非抽样、非近似）。
    # 调小可降低单条 SQL 的宽度（超宽表/代理限制），调大减少扫描次数。
    profile_column_batch: int = 16
    # 宽表上界：最多画像多少列（**0 = 不限制**，向后兼容）。
    # 批量化只省"重复扫表"，省不掉每列 COUNT(DISTINCT) 的去重开销 → 列数因子仍线性。
    # 超此上限时按优先级保留（声明的键/日期列优先），未画像的列记入 `columns_skipped`。
    # 默认 40：常见分析表（≤40 列）行为与之前完全一致；超宽表才触发截断。
    profile_max_columns: int = 40
    # E4/02 输出脱敏：**默认开**。敏感列的值不进 LLM 上下文（CSV 产物不脱敏）。
    mask_pii_enabled: bool = True
    mask_level: str = "sample"        # none | sample | strict
    mask_pii_columns: str = ""        # 额外敏感列（逗号分隔，列名未命中模式表时用）
    # E4/06 DLP 细粒度：按角色/字段级策略（JSON：{role: {default_level, column_levels, deny_columns}}）
    # 空 = 不激活策略，导出行为与 E4/02 一字不变（默认零影响）
    dlp_policy: str = ""
    # 导出水印密钥（HMAC）。空 = 不出水印（默认零影响）
    dlp_watermark_secret: str = ""
    # D50 MCP 接入层：**默认关**。把只读工具按 MCP 协议暴露给外部 Agent（Claude Desktop /
    # Cursor）。开启后 `/api/v1/mcp/*` 生效；关闭时两端点返回 503（不静默空跑）。
    # 接入层只做编排，鉴权/限流/守卫/审计全部委托 execute_tool（不重写安全）。
    mcp_enabled: bool = False
    # MCP 调用的归集 session_id（供限流/审计）。不伪造真实会话，默认 "mcp"。
    mcp_session_id: str = "mcp"
    # SEMANTIC/01 业务语义层：维表枚举采集上限与缓存时长
    profile_enum_max_cardinality: int = 20   # > 此基数的列不采枚举（避免高基数列）
    profile_enum_max_values: int = 20        # 每个维表最多取多少个取值
    semantics_ttl_s: float = 3600.0          # 语义缓存 TTL（秒）
    # 短期记忆（Redis `da:st:<session>` hash）的滑动过期时间。
    # <=0 表示不过期（仅本地调试）；默认 24h 比参考实现的 60min 长，因为会话可能停在
    # CLARIFY 等用户回答、跨天回来还要能续跑。不设过期会让会话键在 Redis 里只增不减。
    short_term_ttl_s: float = 86400.0
    sql_readonly: bool = True
    python_sandbox_enabled: bool = True
    python_max_exec_s: int = 30
    # python_analysis 执行后端：subprocess（默认，AST+子进程）| docker（受限容器，
    # 参考 DeepAnalyze docker_executor）| auto（docker 可用则用，否则子进程）
    python_sandbox_mode: str = "subprocess"
    python_sandbox_image: str = "da-python-sandbox"
    # 错误处理协议（§23）：RETRYABLE 错误的最大重试次数（规格建议 2~3，禁止无限重试）
    max_tool_retries: int = 2

    # --- Orchestration ---
    # Orchestration
    max_replans: int = 2
    max_plan_steps: int = 8
    # INTERVIEW/01 ③：工具数超过此阈值才启用语义路由（工具少时全给，路由是负收益）
    tool_routing_threshold: int = 12
    # INTERVIEW/01 ④：请求级缓存（同问重复问直接命中，省下整条链的 LLM 调用）
    # 按会话隔离；只缓存 FINISH；force_full_rerun 绕过并刷新
    response_cache_enabled: bool = True
    response_cache_ttl_s: float = 3600.0
    # P1-1 并行化：依赖无关的计划步骤按「波次」并发执行（线程池）。
    # <=1 表示关闭并行、退化为逐步骤顺序执行（与原行为一致）。
    parallel_executor_workers: int = 4
    # AgentState 检查点目录（可回放/可续跑）
    checkpoint_dir: str = "data/checkpoints"
    # 长期记忆（跨会话）JSONL 落盘路径（PG 可用时走 PG；此处是可测试/可迁移的兜底位置）
    long_term_path: str = "data/long_term.jsonl"

    @property
    def use_mock_llm(self) -> bool:
        return self.mock_llm or not self.llm_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
