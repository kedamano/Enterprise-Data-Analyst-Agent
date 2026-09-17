import pathlib

root = pathlib.Path(r"D:\work\项目\Enterprise Data Analyst Agent\web\src")
targets = [
    r"components\ChatMessage.test.tsx",
    r"components\ConversationList.test.tsx",
    r"components\DataSourcesView.test.tsx",
    r"components\FilesView.test.tsx",
    r"components\settings\team.test.tsx",
]

for rel in targets:
    p = root / rel
    lines = p.read_text(encoding="utf-8").splitlines(keepends=False)
    changed = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('it("') or s.count('"') != 4:
            continue
        parts = line.split('"')
        # [前缀, A, B, '', 余下]
        lines[i] = parts[0] + '"' + parts[1] + "「" + parts[2] + "」" + '"' + parts[4]
        print("FIX", rel, i + 1)
        print("  旧:", line.strip())
        print("  新:", lines[i].strip())
        changed = True
    if changed:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
