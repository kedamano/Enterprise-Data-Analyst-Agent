"""技能存储层（SKILL.md 目录）的契约测试。

钉住三件事：
1. SKILL.md 是**权威内容**，`.meta.json` 只存本机状态（启停/时间/来源）；
2. zip 导入兼容"根目录 SKILL.md"与"<name>/SKILL.md"两种布局，且有 **zip-slip 防护**；
3. `render()` 只渲染传入的 id，未知 id 静默跳过。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.core.skills.store import SkillStore, split_frontmatter


@pytest.fixture
def store(tmp_path) -> SkillStore:
    return SkillStore(tmp_path / "skills")


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #
def test_split_frontmatter_parses_scalars_and_lists():
    fm, body = split_frontmatter(
        "---\nname: 营收口径\ndescription: \"统一口径\"\nallowed-tools:\n  - sql_query\n---\n\n正文"
    )
    assert fm["name"] == "营收口径"
    assert fm["description"] == "统一口径"
    assert fm["allowed-tools"] == ["sql_query"]
    assert body.strip() == "正文"


def test_split_frontmatter_without_block_returns_body_unchanged():
    fm, body = split_frontmatter("# 没有 frontmatter")
    assert fm == {}
    assert body == "# 没有 frontmatter"


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_writes_skill_md_and_meta(store: SkillStore):
    item = store.create("营收口径规范", "统一口径", "营收 = SUM(paid)")
    d = store.root / item["id"]
    assert (d / "SKILL.md").is_file()
    meta = json.loads((d / ".meta.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is True
    assert meta["origin"] == "manual"
    # 内容在 SKILL.md 里（frontmatter + 正文），不在 meta
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "营收 = SUM(paid)" in text


def test_create_requires_name(store: SkillStore):
    with pytest.raises(ValueError):
        store.create("   ")


def test_list_returns_all_and_sorts_disabled_last(store: SkillStore):
    a = store.create("甲", "a", "body-a")
    b = store.create("乙", "b", "body-b")
    store.set_enabled(a["id"], False)
    ids = [s["id"] for s in store.list()]
    assert set(ids) == {a["id"], b["id"]}
    assert ids[-1] == a["id"]  # 停用的排在后面
    # 列表默认不带正文
    assert "body" not in store.list()[0]


def test_get_includes_body_and_detail_fields(store: SkillStore):
    a = store.create("丙", "desc", "正文内容")
    got = store.get(a["id"])
    assert got["body"].strip() == "正文内容"
    assert got["description"] == "desc"
    assert got["chars"] > 0
    assert got["path"]


def test_update_partial_fields(store: SkillStore):
    a = store.create("丁", "d", "旧正文")
    upd = store.update(a["id"], body="新正文")
    assert upd["body"].strip() == "新正文"
    assert upd["name"] == "丁"  # 未传则保持
    assert store.get(a["id"])["description"] == "d"


def test_set_enabled_and_delete(store: SkillStore):
    a = store.create("戊", "", "x")
    assert store.set_enabled(a["id"], False)["enabled"] is False
    assert store.get(a["id"])["enabled"] is False
    assert store.delete(a["id"]) is True
    assert store.get(a["id"]) is None
    assert store.delete(a["id"]) is False  # 已删 → False，不抛


def test_unknown_id_returns_none(store: SkillStore):
    assert store.get("不存在") is None
    assert store.update("不存在", body="x") is None
    assert store.set_enabled("不存在", True) is None


def test_slug_collision_gets_suffix(store: SkillStore):
    a = store.create("SQL 规范", "", "1")
    b = store.create("SQL 约束", "", "2")
    # 两者 slug 都是 "sql"，第二个必须拿到不同的 id（不能覆盖第一个）
    assert a["id"] != b["id"]
    assert store.get(a["id"]) is not None
    assert store.get(b["id"]) is not None


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def test_render_only_includes_requested_ids(store: SkillStore):
    a = store.create("甲技能", "desc-a", "内容A")
    b = store.create("乙技能", "desc-b", "内容B")
    text = store.render([a["id"]])
    assert "甲技能" in text and "内容A" in text
    assert "乙技能" not in text and "内容B" not in text


def test_render_skips_unknown_and_dedupes(store: SkillStore):
    a = store.create("甲技能", "", "内容A")
    text = store.render([a["id"], a["id"], "ghost"])
    assert text.count("内容A") == 1
    assert "ghost" not in text


def test_render_empty_input_is_empty_string(store: SkillStore):
    assert store.render([]) == ""


# --------------------------------------------------------------------------- #
# zip 导入
# --------------------------------------------------------------------------- #
def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_import_zip_folder_layout(store: SkillStore):
    data = _zip({
        "my-skill/SKILL.md": "---\nname: 行业词表\ndescription: 零售术语\n---\n\n# 用法\n先查词表",
        "my-skill/extra.txt": "附属文件",
    })
    res = store.import_zip(data)
    assert res["errors"] == []
    assert len(res["imported"]) == 1
    item = res["imported"][0]
    assert item["name"] == "行业词表"
    assert item["origin"] == "imported"
    # 附属文件原样保留
    assert (store.root / item["id"] / "extra.txt").is_file()


def test_import_zip_root_layout(store: SkillStore):
    data = _zip({"SKILL.md": "---\nname: 根布局技能\n---\n正文"})
    res = store.import_zip(data)
    assert [i["name"] for i in res["imported"]] == ["根布局技能"]


def test_import_zip_rejects_path_traversal(store: SkillStore, tmp_path):
    data = _zip({
        "evil/SKILL.md": "---\nname: evil\n---\nx",
        "evil/../../pwned.txt": "pwned",
    })
    res = store.import_zip(data)
    assert res["imported"] == []
    assert any("非法路径" in e for e in res["errors"])
    # 关键：越界文件绝不能真的落盘
    assert not (tmp_path / "pwned.txt").exists()


def test_import_zip_without_skill_md_reports_error(store: SkillStore):
    res = store.import_zip(_zip({"readme.md": "no skill here"}))
    assert res["imported"] == []
    assert any("SKILL.md" in e for e in res["errors"])


def test_import_zip_bad_bytes_reports_error(store: SkillStore):
    res = store.import_zip(b"not a zip at all")
    assert res["imported"] == []
    assert any("zip" in e for e in res["errors"])


def test_import_multiple_skills_in_one_zip(store: SkillStore):
    data = _zip({
        "a/SKILL.md": "---\nname: 技能A\n---\nA",
        "b/SKILL.md": "---\nname: 技能B\n---\nB",
    })
    res = store.import_zip(data)
    assert sorted(i["name"] for i in res["imported"]) == ["技能A", "技能B"]
