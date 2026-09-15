"""API request / response schemas."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    query: str = Field(..., description="用户的分析请求")
    session_id: Optional[str] = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    force_full_rerun: bool = Field(
        default=False, description="显式全链：即使命中「基于上一结果」也不走增量（E3/02）")
    clarification_answer: Optional[str] = Field(
        default=None, description="对上一轮澄清提问的回答（CLARIFY/01）；也可直接把答案写在 query 里")


class AnalyzeResponse(BaseModel):
    session_id: str
    status: str
    mode: str = "full"
    iteration: Optional[dict[str, Any]] = None
    # DEGRADE/01：本轮是否发生 LLM 降级（静默兜底必须对调用方可见）
    degraded: bool = False
    llm_fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    # E4/04 质量门禁：确定性检查给出的数据质量问题（code/severity/detail）
    quality_issues: list[dict[str, Any]] = Field(default_factory=list)
    # CLARIFY/01：本轮的澄清提问（status=CLARIFY 时非空）
    clarification: Optional[dict[str, Any]] = None
    # INTERVIEW/01 ④：本次是否由请求级缓存命中（避免误以为是新一轮分析）
    cache_hit: bool = False
    report: str = ""
    objective: str = ""
    plan_steps: list[str] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    # D51：指标卡（已按唯一口径归一为 {name, value, comparison}）
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    reflection_decision: Optional[str] = None
    confidence: Optional[float] = None
    error: Optional[str] = None


class StreamEvent(BaseModel):
    status: str
    message: str = ""
    partial: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    text: Optional[str] = None
    source: str = "inline"
    file_path: Optional[str] = None


class IngestResponse(BaseModel):
    chunks: int
    source: str


# --- Knowledge base (RAG) 管理接口 ---
class KBSource(BaseModel):
    """单个知识来源（按 source 聚合的分块数）。"""
    source: str
    chunks: int


class KBStatus(BaseModel):
    """知识库整体状态（管理面板用）。"""
    enabled: bool = True
    backend: str = "sqlite"      # sqlite | milvus
    total_chunks: int = 0
    sources: int = 0


class KBListResponse(BaseModel):
    status: KBStatus
    documents: list[KBSource] = Field(default_factory=list)


class KBHit(BaseModel):
    source: str
    text: str
    score: float = 0.0


class KBSearchResponse(BaseModel):
    query: str
    hits: list[KBHit] = Field(default_factory=list)


class KBDeleteResponse(BaseModel):
    source: str
    deleted: int = 0


# --- Data sources（数据库连接配置）---
class DataSourceConn(BaseModel):
    """一个可寻址的数据库连接配置。"""
    name: str
    dialect: str
    url: str                     # 已脱敏（密码打码），前端直接展示
    readonly: bool = True


class DataSourceListResponse(BaseModel):
    sources: list[DataSourceConn] = Field(default_factory=list)


# --- 多知识库（KB）：一个知识库是一份独立的 RAG 资产 ---
class KbBase(BaseModel):
    id: str
    name: str
    description: str = ""
    kb_type: str = "general"        # general（通用） | website（网站）
    visibility: str = "private"     # private | public
    owner: str = "本地用户"
    created_at: str = ""
    updated_at: str = ""
    documents: int = 0
    chunks: int = 0


class KbBaseListResponse(BaseModel):
    bases: list[KbBase] = Field(default_factory=list)
    backend: str = "sqlite"
    total_chunks: int = 0


class KbCreateRequest(BaseModel):
    name: str
    description: str = ""
    kb_type: str = "general"
    visibility: str = "private"


class KbUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    kb_type: Optional[str] = None
    visibility: Optional[str] = None


class KbDocument(BaseModel):
    id: str
    kb_id: str
    name: str
    source: str
    doc_type: str = "file"          # file | text | website
    mime: str = ""
    bytes: int = 0
    chunks: int = 0
    status: str = "ready"
    created_at: str = ""
    updated_at: str = ""


class KbDocumentListResponse(BaseModel):
    kb_id: str
    documents: list[KbDocument] = Field(default_factory=list)


class KbTextRequest(BaseModel):
    text: str
    name: str = "粘贴文本"


class KbWebsiteRequest(BaseModel):
    url: str
    title: Optional[str] = None


class KbIngestResponse(BaseModel):
    ok: bool = True
    document: KbDocument
    hint: Optional[str] = None


# --- 文件库：企业文件管理（目录树 + 文件）---
class FsNode(BaseModel):
    id: str
    parent_id: str = ""
    name: str
    is_dir: bool = False
    bytes: int = 0
    mime: str = ""
    size_label: str = ""
    created_at: str = ""
    updated_at: str = ""
    path: Optional[str] = None      # 搜索结果携带完整路径


class FsTreeResponse(BaseModel):
    nodes: list[FsNode] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class FsListResponse(BaseModel):
    parent_id: str = ""
    breadcrumb: list[FsNode] = Field(default_factory=list)
    nodes: list[FsNode] = Field(default_factory=list)


class FsFolderRequest(BaseModel):
    parent_id: str = ""
    name: str


class FsRenameRequest(BaseModel):
    name: str


class FsSearchResponse(BaseModel):
    query: str
    results: list[FsNode] = Field(default_factory=list)


class FsDeleteResponse(BaseModel):
    ok: bool = True
    deleted: int = 0


# P0-4：降级事件按阶段聚合后的可读摘要
class DegradedStage(BaseModel):
    stage: str = ""
    label: str = ""                 # 人话阶段名，如"证据分析"
    impact: str = ""                # 该阶段降级对结论的影响
    kind: str = "unknown"           # region_blocked / auth_failed / ...
    summary: str = ""               # 原因摘要
    action: str = ""                # 建议动作
    code: str = ""                  # HTTP 码（若有）


class DegradationSummary(BaseModel):
    """把原始 llm_fallbacks 翻译成"哪一级降了 / 为什么 / 影响什么"。"""

    degraded: bool = False
    count: int = 0
    severity: str = "none"          # none | partial | total
    headline: str = ""
    stages: list[DegradedStage] = []
    impacts: list[str] = []


class HealthResponse(BaseModel):
    status: str = "ok"
    mock_llm: bool = False          # 配置意图（历史字段，语义不变）
    data_source: str = ""
    # DEGRADE/01：配置意图 vs 观测事实必须分开——D18 的坑正是"只看配置值"
    llm_mode: str = "real"          # "mock" | "real"
    llm_degraded: bool = False      # 观测：最近一次真实 LLM 调用是否降级
    llm_fallbacks_total: int = 0    # 进程内降级累计
    llm_last_error: Optional[str] = None
    # P0-4：按阶段聚合 + 归因（不再让调用方自己解析原始事件流）
    llm_degraded_stages: list[str] = []      # 如 ["context","planner","analyst"]
    llm_degradation: Optional[DegradationSummary] = None
    llm_fallbacks_by_stage: dict[str, int] = {}   # 监控告警用
    # E7/01 命名数据源：**只回源名**（绝不回 DSN/密码）
    data_sources: list[str] = []


class AttachmentInfo(BaseModel):
    """单个附件的结构化摘要（供前端展示 + LLM 上下文）。"""

    name: str
    kind: str = "file"              # table | text | image | file
    rows: Optional[int] = None
    columns: Optional[list[str]] = None
    sample: Optional[list[dict[str, Any]]] = None
    excerpt: Optional[str] = None
    bytes: int = 0
    # P2-2：图片类附件落地到磁盘的绝对路径（前端可据此渲染预览；image_analyze 工具据此读取原图）
    path: Optional[str] = None


class UploadResponse(BaseModel):
    ok: bool = True
    session_id: str
    attachment: AttachmentInfo
    hint: Optional[str] = None


class AttachmentListResponse(BaseModel):
    session_id: str
    attachments: list[AttachmentInfo] = Field(default_factory=list)


class LLMProbeResponse(BaseModel):
    """GET /health/llm：主动探测真实可达性（不降级、不计入降级事件）。"""
    reachable: bool = False
    model: str = ""
    latency_ms: Optional[float] = None
    error: Optional[str] = None
