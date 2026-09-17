"""core/filestore.py — 路径安全校验（防目录穿越）。

待测模块：app/core/filestore.py（safe_name / save_file 的路径约束）

覆盖链路：
- safe_name：把 / \\ 等路径分隔符替换为 _，杜绝目录穿越。
- safe_name：去掉控制字符与特殊字符。
- save_file 的 blob 路径：blob 文件名 = node_id（服务端生成），用户传的名字不进入路径。
- 同名覆盖：同目录同名文件 save_file = 覆盖，而不是新建重复节点。

mock 策略：不 mock；直接用真实 FileStore 在 tmp_path 下建库（不污染 data/）。
本模块与 tests/test_safe_fs_safety.py 同风格。
"""
from __future__ import annotations

import pytest

from app.core.filestore import ROOT, FileStore, safe_name


# --------------------------------------------------------------------------- #
# 1. safe_name 入口清洁
# --------------------------------------------------------------------------- #
class TestSafeName:
    def test_replaces_forward_slash(self):
        assert "/" not in safe_name("../../../etc/passwd")

    def test_replaces_backslash(self):
        assert "\\" not in safe_name("..\\..\\Windows\\System32\\config")

    def test_strips_control_characters(self):
        cleaned = safe_name("hello\x00world\x1f.txt")
        assert "\x00" not in cleaned
        assert "\x1f" not in cleaned

    def test_strips_dots_and_spaces_from_ends(self):
        """Windows 不允许以点/空格结尾；safe_name 做 strip('. ')。"""
        cleaned = safe_name("  spaced.  ")
        assert not cleaned.endswith(".")
        assert not cleaned.endswith(" ")
        assert not cleaned.startswith(" ")

    def test_falls_back_to_unnamed_when_empty(self):
        assert safe_name("") == "未命名"
        assert safe_name("   ") == "未命名"
        assert safe_name("...") == "未命名"

    def test_preserves_chinese(self):
        """企业文件大量中文，clean 后必须保留。"""
        cleaned = safe_name("2024年度报告（终版）.docx")
        # 括号被 strip 掉但中文保留
        assert "2024" in cleaned
        assert "年度报告" in cleaned
        # 路径穿越字符 / \\ 不在内
        assert "/" not in cleaned
        assert "\\" not in cleaned

    def test_max_length_enforced(self):
        long_name = "a" * 200 + ".csv"
        cleaned = safe_name(long_name)
        assert len(cleaned) <= 120  # MAX_NAME_LEN


# --------------------------------------------------------------------------- #
# 2. FileStore 的 save_file 路径安全
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    db = tmp_path / "filestore.db"
    blob_dir = tmp_path / "blobs"
    return FileStore(db_path=db, blob_dir=blob_dir)


class TestSaveFilePathSafety:
    def test_blob_file_uses_node_id_not_user_name(self, store, tmp_path):
        """blob 文件名必须是服务端生成的 node_id，不能用用户传入的名字（防穿越）。"""
        node = store.create_folder(ROOT, "数据集")
        saved = store.save_file(
            parent_id=node["id"], name="../../../evil.sh", data=b"malicious"
        )
        blob = store.blob_path(saved["id"])
        assert blob is not None
        # blob 文件名就是 node_id（n_ 开头）
        assert blob.name.startswith("n_")
        # 不存在任何含 "evil" 的文件
        blobs = list((tmp_path / "blobs").iterdir())
        assert all("evil" not in b.name for b in blobs)

    def test_cannot_escape_blob_directory_via_name(self, store):
        """即使用户名是穿越路径，存储位置仍在 blob_dir 内。"""
        node = store.create_folder(ROOT, "安全测试")
        store.save_file(
            parent_id=node["id"], name="../../../etc/passwd", data=b"x"
        )
        # 所有 blob 都在 store.blobs 下
        for blob_file in store.blobs.iterdir():
            # 解析后的路径必须在 store.blobs 内
            resolved = blob_file.resolve()
            # store.blobs 是 tmp_path/blobs；所有子文件必须是其子路径
            assert str(resolved).startswith(str(store.blobs.resolve()))

    def test_same_name_same_parent_overwrites(self, store):
        """同目录同名文件 save = 覆盖，不是新建重复节点。"""
        folder = store.create_folder(ROOT, "数据集")
        first = store.save_file(folder["id"], "report.csv", b"v1")
        second = store.save_file(folder["id"], "report.csv", b"v2-longer")
        # 同一 nodeId（覆盖）
        assert first["id"] == second["id"]
        # 同目录下只有一个 report.csv
        children = store.list_children(folder["id"])
        csvs = [c for c in children if c["name"] == "report.csv"]
        assert len(csvs) == 1
