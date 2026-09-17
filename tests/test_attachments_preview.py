"""core/attachments.py — 附件预览 / 缩略图入口。

待测模块：app/core/attachments.py（build_preview / materialize_table / sidecar_path）

覆盖链路：
- build_preview：CSV 附件 → DatasetPreview(kind=table) with columns/sample。
- build_preview：图片附件 → DatasetPreview(kind=image) with bytes、空 excerpt。
- materialize_table：把表格数据写入 sidecar SQLite（_sidecar/upload.db），mock 文件系统。
- sidecar_path：session_id 经字符白名单过滤后落到 data/uploads/<session>/ 下。
- small file 判定：is_small 在字节/行均低于阈值时为 True。

mock 策略：sidecar_path 文件 IO 用 tmp_path + monkeypatch；文件系统落盘在 tmp 目录。
"""
from __future__ import annotations

import pytest

from app.core.attachments import (
    SMALL_FILE_MAX_BYTES,
    SMALL_FILE_MAX_ROWS,
    DatasetPreview,
    build_preview,
    materialize_table,
    sidecar_path,
)


# --------------------------------------------------------------------------- #
# 1. build_preview 主入口
# --------------------------------------------------------------------------- #
class TestBuildPreview:
    def test_csv_preview_has_columns_and_rows(self):
        """CSV 附件解析为 table 预览，含列名与样例行。"""
        csv_bytes = "region,revenue\n华东,1200\n华北,800\n华南,900\n".encode("utf-8")
        prev = build_preview(csv_bytes, "sales.csv")
        assert prev.kind == "table"
        assert "region" in prev.columns
        assert "revenue" in prev.columns
        assert prev.rows == 3
        assert len(prev.sample) >= 1

    def test_json_preview_has_columns(self):
        """JSON 数组的记录 → table 预览。"""
        json_bytes = b'[{"name":"alice","score":90},{"name":"bob","score":85}]'
        prev = build_preview(json_bytes, "users.json")
        assert prev.kind == "table"
        assert "name" in prev.columns

    def test_image_preview_kind_is_image(self):
        """图片附件（按后缀）→ image 预览，不解析内容。"""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 假 PNG 头
        prev = build_preview(png_bytes, "chart.png")
        assert prev.kind == "image"
        assert prev.bytes == len(png_bytes)
        assert prev.rows == 0

    def test_text_preview_kind_is_text(self):
        """md/txt → text 预览，保留 excerpt。"""
        md_bytes = (b"# Report\n\nThis is an analysis report.\n" * 5)
        prev = build_preview(md_bytes, "notes.md")
        assert prev.kind == "text"
        assert len(prev.excerpt) > 0


# --------------------------------------------------------------------------- #
# 2. is_small 小文件判定
# --------------------------------------------------------------------------- #
class TestIsSmall:
    def test_small_csv_is_small(self):
        """行数与字节均低于阈值 → is_small=True。"""
        prev = DatasetPreview(
            name="tiny.csv",
            kind="table",
            rows=10,
            columns=["a", "b"],
            sample=[{"a": 1, "b": 2}],
            bytes=500,
            all_rows=[{"a": 1, "b": 2}] * 10,
        )
        assert prev.is_small is True

    def test_large_rows_not_small(self):
        """行数超过阈值 → is_small=False。"""
        prev = DatasetPreview(
            name="big.csv",
            kind="table",
            rows=SMALL_FILE_MAX_ROWS + 1,
            columns=["a"],
            sample=[{"a": 1}],
            bytes=1000,
        )
        assert prev.is_small is False

    def test_large_bytes_not_small(self):
        """字节超过阈值 → is_small=False。"""
        prev = DatasetPreview(
            name="wide.csv",
            kind="table",
            rows=10,
            columns=["a"] * 50,
            sample=[{}],
            bytes=SMALL_FILE_MAX_BYTES + 1,
        )
        assert prev.is_small is False

    def test_non_table_never_small(self):
        """非 table 类附件永远不是 small（文本/图片）。"""
        prev = DatasetPreview(name="x.png", kind="image", bytes=100)
        assert prev.is_small is False


# --------------------------------------------------------------------------- #
# 3. sidecar_path 路径安全
# --------------------------------------------------------------------------- #
class TestSidecarPath:
    def test_path_within_upload_dir(self, tmp_path, monkeypatch):
        """sidecar 必须落在 data/uploads/<session>/ 下，不能 escape。"""
        monkeypatch.setattr("app.core.attachments.UPLOAD_DIR", tmp_path)
        # session_id 经白名单过滤后拼入路径
        path = sidecar_path("s_abc123")
        assert path.name == "upload.db"
        assert path.parent.name == "s_abc123"

    def test_special_characters_sanitized_in_session_id(self):
        """session_id 中的特殊字符被过滤为 _，路径不穿越。"""
        cleaned = sidecar_path("../../etc/passwd")
        # sidecar 文件名始终是 upload.db
        assert cleaned.name == "upload.db"
        # session_id 中的 / \\ 被替换为 _ → 不出现字面斜杠
        parent = cleaned.parent.name
        assert "/" not in parent
        assert "\\" not in parent
        # 目录名就是字面清洗结果（不允许原始路径穿越字符）
        assert "etc_passwd" in parent


# --------------------------------------------------------------------------- #
# 4. materialize_table 边车库落地
# --------------------------------------------------------------------------- #
class TestMaterializeTable:
    def test_materialize_writes_to_sidecar(self, tmp_path, monkeypatch):
        """一条表格 preview 落地成 sidecar SQLite，且 preview.table 被设置为 slug。"""
        # 把 module 级 UPLOAD_DIR 切到 tmp（sidecar_path 内部读这个常量）
        monkeypatch.setattr("app.core.attachments.UPLOAD_DIR", tmp_path)

        prev = DatasetPreview(
            name="orders.csv",
            kind="table",
            rows=2,
            columns=["region", "amount"],
            sample=[{"region": "华东", "amount": "100"}, {"region": "华北", "amount": "80"}],
            all_rows=[{"region": "华东", "amount": "100"}, {"region": "华北", "amount": "80"}],
            bytes=50,
        )
        table_name = materialize_table(prev, session_id="s_test123")
        assert table_name != ""
        assert prev.table == table_name
        # sidecar 文件真实存在
        sc = sidecar_path("s_test123")
        assert sc.exists()

    def test_materialize_empty_columns_returns_empty(self):
        """没有列的 preview 不落地、返回空串。"""
        prev = DatasetPreview(name="empty.csv", kind="table", rows=0, columns=[])
        result = materialize_table(prev, session_id="s_empty")
        assert result == ""

    def test_table_slug_from_csv_name(self):
        """表名由文件名 stem 转 slug（去中文/特殊字符）。"""
        from app.core.attachments import _slug

        assert _slug("orders.csv") == "orders"
        # 数字开头 → 加 t_ 前缀（SQLite 表名不能以数字开头）
        assert _slug("2024 report.csv") == "t_2024_report"
        assert _slug("123.csv") == "t_123"
        # 中文字符被 [^a-z0-9_] 清洗：中文字符全部变 _，再 strip → 空 → uploaded
        assert _slug("中文文件.xlsx") == "uploaded"
