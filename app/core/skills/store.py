"""技能（Skill）存储层 —— 以 ``SKILL.md`` 目录为标准（兼容 Anthropic Skills 约定）。

为什么是"目录 + SKILL.md"而不是"一张表的纯文本"
================================================
- **可移植**：一个技能 = 一个文件夹，含 `SKILL.md`（+ 可选附属文件）。可以直接
  和 Claude Skills / 其它 agent 生态互相导入导出，不必被本项目的表结构锁死。
- **可读可 diff**：正文就是 Markdown，评审/版本管理天然友好。
- **元数据分离**：`SKILL.md` 只放"这个技能是什么"（name/description/正文），
  属于**内容**；`enabled / created_at / origin` 属于**本机状态**，放旁挂
  `.meta.json`——否则每次启停都要重写 SKILL.md，既污染内容也破坏可移植性。

目录约定::

    <skills_dir>/
      <id>/
        SKILL.md          # 权威内容（frontmatter + 正文）
        .meta.json        # 本机状态（enabled/created_at/updated_at/origin）
        <附属文件...>      # 可选，zip 导入时原样保留

id 由名称 slug 化而来（`数据分析规范` → 无 ASCII 时回落 `skill-<hash>`），
保证稳定、可点、且不会因为改名而丢引用。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

SKILL_FILENAME = "SKILL.md"
META_FILENAME = ".meta.json"

# frontmatter：文件开头的 `---\n...\n---`
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
# 名称合法字符（slug 用）
_SLUG_RE = re.compile(r"[^a-z0-9\-]+")
# frontmatter 的 `key: value`
_KV_RE = re.compile(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


# --------------------------------------------------------------------------- #
# frontmatter 解析（极简 YAML 子集：`key: value` / `key:` + `- item` 列表）
# --------------------------------------------------------------------------- #
def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """把 frontmatter 文本解析成 dict。**不引入 pyyaml**：只需覆盖简单标量与短列表。"""
    meta: dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value.strip():
            meta[key] = _unquote(value)
            continue
        # 空值 → 可能是列表（后续若干 `- item`）
        items: list[str] = []
        while i < len(lines):
            lm = _LIST_ITEM_RE.match(lines[i])
            if not lm:
                break
            items.append(_unquote(lm.group(1)))
            i += 1
        meta[key] = items if items else ""
    return meta


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """切出 (frontmatter dict, 正文)。没有 frontmatter 时返回 ({}, 原文)。"""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, (text or "")
    return _parse_frontmatter(m.group(1)), (text or "")[m.end():]


def _render_frontmatter(meta: dict[str, Any]) -> str:
    """把 dict 写回 frontmatter（只支持标量；列表写成 `- item` 行）。"""
    out: list[str] = ["---"]
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            out.append(f"{key}:")
            out.extend(f"  - {v}" for v in value)
        else:
            text = str(value)
            # 含特殊字符时加引号，避免被解析器误读
            if any(c in text for c in (":", "#", "\n")) or text != text.strip():
                text = '"' + text.replace('"', '\\"') + '"'
            out.append(f"{key}: {text}")
    out.append("---")
    return "\n".join(out) + "\n"


def _slugify(name: str) -> str:
    """名称 → 稳定 id。纯中文名会 slug 成空 → 回落 `skill-<hash6>`。"""
    base = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    if not base:
        digest = hashlib.sha1((name or uuid.uuid4().hex).encode("utf-8")).hexdigest()[:8]
        base = f"skill-{digest}"
    return base[:48]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# --------------------------------------------------------------------------- #
# SkillStore
# --------------------------------------------------------------------------- #
class SkillStore:
    """文件系统技能仓库。**无全局状态**：root 是唯一真相源，便于测试注入临时目录。"""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            try:
                from ...config import get_settings

                root = getattr(get_settings(), "skills_dir", "data/skills")
            except Exception:  # 配置层故障也要能起（用默认路径）
                root = "data/skills"
        self.root = Path(root)

    # -- 路径工具 ----------------------------------------------------------- #
    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _skill_dir(self, skill_id: str) -> Path:
        # 防目录穿越：id 只允许安全字符
        safe = _SLUG_RE.sub("-", str(skill_id or "").strip().lower()).strip("-")
        if not safe or safe != str(skill_id).strip().lower():
            # id 非法时不要落到 root 之外/之上
            safe = _slugify(str(skill_id))
        return self.root / safe

    def _meta_path(self, skill_id: str) -> Path:
        return self._skill_dir(skill_id) / META_FILENAME

    def _skill_md(self, skill_id: str) -> Path:
        return self._skill_dir(skill_id) / SKILL_FILENAME

    def _read_meta(self, skill_id: str) -> dict[str, Any]:
        p = self._meta_path(skill_id)
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _write_meta(self, skill_id: str, meta: dict[str, Any]) -> None:
        d = self._skill_dir(skill_id)
        d.mkdir(parents=True, exist_ok=True)
        self._meta_path(skill_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _unique_id(self, base: str) -> str:
        candidate = base
        n = 2
        while self._skill_dir(candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # -- 读取 --------------------------------------------------------------- #
    def _load(self, skill_id: str, *, include_body: bool = True) -> Optional[dict[str, Any]]:
        md = self._skill_md(skill_id)
        if not md.is_file():
            return None
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            return None
        fm, body = split_frontmatter(text)
        meta = self._read_meta(skill_id)
        name = str(fm.get("name") or meta.get("name") or skill_id)
        description = str(fm.get("description") or meta.get("description") or "")
        item: dict[str, Any] = {
            "id": skill_id,
            "name": name,
            "description": description,
            "enabled": bool(meta.get("enabled", True)),
            "origin": str(meta.get("origin") or "manual"),
            "created_at": meta.get("created_at") or "",
            "updated_at": meta.get("updated_at") or "",
            "chars": len(body or ""),
            "path": str(self._skill_dir(skill_id)),
        }
        if fm.get("version"):
            item["version"] = fm["version"]
        if fm.get("allowed-tools") or fm.get("allowed_tools"):
            item["allowed_tools"] = fm.get("allowed-tools") or fm.get("allowed_tools")
        if include_body:
            item["body"] = body or ""
        return item

    def list(self, *, include_body: bool = False) -> list[dict[str, Any]]:
        """列出全部技能（按名称排序）。坏目录/坏文件**跳过而不炸**（管理面要能看全）。"""
        self._ensure_root()
        out: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            item = self._load(child.name, include_body=include_body)
            if item is not None:
                out.append(item)
        out.sort(key=lambda x: (not x.get("enabled", True), x.get("name", "").lower()))
        return out

    def get(self, skill_id: str, *, include_body: bool = True) -> Optional[dict[str, Any]]:
        return self._load(skill_id, include_body=include_body)

    # -- 写入 --------------------------------------------------------------- #
    def create(self, name: str, description: str = "", body: str = "",
               *, enabled: bool = True, origin: str = "manual",
               skill_id: str | None = None) -> dict[str, Any]:
        if not (name or "").strip():
            raise ValueError("技能名称不能为空")
        self._ensure_root()
        sid = self._unique_id(_slugify(skill_id or name))
        front = {"name": name.strip(), "description": (description or "").strip()}
        text = _render_frontmatter(front) + "\n" + (body or "").strip() + "\n"
        d = self._skill_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        self._skill_md(sid).write_text(text, encoding="utf-8")
        now = _now_iso()
        self._write_meta(sid, {"enabled": bool(enabled), "origin": origin,
                               "created_at": now, "updated_at": now,
                               "name": front["name"], "description": front["description"]})
        return self._load(sid)  # type: ignore[return-value]

    def update(self, skill_id: str, *, name: str | None = None,
               description: str | None = None, body: str | None = None,
               enabled: bool | None = None) -> Optional[dict[str, Any]]:
        cur = self._load(skill_id)
        if cur is None:
            return None
        new_name = cur["name"] if name is None else name.strip()
        new_desc = cur["description"] if description is None else description.strip()
        new_body = cur["body"] if body is None else body
        front: dict[str, Any] = {"name": new_name, "description": new_desc}
        if cur.get("version"):
            front["version"] = cur["version"]
        if cur.get("allowed_tools"):
            front["allowed-tools"] = cur["allowed_tools"]
        text = _render_frontmatter(front) + "\n" + (new_body or "").strip() + "\n"
        self._skill_md(skill_id).write_text(text, encoding="utf-8")
        meta = self._read_meta(skill_id)
        meta.update({"name": new_name, "description": new_desc, "updated_at": _now_iso()})
        if enabled is not None:
            meta["enabled"] = bool(enabled)
        meta.setdefault("created_at", _now_iso())
        meta.setdefault("origin", "manual")
        self._write_meta(skill_id, meta)
        return self._load(skill_id)

    def set_enabled(self, skill_id: str, enabled: bool) -> Optional[dict[str, Any]]:
        if not self._skill_dir(skill_id).is_dir():
            return None
        meta = self._read_meta(skill_id)
        meta["enabled"] = bool(enabled)
        meta["updated_at"] = _now_iso()
        meta.setdefault("created_at", _now_iso())
        meta.setdefault("origin", "manual")
        self._write_meta(skill_id, meta)
        return self._load(skill_id)

    def delete(self, skill_id: str) -> bool:
        d = self._skill_dir(skill_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    # -- zip 导入 ----------------------------------------------------------- #
    def import_zip(self, data: bytes, *, overwrite: bool = False) -> dict[str, Any]:
        """从 zip 导入技能。

        兼容两种布局：``SKILL.md`` 在根，或 ``<name>/SKILL.md``（Anthropic 约定）。
        有 **zip-slip 防护**：绝对路径 / `..` 一律拒绝；只解压 SKILL.md 所在目录内的文件。
        """
        imported: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as exc:
            return {"imported": [], "errors": [f"不是合法的 zip：{exc}"]}

        with zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # 找到所有 SKILL.md 的位置，得到"技能根目录"（其父目录）
            roots: list[str] = []
            for n in names:
                parts = n.replace("\\", "/").split("/")
                if parts[-1].lower() == SKILL_FILENAME.lower():
                    root = "/".join(parts[:-1])
                    if root not in roots:
                        roots.append(root)
            if not roots:
                return {"imported": [], "errors": ["zip 内未找到 SKILL.md"]}

            for root in roots:
                prefix = (root + "/") if root else ""
                members = [n for n in names if n.replace("\\", "/").startswith(prefix)]
                # zip-slip 校验
                bad = [n for n in members
                       if n.replace("\\", "/").startswith("/")
                       or ".." in n.replace("\\", "/").split("/")]
                if bad:
                    errors.append(f"{root or '.'}: 含非法路径（拒绝解压）：{bad[:3]}")
                    continue
                try:
                    self._import_members(zf, members, prefix, overwrite)
                    sid = self._id_of_imported(zf, prefix)
                    item = self._load(sid)
                    if item:
                        imported.append(item)
                except Exception as exc:  # 单个技能失败不影响其余
                    errors.append(f"{root or '.'}: {type(exc).__name__}: {exc}")
        return {"imported": imported, "errors": errors}

    def _id_of_imported(self, zf: zipfile.ZipFile, prefix: str) -> str:
        """导入后确定该技能的 id（由 SKILL.md frontmatter 的 name 推导，与 _import_members 一致）。"""
        members = [n for n in zf.namelist()
                   if n.replace("\\", "/").startswith(prefix)
                   and n.replace("\\", "/").split("/")[-1].lower() == SKILL_FILENAME.lower()]
        if not members:
            return ""
        text = zf.read(members[0]).decode("utf-8", "ignore")
        fm, _ = split_frontmatter(text)
        folder = prefix.rstrip("/").split("/")[-1] if prefix else ""
        name = str(fm.get("name") or folder or "skill")
        # 与 _import_members 用同样的候选顺序：先 folder，后 name
        for cand in (folder, name):
            if not cand:
                continue
            sid = _slugify(cand)
            if self._skill_md(sid).is_file():
                return sid
        return _slugify(name)

    def _import_members(self, zf: zipfile.ZipFile, members: list[str],
                        prefix: str, overwrite: bool) -> None:
        skill_md = next(n for n in members
                        if n.replace("\\", "/").split("/")[-1].lower() == SKILL_FILENAME.lower())
        text = zf.read(skill_md).decode("utf-8", "ignore")
        fm, _ = split_frontmatter(text)
        folder = prefix.rstrip("/").split("/")[-1] if prefix else ""
        name = str(fm.get("name") or folder or "skill")
        # id 优先取 zip 内目录名（更贴近作者意图），无则用 name
        sid = self._unique_id(_slugify(folder or name))
        target = self._skill_dir(sid)
        if target.exists() and not overwrite:
            # 同名 → 追加序号（不覆盖用户已有技能）
            sid = self._unique_id(_slugify(folder or name))
            target = self._skill_dir(sid)
        target.mkdir(parents=True, exist_ok=True)
        for n in members:
            rel = n.replace("\\", "/")[len(prefix):] if prefix else n.replace("\\", "/")
            if not rel:
                continue
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(n))
        now = _now_iso()
        self._write_meta(sid, {"enabled": True, "origin": "imported",
                               "created_at": now, "updated_at": now,
                               "name": str(fm.get("name") or name),
                               "description": str(fm.get("description") or "")})

    # -- 提示词渲染 ---------------------------------------------------------- #
    def render(self, skill_ids: list[str], *, max_chars: int | None = None) -> str:
        """把若干技能渲染成注入提示词的文本块。未知/已删除的 id 静默跳过。"""
        if not skill_ids:
            return ""
        if max_chars is None:
            try:
                from ...config import get_settings

                max_chars = int(getattr(get_settings(), "skill_max_chars", 8000) or 8000)
            except Exception:
                max_chars = 8000
        blocks: list[str] = []
        seen: set[str] = set()
        for sid in skill_ids:
            if not sid or sid in seen:
                continue
            seen.add(sid)
            item = self.get(sid)
            if item is None:
                continue
            body = (item.get("body") or "").strip()
            if len(body) > max_chars:
                body = body[:max_chars] + "\n…[技能正文超长已截断]"
            desc = item.get("description") or ""
            head = f'<skill name="{item["name"]}"'
            if desc:
                head += f' description="{desc}"'
            head += ">"
            blocks.append(f"{head}\n{body}\n</skill>")
        if not blocks:
            return ""
        return (
            "以下技能由用户在本次对话中显式勾选，是**必须遵循**的方法论/口径/领域约束：\n"
            + "\n\n".join(blocks)
        )


# --------------------------------------------------------------------------- #
# 单例与便捷函数
# --------------------------------------------------------------------------- #
_STORE: SkillStore | None = None
_STORE_ROOT: str | None = None


def get_skill_store() -> SkillStore:
    """进程级单例。配置里的 `skills_dir` 变了会自动重建（测试常切临时目录）。"""
    global _STORE, _STORE_ROOT
    try:
        from ...config import get_settings

        root = str(getattr(get_settings(), "skills_dir", "") or "data/skills")
    except Exception:
        root = "data/skills"
    if _STORE is None or _STORE_ROOT != root:
        _STORE = SkillStore(root)
        _STORE_ROOT = root
    return _STORE


def render_skills(skill_ids: list[str] | None) -> str:
    """便捷入口：把勾选的技能渲染成提示词块（空输入 → 空串）。"""
    if not skill_ids:
        return ""
    try:
        return get_skill_store().render(list(skill_ids))
    except Exception:
        return ""  # 技能层故障绝不打断主流水线
