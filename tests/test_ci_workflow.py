"""CI 工作流契约（源码级）：守住"配了、但从未生效"这一类静默缺陷。

背景：本项目**没有 git 仓库**，`.github/workflows/ci.yml` 从未在 Actions 上真正跑过，
所以"CI 里写了"≠"CI 里会执行"。一次静态审计就查出 3 处**不会让 CI 变红**的静默失效：

1. **e2e 的 Playwright 报告 artifact 永远为空** —— `web/playwright.config.ts` 只配了
   `reporter: [["list"]]`，从不生成 `playwright-report/`；而 ci.yml 上传该目录，
   `if-no-files-found` 默认只 `warn` → 静默上传空。
   后果：**失败时没有任何可视化报告可查**，排查只能靠日志。
2. **PostgreSQL live 在 CI 里从不运行** —— offline job 用 `--ignore` 排除了它，
   `live` job 又没有 PG service container。后果：**PG 方言回归只在本机存在**，
   换台机器/换人接手就静默丢失覆盖。
3. **Redis live 在 CI 里从不运行** —— 同上。

这三条都属于"少跑/传空但全绿"的缺陷，靠 CI 自己永远发现不了，必须用契约测试钉住。
浏览器级真跑见 `web/e2e/*.spec.ts`（需 `npx playwright install`，离线环境跑不了，
故此处是离线能守的那一层）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
_WEB_CONFIG = Path(__file__).resolve().parent.parent / "web" / "playwright.config.ts"

pytestmark = pytest.mark.skipif(not _CI.exists(), reason="无 CI 配置（跳过））")


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _named(job: dict, name: str) -> dict:
    for s in _steps(job):
        if s.get("name") == name:
            return s
    raise AssertionError(f"未找到步骤 {name!r}；现有步骤："
                         f"{[s.get('name') for s in _steps(job)]}")


# --------------------------------------------------------------------------- #
# 骨架
# --------------------------------------------------------------------------- #
def test_workflow_is_valid_yaml_with_expected_jobs(ci):
    assert {"test", "e2e", "live", "image"} <= set(ci["jobs"]), ci["jobs"].keys()


def test_offline_job_excludes_real_dependency_suites(ci):
    """离线门禁必须显式排除"需真实外部依赖"的四个文件，否则会因余额/凭据变红。"""
    run = _steps(ci["jobs"]["test"])[0].get("run") or ""
    blob = " ".join(str(s.get("run") or "") for s in _steps(ci["jobs"]["test"]))
    for f in ("test_agent_real.py", "test_pg_live.py",
              "test_redis_live.py", "test_milvus_live.py"):
        assert f"--ignore=tests/{f}" in blob, f"离线 job 未排除 {f}"


# --------------------------------------------------------------------------- #
# 缺陷 1：Playwright 报告必须真的会被生成，且上传路径与生成路径一致
# --------------------------------------------------------------------------- #
def test_e2e_artifact_path_matches_a_reporter_that_actually_emits_it(ci):
    """上传 `playwright-report/` 的前提是**真的启用 html reporter**。

    只看 config 不够：CI 用 `--reporter=` 覆盖 config，所以两边都要查。
    """
    e2e = ci["jobs"]["e2e"]
    run_e2e = _named(e2e, "Run E2E")["run"]
    upload = _named(e2e, "Upload Playwright report")["with"]
    path = str(upload["path"])

    assert "playwright-report/" in path, path

    config_has_html = 'reporter' in _WEB_CONFIG.read_text(encoding="utf-8") \
        and '"html"' in _WEB_CONFIG.read_text(encoding="utf-8")
    ci_has_html = "html" in run_e2e.replace(" ", "")
    assert config_has_html or ci_has_html, (
        "上传了 playwright-report/ 却没有任何地方启用 html reporter → artifact 永远为空。"
        f"Run E2E={run_e2e!r}"
    )


def test_e2e_uploads_traces_for_failure_debugging(ci):
    """仅传 html 报告不够：失败时的 trace/截图在 `test-results/`（默认 outputDir）。"""
    upload = _named(ci["jobs"]["e2e"], "Upload Playwright report")["with"]
    assert "test-results/" in str(upload["path"]), upload["path"]


# --------------------------------------------------------------------------- #
# 缺陷 2/3：live job 的 service container 必须覆盖它实际要跑的套件
# --------------------------------------------------------------------------- #
def test_live_job_declares_every_service_its_suites_need(ci):
    live = ci["jobs"]["live"]
    services = set(live.get("services") or {})
    assert {"mysql", "postgres", "redis"} <= services, services


def test_live_job_actually_runs_the_pg_and_redis_suites(ci):
    """有 service 但没 step、或没 service 但 step 会自 skip —— 两种都是"假覆盖"。"""
    live = ci["jobs"]["live"]
    blob = " ".join(str(s.get("run") or "") for s in _steps(live))
    for suite in ("tests/test_mysql_live.py", "tests/test_pg_live.py",
                  "tests/test_redis_live.py", "tests/test_milvus_live.py",
                  "tests/test_profile_cross_dialect.py"):
        assert suite in blob, f"live job 未运行 {suite}"


def test_pg_live_step_supplies_a_dsn_matching_the_service_credentials(ci):
    """service 用 da/da/da_agent，DSN 就必须对得上（否则用例自 skip → 又变成假覆盖）。"""
    live = ci["jobs"]["live"]
    pg = live["services"]["postgres"]["env"]
    step = _named(live, "Live PostgreSQL (真实库：长期记忆 JSONL→PG 迁移 + 回退)")
    dsn = str(step["env"]["POSTGRES_DSN"])
    assert pg["POSTGRES_USER"] in dsn and pg["POSTGRES_PASSWORD"] in dsn
    assert pg["POSTGRES_DB"] in dsn
    assert str(live["services"]["postgres"]["ports"][0]).endswith("5432:5432")


def test_mysql_live_step_dsn_matches_service(ci):
    live = ci["jobs"]["live"]
    ms = live["services"]["mysql"]
    dsn = str(_named(live, "Live MySQL (真实库：只读守卫 + 文件原语拦截 + 数据权限)")["env"]["MYSQL_DSN"])
    assert str(ms["ports"][0]).endswith("3306:3306")
    assert "3306" in dsn and ms["env"]["MYSQL_DATABASE"] in dsn


def test_milvus_server_is_not_silently_dropped(ci):
    """CI 只跑 Milvus Lite 是有意为之 —— 但必须在配置里写明理由，避免被当成漏接。"""
    text = _CI.read_text(encoding="utf-8")
    assert "Milvus Lite" in text
    assert "standalone" in text, "应就地注释说明为何 CI 不跑 Milvus 服务端"


# --------------------------------------------------------------------------- #
# image job：体积硬断言 + 冒烟
# --------------------------------------------------------------------------- #
def test_image_job_asserts_size_below_2gb(ci):
    steps = _steps(ci["jobs"]["image"])
    blob = " ".join(str(s.get("run") or "") for s in steps)
    assert "-lt 2147483648" in blob, "缺少 2GB 硬断言"
    assert "Dockerfile.prod" in blob


def test_image_smoke_asserts_a_metric_that_the_app_really_exposes(ci):
    """冒烟里 grep 的指标名必须真实存在（曾疑心 `http_requests_total` 不存在，实测存在）。

    真正的防线在 `app/infrastructure/observability/metrics.py`。
    """
    blob = " ".join(str(s.get("run") or "") for s in _steps(ci["jobs"]["image"]))
    assert "grep -q http_requests_total" in blob
    metrics_py = (Path(__file__).resolve().parent.parent
                  / "app" / "infrastructure" / "observability" / "metrics.py")
    assert 'metrics.inc("http_requests_total")' in metrics_py.read_text(encoding="utf-8")
