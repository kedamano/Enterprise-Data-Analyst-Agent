# -*- coding: utf-8 -*-
"""清理色彩迁移留下的「空值类」残留。

背景：migrate-colors.mjs 把 from-indigo-500 / to-violet-600 这类色标映射掉了，
但 bg-gradient-to-br 这层「渐变方向」类不属于颜色族，脚本够不到，于是留下
`bg-gradient-to-br   text-white`（无颜色的渐变 = 完全透明）和 `hover:` 后无值的空悬空类。
这些类名不会报错，只会让按钮/头像静默变成透明底 —— 属于必须人工收口的迁移尾巴。

用法：
    python scripts/fix-empty-gradients.py          # dry-run
    python scripts/fix-empty-gradients.py --write  # 写入
"""
from __future__ import annotations

import io
import pathlib
import sys

ROOT = pathlib.Path("src")
WRITE = "--write" in sys.argv

# (文件, 原文, 新文, 说明)
EDITS: list[tuple[str, str, str, str]] = [
    # ── 1. 助手头像：空渐变 → 品牌实色 ────────────────────────────────────
    (
        "components/ChatMessage.tsx",
        '<div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-panel bg-gradient-to-br   text-white shadow-md ">',
        '<div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-panel bg-brand text-white">',
        "助手头像：空渐变 + 悬空 shadow-md → 实色 bg-brand",
    ),
    # ── 2. 发送按钮：空渐变 + 两个悬空 hover: ─────────────────────────────
    (
        "components/Composer.tsx",
        '? "bg-gradient-to-br   text-white shadow-md  hover: hover:"',
        '? "bg-brand text-white hover:bg-brand-hover"',
        "发送按钮：空渐变 + 悬空 hover: → 实色 + 品牌 hover",
    ),
    # ── 3. 新建对话按钮：同上，另外去掉与实底同色的边框 ────────────────────
    (
        "components/ConversationList.tsx",
        'className="flex w-full items-center justify-center gap-1.5 rounded-control border border-rule-strong bg-gradient-to-br   px-3 py-2 text-small font-medium text-white shadow-sm  transition hover: hover:"',
        'className="flex w-full items-center justify-center gap-1.5 rounded-control bg-brand px-3 py-2 text-small font-medium text-white transition hover:bg-brand-hover"',
        "新建对话：空渐变 + 悬空 hover: + 冗余边框 → 实色按钮",
    ),
    # ── 4. 首页主标题：渐变文字（生成式界面公共默认手法）→ 实色墨色 ────────
    (
        "components/Welcome.tsx",
        '<h1 className="text-balance bg-gradient-to-br    bg-clip-text text-display font-semibold leading-tight tracking-tight text-transparent sm:text-display">',
        '<h1 className="text-balance text-display font-semibold leading-tight tracking-tight text-ink">',
        "首页主标题：渐变文字 → 实色墨色（去掉 bg-clip-text / text-transparent 这套组合）",
    ),
    # ── 5. 时间轴滚动光束：色标被删空，只剩 to-transparent ─────────────────
    (
        "components/ui/timeline.tsx",
        'const TONE_BEAM = "  to-transparent";',
        'const TONE_BEAM = "from-brand via-brand to-transparent";',
        "滚动光束：色标被删空 → 品牌色",
    ),
    # ── 6-9. 状态圆点的发光阴影仍是旧调色板硬编码（arbitrary value 脚本够不到）
    (
        "components/ui/timeline.tsx",
        "shadow-[0_0_10px_rgba(99,102,241,.45)]",
        "shadow-[0_0_10px_rgba(20,58,94,.35)]",
        "进行中圆点：靛蓝发光 → 品牌色发光",
    ),
    (
        "components/ui/timeline.tsx",
        "shadow-[0_0_10px_rgba(16,185,129,.4)]",
        "shadow-[0_0_10px_rgba(27,127,90,.35)]",
        "成功圆点：翠绿发光 → verified 发光",
    ),
    (
        "components/ui/timeline.tsx",
        "shadow-[0_0_10px_rgba(245,158,11,.4)]",
        "shadow-[0_0_10px_rgba(154,103,0,.35)]",
        "告警圆点：琥珀发光 → attention 发光",
    ),
    (
        "components/ui/timeline.tsx",
        "shadow-[0_0_10px_rgba(244,63,94,.4)]",
        "shadow-[0_0_10px_rgba(179,38,30,.35)]",
        "失败圆点：玫瑰发光 → danger 发光",
    ),
]


def main() -> int:
    applied = 0
    missed: list[tuple[str, str]] = []

    for rel, old, new, desc in EDITS:
        path = ROOT / rel
        if not path.exists():
            missed.append((rel, "文件不存在"))
            continue
        src = io.open(path, encoding="utf-8").read()
        hits = src.count(old)
        if hits == 0:
            missed.append((rel, old[:70]))
            continue
        print("  [%s x%d] %-26s %s" % ("写入" if WRITE else "命中", hits, rel.split("/")[-1], desc))
        if WRITE:
            io.open(path, "w", encoding="utf-8", newline="").write(src.replace(old, new))
        applied += 1

    print()
    print("%s %d/%d 处" % ("已应用" if WRITE else "可应用", applied, len(EDITS)))
    if missed:
        print("未匹配：")
        for rel, frag in missed:
            print("  %s :: %r" % (rel, frag))
    if not WRITE:
        print("\n（dry-run；加 --write 写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
