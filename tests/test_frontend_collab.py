"""#1/#6 前端契约（源码级）：鉴权桥接 + 协作骨架 + E2E 脚手架。

沿用 test_clarify_loop.py / test_ui.py 的源码级断言范式，防「只改后端漏前端」。
这里的每条用例都对应一个**用户可见的后果**，不是形式检查：

- 鉴权头没注入 → 开了 AUTH_ENABLED 后前端整站 401，且用户无从输入 key；
- 401 没转成 AuthError → 登录框永不弹出（用户只看到"连接失败"）；
- 登录成功不重试 → 用户填完 key 还得自己重问一遍；
- ShareBar 没被渲染 → 协作骨架是死代码；
- E2E 脚手架没声明 → "有 Playwright"只是口头声明（本机装不上时尤其要钉住配置存在）。

浏览器级回归见 web/e2e/*.spec.ts（需 `npx playwright install`，离线环境跑不了，
故此处的源码级契约是离线能守的那一层）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent / "web"


def _read(rel: str) -> str:
    return (_WEB / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# #1 鉴权桥接
# --------------------------------------------------------------------------- #

def test_auth_module_injects_api_key_and_maps_401():
    """auth.ts：key 存同源 localStorage、注入 X-API-Key、401/503 转 AuthError。"""
    auth = _read("src/lib/auth.ts")
    assert "da_api_key" in auth, "API Key 的存储键必须稳定（换键等于让已登录用户掉线）"
    assert "X-API-Key" in auth, "必须注入 X-API-Key 头"

    # authHeaders 无 key 时返回空对象：后端 AUTH 关闭时不能凭空加头。
    block = auth.split("export function authHeaders", 1)[1].split("}", 1)[0]
    assert "X-API-Key" in block

    # 401 与 503 都要转 AuthError —— 503 是「开了鉴权但没配 key」的配置错误，
    # 前端同样需要让用户输入 key，否则用户看到的是无解的服务错误。
    err_block = auth.split("export function maybeAuthError", 1)[1]
    assert "401" in err_block and "503" in err_block, "401/503 都必须转成 AuthError"
    assert "AuthError" in err_block


def test_api_client_actually_applies_auth_headers():
    """光定义 authHeaders 不算数——api.ts 必须在真实请求上用它。"""
    api = _read("src/lib/api.ts")
    assert "authHeaders" in api, "api.ts 必须导入并调用 authHeaders"
    # 至少两处：分析请求 + 上传请求（两处都要带 key）。
    assert api.count("...authHeaders()") >= 2, "所有出站请求都应携带鉴权头"
    assert "maybeAuthError" in api, "响应侧必须把 401/503 转成 AuthError"
    assert re.search(r"export\s*\{[^}]*AuthError", api), "AuthError 要透出给 App 消费"


def test_app_gate_flow_retries_the_blocked_query():
    """闭环三件事：捕获 AuthError → 弹框 + 暂存参数 → 登录后重试原问题。"""
    app = _read("src/App.tsx")
    assert "AuthGate" in app, "登录弹窗必须真的挂载"
    assert "instanceof AuthError" in app, "必须捕获 AuthError（否则弹框永不触发）"

    catch_block = app.split("instanceof AuthError", 1)[1].split("return;", 1)[0]
    assert "pendingRef.current" in catch_block, "被 401 拦下的问题必须暂存，否则用户要重问"
    assert "setAuthOpen(true)" in catch_block, "必须弹出登录框"

    authed = app.split("const handleAuthed", 1)[1].split("}, [", 1)[0]
    assert "pendingRef.current" in authed, "登录成功后必须取回暂存参数"
    assert "send(" in authed, "登录成功必须重试刚才被拦下的分析"


# --------------------------------------------------------------------------- #
# #6 协作骨架 + 交付包
# --------------------------------------------------------------------------- #

def test_share_bar_has_three_collab_actions():
    """分享/评论/权限三个入口都要存在且可被无障碍定位（E2E 靠 aria-label 找）。"""
    bar = _read("src/components/ShareBar.tsx")
    for label in ("分享", "评论", "权限"):
        assert f'aria-label="{label}"' in bar, f"缺少「{label}」按钮"


def test_share_bar_and_export_are_rendered_in_chat():
    """骨架必须被渲染，且导出链接指向真实端点（防写死死端点）。"""
    chat = _read("src/components/ChatMessage.tsx")
    assert "ShareBar" in chat, "ShareBar 必须真的被渲染，否则是死代码"
    assert "/api/v1/chat/analyze/export/" in chat, "导出按钮必须指向真实导出端点"
    assert "format=zip" in chat, "默认导出交付包（zip）"


# --------------------------------------------------------------------------- #
# E2E 脚手架（配置存在性——离线环境装不上浏览器，但配置漂移必须能被发现）
# --------------------------------------------------------------------------- #

def test_playwright_harness_is_declared():
    assert (_WEB / "playwright.config.ts").exists(), "缺少 Playwright 配置"
    cfg = _read("playwright.config.ts")
    assert "./e2e" in cfg, "Playwright 必须扫 e2e 目录"

    specs = list((_WEB / "e2e").glob("*.spec.ts"))
    assert specs, "e2e 目录下必须有用例文件"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in specs)
    assert "/api/v1/chat/analyze/export/" in blob, "E2E 必须覆盖导出主链路"


def test_package_json_declares_e2e_dependency_and_script():
    pkg = json.loads(_read("package.json"))
    assert "@playwright/test" in pkg.get("devDependencies", {}), \
        "@playwright/test 必须在 devDependencies（否则 CI 跑不了 E2E）"
    assert pkg.get("scripts", {}).get("test:e2e"), "缺少 npm run test:e2e 入口"


def test_e2e_test_timeout_exceeds_every_explicit_expect_timeout():
    """**测试级 timeout 必须大于 spec 里显式写的 expect timeout，否则那句 timeout 是死的。**

    真实缺陷（已修）：config 里 `timeout: 30_000`，而 `export.spec.ts` 写
    `expect(...).toBeVisible({ timeout: 60_000 })` 想等一轮分析跑完 ——
    但测试级 30s 上限**先生效**，60s 永远等不到，用例在 30s 被掐断。
    徽标用例各自 `setTimeout(180_000)` 绕开了，`export.spec.ts` **没绕** → 潜伏的必现 flake。

    这类"写了但从未生效"的配置与本文件其它契约同族：靠真跑发现不了（跑得快时照样绿），
    必须静态钉住。
    """
    import re

    def _strip_ts_comments(text: str) -> str:
        """先剥注释再匹配——否则会读到**文档注释里举的例子**。
        （本测试第一版就栽在这：注释里写的 `{ timeout: 60_000 }` 被当成了实际配置值。）"""
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        return re.sub(r"//[^\n]*", "", text)

    cfg = _strip_ts_comments(_read("playwright.config.ts"))
    m = re.search(r"timeout:\s*([0-9_]+)", cfg)
    assert m, "playwright.config.ts 里找不到 timeout"
    test_timeout = int(m.group(1).replace("_", ""))

    worst = 0
    for p in (_WEB / "e2e").glob("*.spec.ts"):
        body = _strip_ts_comments(p.read_text(encoding="utf-8"))
        for raw in re.findall(r"timeout:\s*([0-9_]+)", body):
            worst = max(worst, int(raw.replace("_", "")))
    assert worst, "spec 里没有任何显式 timeout —— 本契约失去意义，请复核"
    assert test_timeout >= worst, (
        f"测试级 timeout={test_timeout}ms 小于 spec 里的 expect timeout={worst}ms "
        f"→ 那句 timeout 永远不会生效（用例会先被测试级上限掐断）"
    )


# --------------------------------------------------------------------------- #
# E3/E4 徽标：mode/iteration/质量门禁的 UI 呈现
#    后端早就下发了 iteration / quality_issues，前端此前没人消费——
#    这类"字段发了但没人看"比崩溃更隐蔽：界面一切正常，信息静默丢失。
# --------------------------------------------------------------------------- #

_API_SRC = lambda: _read("src/lib/api.ts")  # noqa: E731


def test_event_type_declares_iteration_and_quality_fields():
    api = _API_SRC()
    assert "interface IterationInfo" in api, "缺少 iteration 的类型声明"
    assert "interface QualityIssue" in api, "缺少 quality_issues 的类型声明"
    event_block = api.split("export interface AgentEvent", 1)[1].split("}", 1)[0]
    assert "iteration?" in event_block, "AgentEvent 必须声明 iteration"
    assert "quality_issues?" in event_block, "AgentEvent 必须声明 quality_issues"


def test_iteration_labels_cover_every_backend_kind():
    """E3 的五类 kind 都必须有中文文案，否则界面会退化成光秃秃的 '增量'。"""
    api = _API_SRC()
    block = api.split("export function iterationLabel", 1)[1].split("}", 1)[0]
    for kind in ("date_change", "granularity", "drilldown", "filter"):
        assert kind in block, f"iterationLabel 缺少 {kind} 的文案"


def test_frontend_field_names_match_backend_payload():
    """跨栈契约：前端读的键名必须与后端 SSE 下发的键名一致。

    任一侧改名而另一侧没跟上 → 徽标静默不显示（不报错、不崩溃），
    只有这条用例能把这种"静默丢信息"钉住。
    """
    chat_src = (
        Path(__file__).resolve().parent.parent / "app" / "api" / "routes" / "chat.py"
    ).read_text(encoding="utf-8")
    assert '"iteration"' in chat_src, "后端 SSE 未下发 iteration"
    assert '"quality_issues"' in chat_src, "后端 SSE 未下发 quality_issues"

    api = _API_SRC()
    assert "iteration" in api and "quality_issues" in api, "前端未消费这两个字段"


def test_run_badges_rendered_and_block_first():
    assert (_WEB / "src/components/RunBadges.tsx").exists(), "缺少徽标组件"

    chat = _read("src/components/ChatMessage.tsx")
    assert "RunBadges" in chat, "徽标组件必须真的被渲染（否则是死代码）"

    badges = _read("src/components/RunBadges.tsx")
    assert "iteration" in badges, "徽标必须读 iteration"
    assert "quality_issues" in badges, "徽标必须读 quality_issues"
    # BLOCK 意味着"结论必然错"，排序上必须先出现——不能混在 ANNOTATE 后面。
    assert "BLOCK" in badges, "必须对 BLOCK 单独处理（最显眼）"

    # 事件是原样透传的：App 若逐字段重建事件对象，新字段会被悄悄丢掉。
    app = _read("src/App.tsx")
    assert "events: [...(m.events ?? []), ev]" in app, \
        "事件必须原样透传，逐字段重建会静默吞掉新字段"
