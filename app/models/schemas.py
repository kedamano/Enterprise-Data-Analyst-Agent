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
    skill_ids: list[str] = Field(
        default_factory=list,
        description="本次对话显式勾选的技能 id（Skills）：仅这些技能的正文注入本轮提示词")


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
    origin: str = "env"          # env（服务端配置）| local（页面「新建连接」落盘）


class DataSourceListResponse(BaseModel):
    sources: list[DataSourceConn] = Field(default_factory=list)


class DataSourceTestRequest(BaseModel):
    """「新建连接」表单字段（Navicat 式）。dialect=sqlite 时用 path，其余用 host 系列。"""
    dialect: str                 # sqlite | mysql | postgresql
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    path: str = ""


class DataSourceTestResponse(BaseModel):
    ok: bool
    message: str = ""
    error: str = ""


class DataSourceCreateRequest(DataSourceTestRequest):
    name: str = ""


class DataSourceDeleteResponse(BaseModel):
    ok: bool
    name: str


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


class KbPreviewResponse(BaseModel):
    """知识库文档预览响应：已入库文档按分块拼回的全文（截断到 ~50KB）。"""
    kb_id: str
    doc_id: str
    name: str
    doc_type: str = "file"
    source: str = ""
    previewable: bool = True
    truncated: bool = False
    text: Optional[str] = None
    chars: int = 0
    chunks: int = 0
    reason: Optional[str] = None
    #: 渲染模式（按 source 扩展名推断）：text / code / markdown / table / unsupported
    render: str = "text"


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


class FsPreviewResponse(BaseModel):
    """文件预览响应：预览类文件返回全文或渲染模式，非预览类文件返回不可预览原因。"""
    id: str
    name: str
    mime: str = ""
    previewable: bool
    truncated: bool = False
    text: Optional[str] = None
    chars: int = 0
    encoding: Optional[str] = None
    reason: Optional[str] = None
    #: 渲染模式：frontend 据此选择渲染器。
    #: text / code / markdown / table / image / pdf / unsupported
    render: str = "text"


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
    # 知识库后端：同 DEGRADE/01 的思路——KNOWLEDGE_ENABLED 是配置意图，
    # 这里报**观测事实**（真连上 Milvus 了吗）。三态区分"没配"与"配了但挂了"，
    # 否则"Milvus 服务没起"会与"压根没配"表现成同一个 sqlite，静默失效查不出来。
    knowledge_backend: str = "sqlite"          # milvus | sqlite | sqlite_fallback
    milvus_error: Optional[str] = None         # 仅 sqlite_fallback 时有值


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


# --------------------------------------------------------------------------- #
# AUTH/02 用户账号体系
# --------------------------------------------------------------------------- #
class UserPublic(BaseModel):
    """对外的用户视图。

    **绝不包含** `password_hash` / `wechat_openid` / `wechat_unionid` ——
    前者是凭证材料，后两者是可被用来关联微信身份的外部标识，
    前端展示用不到，一次都不该下发。
    """
    id: str
    username: str
    email: str = ""
    phone: str = ""
    display_name: str = ""
    bio: str = ""
    role: str = "analyst"
    status: str = "active"
    tenant: str = ""
    avatar: str = ""              # 文件名或外链；前端拼 /api/v1/auth/avatar/<name>
    avatar_version: int = 0
    source: str = "password"      # password | wechat
    created_at: str = ""
    updated_at: str = ""
    last_login_at: str = ""


class AuthSessionResponse(BaseModel):
    ok: bool = True
    token: str
    expires_at: str = ""
    user: UserPublic
    created: bool = False         # 本次是否新建了账号（微信首次扫码 / 注册）


class AuthMeResponse(BaseModel):
    """GET /auth/me：**刻意不返回 401**。

    前端每次启动都要问一次"我是谁"，用 200 + `authenticated=false` 表达
    "没登录"比让它去区分 401 与网络错误更省事，也避免控制台刷错误。
    """
    authenticated: bool = False
    user: Optional[UserPublic] = None
    # 服务端鉴权是否**强制**（auth_enabled）。false 时未登录也能用，只是身份是匿名的。
    enforcement: bool = False
    user_auth_enabled: bool = True


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str = ""
    display_name: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class SessionInfo(BaseModel):
    created_at: str = ""
    expires_at: str = ""
    user_agent: str = ""
    ip: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo] = Field(default_factory=list)


class RoleInfo(BaseModel):
    role: str
    label: str
    summary: str = ""
    rank: int = 0
    permissions: list[str] = Field(default_factory=list)
    permission_labels: list[str] = Field(default_factory=list)


class RoleListResponse(BaseModel):
    roles: list[RoleInfo] = Field(default_factory=list)


class UserListResponse(BaseModel):
    users: list[UserPublic] = Field(default_factory=list)
    total: int = 0


class AdminUpdateUserRequest(BaseModel):
    """管理员改他人：角色 / 状态。改自己会走 users.set_role/set_status 的守卫。"""
    role: Optional[str] = None
    status: Optional[str] = None


class WeChatStatus(BaseModel):
    configured: bool = False
    simulate_available: bool = False
    missing: list[str] = Field(default_factory=list)
    redirect_uri: str = ""


class WeChatQrResponse(BaseModel):
    state: str
    qr_url: str = ""              # 微信官方 qrconnect 地址（前端用 iframe 渲染）
    expires_in: int = 600
    poll_interval_s: int = 2
    configured: bool = False
    simulated: bool = False       # true = 未配置凭据，二维码不来自微信


class WeChatPollResponse(BaseModel):
    status: str                   # pending | confirmed | expired | error
    simulated: bool = False
    token: str = ""
    expires_in: float = 0
    user_id: str = ""
    message: str = ""


class WeChatSimulateRequest(BaseModel):
    state: str
    nickname: str = ""


class AuthConfigResponse(BaseModel):
    """前端一次性拿到所有"要不要显示、能不能点"的依据。"""
    user_auth_enabled: bool = True
    enforcement: bool = False           # auth_enabled：未登录是否被拦
    registration_open: bool = True
    password_min_length: int = 8
    has_users: bool = False             # false 时提示"第一个注册者将成为管理员"
    wechat: WeChatStatus = Field(default_factory=WeChatStatus)


# --------------------------------------------------------------------------- #
# Feedback Loop — user ratings / comments on analysis results
# --------------------------------------------------------------------------- #
class FeedbackIn(BaseModel):
    rating: int                                                   # -1 or 1
    comment: str = ""                                             # optional text (≤500 chars)
    stage_breakdown: Optional[dict[str, float]] = None            # optional stage scores


class FeedbackOut(BaseModel):
    id: int
    session_id: str
    rating: int
    comment: Optional[str] = None
    report_snippet: Optional[str] = None
    stage_breakdown: Optional[dict[str, float]] = None
    created_at: str
