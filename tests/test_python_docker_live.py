"""Live Docker-sandbox suite for ``python_analysis`` (requires a running Docker).

These tests exercise the ``python_sandbox_mode=docker`` executor against the
``da-python-sandbox`` image (DeepAnalyze-style isolation). Skipped when Docker
or the image is unavailable, so the default (offline) suite stays independent.
"""
from __future__ import annotations

import shutil

import pytest

from app.config import Settings, get_settings
from app.core.tools.python_tool import run as py_run


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    import subprocess
    try:
        r = subprocess.run(["docker", "image", "inspect", "da-python-sandbox"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="Docker / 沙箱镜像不可用")


@pytest.fixture
def docker_mode(monkeypatch):
    monkeypatch.setenv("PYTHON_SANDBOX_MODE", "docker")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_docker_benign_analysis_runs(docker_mode, tmp_path):
    code = "print(_json.dumps({'rows': 0 if df is None else int(df.shape[0]), 'pi': 3.14}))"
    res = py_run({"code": code}, workdir=str(tmp_path))
    assert res["ok"], res.get("stderr") or res.get("error")
    assert "pi" in res.get("stdout", "")


def test_docker_blocks_network_despite_code_trying(docker_mode, tmp_path):
    """--network none：容器内无法联网。

    走 pandas.read_csv(url) 触发底层 urllib（用户代码不 import urllib，AST 守卫
    放行），真实验证容器层断网而非仅靠模块黑名单。
    """
    code = (
        "try:\n"
        "    pd.read_csv('http://10.255.255.1/nope.csv', timeout=2)\n"
        "    print('UNREACHABLE-NET-OPEN')\n"
        "except Exception as e:\n"
        "    print('NET-BLOCKED:', type(e).__name__)\n"
    )
    res = py_run({"code": code}, workdir=str(tmp_path))
    assert res["ok"], res.get("stderr") or res.get("error")
    assert "NET-BLOCKED" in res.get("stdout", ""), "容器断网应生效"
    assert "UNREACHABLE" not in res.get("stdout", "")


def test_docker_read_only_rootfs_blocks_host_write(docker_mode, tmp_path):
    """--read-only + tmpfs：写容器 / 根应失败，写 /tmp 应成功（tmpfs）。"""
    code = (
        "attempts = {}\n"
        "try:\n"
        "    open('/etc/evil.txt', 'w').write('x')\n"
        "    attempts['root_write'] = 'OPEN'\n"
        "except Exception as e:\n"
        "    attempts['root_write'] = 'BLOCKED:' + type(e).__name__\n"
        "try:\n"
        "    open('/tmp/ok.txt', 'w').write('y')\n"
        "    attempts['tmp_write'] = 'OK'\n"
        "except Exception as e:\n"
        "    attempts['tmp_write'] = 'FAIL:' + type(e).__name__\n"
        "print(_json.dumps(attempts))"
    )
    res = py_run({"code": code}, workdir=str(tmp_path))
    assert res["ok"], res.get("stderr") or res.get("error")
    import json as _j
    parsed = _j.loads(res["stdout"])
    assert "BLOCKED" in parsed["root_write"], "根文件系统应只读"
    assert parsed["tmp_write"] == "OK", "tmpfs 应可写"


def test_docker_artifact_roundtrip(docker_mode, tmp_path):
    """产物写进 /work（挂载的 wd）→ 宿主能读到 PNG。"""
    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.figure(figsize=(2, 2)); plt.plot([1, 2, 3]); plt.title('t')\n"
        "plt.savefig(WORKDIR + '/chart.png')\n"
        "print(_json.dumps({'saved': True}))"
    )
    res = py_run({"code": code}, workdir=str(tmp_path))
    assert res["ok"], res.get("stderr") or res.get("error")
    assert any("chart.png" in a for a in res.get("artifacts", [])), "PNG 应回传宿主"
