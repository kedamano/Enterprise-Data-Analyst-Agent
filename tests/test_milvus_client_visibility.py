"""配了 Milvus 却连不上时，必须**留痕**——不能静默降级成 SQLite。

**背景（真跑才暴露）**：本仓库的 Milvus Lite 示例路径原本少了 ``.db`` 后缀，
而 pymilvus 硬要求本地 URI 以 ``.db`` 结尾：

    ConnectionConfigException: uri: ./data/milvus_lite is illegal,
    needs start with [unix, http, https, tcp] or a local file endswith [.db]

后缀问题已由 ``_as_lite_path`` 修掉（见 ``test_milvus_live.py::test_lite_uri_is_isolated_file``）。
但 ``get_client()`` 里那个裸 ``except Exception: return None`` **还在**：
它把"URI 非法/服务连不上"这类**配置或连通性错误**，和"压根没配 Milvus"
（调用方回退 SQLite 属**预期**行为）**混成了同一个结果**——调用方无从区分。

这正是铁律 3（任何静默降级判失败）针对的模式，也是 DEGRADE/01 已经确立的规矩：
**配置错误要吵**。所以本文件只钉这一件事：连不上要留下带 URI 的日志。
"""
from __future__ import annotations

import logging

import pytest

from app.config import get_settings
from app.infrastructure.vectorstore import milvus as milvus_mod


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """清掉进程级 env 覆盖，跑完清缓存避免污染其他用例。

    注意：``.env`` 里的值会在 delenv 之后**回落生效**，所以"什么都没配"这一分支
    必须对**每个**入口都显式置空（只 delenv 是不够的）。三个入口的优先级是
    ``MILVUS_LITE_PATH`` > ``MILVUS_URI`` > ``MILVUS_HOST``，漏掉任何一个，
    高优先级的 .env 值都会让本用例永远走不到"未配置"分支（假绿/假红都可能是它）。
    """
    for key in ("MILVUS_LITE_PATH", "MILVUS_URI", "MILVUS_HOST", "MILVUS_PORT"):
        monkeypatch.delenv(key, raising=False)
    # 逐个显式置空（MILVUS_PORT 是 int 字段，置空会触发校验错误，故不动）
    monkeypatch.setenv("MILVUS_LITE_PATH", "")
    monkeypatch.setenv("MILVUS_URI", "")
    monkeypatch.setenv("MILVUS_HOST", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unconfigured_is_silent_and_returns_none(caplog):
    """没配 Milvus → None 且**不该**报错：回退 SQLite 是预期行为，不是降级。"""
    with caplog.at_level(logging.WARNING, logger="da.vectorstore.milvus"):
        assert milvus_mod.resolve_uri() is None
        assert milvus_mod.get_client() is None
    assert not caplog.records, f"未配置不应产生告警：{[r.getMessage() for r in caplog.records]}"


def test_illegal_uri_logs_instead_of_failing_silently(monkeypatch, caplog):
    """URI 非法时必须留痕：否则"配置写错"会伪装成"Milvus 不可达"。"""
    monkeypatch.setenv("MILVUS_URI", "not-a-valid-uri")
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="da.vectorstore.milvus"):
        client = milvus_mod.get_client()

    assert client is None, "非法 URI 不应返回客户端"
    assert caplog.records, "连接失败必须留下日志（静默降级是铁律 3 禁止的）"
    messages = [r.getMessage() for r in caplog.records]
    assert any("not-a-valid-uri" in m for m in messages), \
        f"日志要能说明是哪个 URI 失败：{messages}"


def test_unreachable_server_logs_instead_of_failing_silently(monkeypatch, caplog):
    """配了但连不上（服务没起）同样要留痕——这是运维最需要看到的一条。"""
    monkeypatch.setenv("MILVUS_URI", "http://127.0.0.1:1")  # 必然连不上
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="da.vectorstore.milvus"):
        assert milvus_mod.get_client() is None

    assert caplog.records, "服务连不上必须留下日志"
