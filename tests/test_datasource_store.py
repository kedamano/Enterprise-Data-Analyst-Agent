"""E7/02 页面「新建连接」——本地数据源存储 + URL 构造 + sources() 合并。

契约：
- build_url 为三种方言（SQLite / MySQL / PostgreSQL）产出 SQLAlchemy 可用 DSN，
  用户名/密码必须 quote_plus（密码里的 @ : / 不得破坏 URL 结构）。
- DataSourceStore 落盘 JSON：add/remove/list roundtrip；重名 / 保留名 default 拒绝；
  文件损坏当空库（配置问题不阻塞服务——与 DATA_SOURCES 同哲学）。
- datasource.sources() 合并本地存储：local 同名**覆盖** env 源（页面是最新意图），
  default 主源永不被覆盖。
"""
from __future__ import annotations

import pytest

from app.core.tools.datasource import DEFAULT_SOURCE, sources
from app.core.tools.datasource_store import DataSourceStore, build_url


# --- build_url ---


def test_build_url_mysql_encodes_special_password():
    url = build_url(
        "mysql", host="10.0.0.2", port=3307, database="hr",
        username="root", password="p@ss:1/",
    )
    assert url == "mysql+pymysql://root:p%40ss%3A1%2F@10.0.0.2:3307/hr"


def test_build_url_mysql_default_port():
    url = build_url("mysql", host="h", database="db", username="u", password="p")
    assert url.startswith("mysql+pymysql://u:p@h:3306/db")


def test_build_url_postgresql():
    url = build_url(
        "postgresql", host="h", port=5433, database="dw",
        username="u", password="p",
    )
    assert url.startswith("postgresql+psycopg2://u:p@h:5433/dw")


def test_build_url_sqlite_windows_path_normalized():
    assert build_url("sqlite", path=r"D:\data\biz.db") == "sqlite:///D:/data/biz.db"


def test_build_url_sqlite_requires_path():
    with pytest.raises(ValueError):
        build_url("sqlite")


def test_build_url_mysql_requires_host_and_database():
    with pytest.raises(ValueError):
        build_url("mysql", host="", database="db", username="u", password="p")
    with pytest.raises(ValueError):
        build_url("mysql", host="h", database="", username="u", password="p")


def test_build_url_unknown_dialect_rejected():
    with pytest.raises(ValueError):
        build_url("oracle", host="h", database="db", username="u", password="p")


# --- DataSourceStore ---


def test_store_roundtrip(tmp_path):
    store = DataSourceStore(tmp_path / "ds.json")
    store.add("hr", "mysql+pymysql://u:p@h:3306/hr", "mysql")
    entries = store.list_entries()
    assert set(entries) == {"hr"}
    assert entries["hr"]["dialect"] == "mysql"
    assert entries["hr"]["url"].startswith("mysql+pymysql://")
    assert store.remove("hr") is True
    assert store.list_entries() == {}
    assert store.remove("hr") is False


def test_store_persists_across_instances(tmp_path):
    p = tmp_path / "ds.json"
    DataSourceStore(p).add("biz", "postgresql+psycopg2://u:p@b:5432/dw", "postgresql")
    assert set(DataSourceStore(p).list_entries()) == {"biz"}


def test_store_rejects_duplicate_and_default(tmp_path):
    store = DataSourceStore(tmp_path / "ds.json")
    store.add("hr", "mysql://x", "mysql")
    with pytest.raises(ValueError):
        store.add("hr", "mysql://y", "mysql")
    with pytest.raises(ValueError):
        store.add("default", "mysql://y", "mysql")


def test_store_corrupt_file_treated_as_empty(tmp_path):
    p = tmp_path / "ds.json"
    p.write_text("{broken", encoding="utf-8")
    assert DataSourceStore(p).list_entries() == {}


def test_store_missing_file_treated_as_empty(tmp_path):
    assert DataSourceStore(tmp_path / "nope.json").list_entries() == {}


# --- sources() 合并 ---


def test_sources_merge_local_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_SOURCES", "hr=mysql://env-only/hr")
    store = DataSourceStore(tmp_path / "ds.json")
    store.add("hr", "mysql+pymysql://u:p@local:3306/hr", "mysql")
    store.add("biz", "postgresql+psycopg2://u:p@b:5432/dw", "postgresql")
    monkeypatch.setenv("DATASOURCE_STORE_PATH", str(tmp_path / "ds.json"))

    srcs = sources()
    assert srcs["hr"]["url"] == "mysql+pymysql://u:p@local:3306/hr"  # local 覆盖 env
    assert srcs["biz"]["dialect"] == "postgresql"
    assert DEFAULT_SOURCE in srcs  # 主源始终在


def test_sources_merge_without_store_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_SOURCES", "")
    monkeypatch.setenv("DATASOURCE_STORE_PATH", str(tmp_path / "nope.json"))
    srcs = sources()
    assert list(srcs) == [DEFAULT_SOURCE]
