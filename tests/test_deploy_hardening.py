"""E7/01 部署硬化：生产镜像 / 容器冒烟 / 安全说明。

Spec: docs/specs/E7/01-multi-source-deploy.md
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD = ROOT / "Dockerfile.prod"
SMOKE = ROOT / "scripts" / "smoke_container.sh"
DEPLOY_DOC = ROOT / "docs" / "部署上线.md"


def test_prod_dockerfile_is_multistage_and_hardened():
    text = PROD.read_text(encoding="utf-8")
    assert len(re.findall(r"^FROM ", text, re.M)) >= 2, "必须是多阶段构建（依赖与运行分离）"
    assert re.search(r"^USER \w+", text, re.M), "必须以非 root 运行"
    assert "HEALTHCHECK" in text, "必须带健康检查"
    assert "COPY tests/" not in text and "COPY . " not in text, \
        "不得整仓 COPY（会把 tests/.git/node_modules 带进镜像）"


def test_prod_image_drops_heavy_embedding_stack_by_default():
    """<2GB 目标的关键：默认不装 sentence-transformers（会拉 torch）。"""
    text = PROD.read_text(encoding="utf-8")
    assert "sentence-transformers" in text, "必须显式说明为何排除它"
    assert "WITH_EMBEDDINGS" in text, "需要向量检索时应可通过 build-arg 打开"
    assert re.search(r"ARG WITH_EMBEDDINGS=0", text), "默认必须是关（否则 <2GB 不成立）"


def _git_bash() -> str | None:
    """找**Git Bash**（不是 WSL 的 bash.exe：后者找不到 /bin/bash 会以 1 退出）。"""
    import os
    import shutil

    cands = [os.path.join(os.environ.get("EXEPATH", ""), "bin", "bash.exe"),
             os.path.join(os.environ.get("EXEPATH", ""), "usr", "bin", "bash.exe"),
             r"C:\Program Files\Git\bin\bash.exe",
             r"C:\Program Files (x86)\Git\bin\bash.exe",
             shutil.which("bash")]
    for c in cands:
        if c and Path(c).exists() and "system32" not in str(c).lower():
            return str(c)
    return None


def test_smoke_script_skip_is_a_distinct_state():
    """「跳过」必须可识别：既不能当通过（假绿），也不能当失败（误报）。"""
    import os

    bash = _git_bash()
    if bash is None:
        import pytest

        pytest.skip("未找到 Git Bash —— 显式跳过（不计入通过），而非静默放行")

    # 继承宿主环境（Windows 上 bash 需要 SYSTEMROOT 等，不能给残缺 env）
    env = {**os.environ, "SMOKE_FORCE_SKIP": "1"}
    r = subprocess.run([bash, str(SMOKE)], capture_output=True, text=True,
                       env=env, timeout=60, encoding="utf-8", errors="replace")
    assert r.returncode == 2, f"跳过应返回 2，实际 {r.returncode}: {r.stdout}"
    out = (r.stdout or "") + (r.stderr or "")
    assert "SKIP" in out
    assert "不计入通过" in out


def test_smoke_script_covers_build_health_analyze_size():
    text = SMOKE.read_text(encoding="utf-8")
    for token in ("docker build", "/api/v1/health", "/api/v1/chat/analyze", "镜像体积"):
        assert token in text, f"冒烟必须覆盖：{token}"


def test_deploy_doc_has_security_section():
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    for token in ("鉴权", "HTTPS", "反向代理"):
        assert token in text, f"部署文档缺少安全说明：{token}"
