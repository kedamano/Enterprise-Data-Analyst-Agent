import json, os, re, urllib.request, sys

BASE = "https://ui.aceternity.com/registry"
OUT = "src"
PROXY = "http://127.0.0.1:7890"

os.environ["HTTP_PROXY"] = PROXY
os.environ["HTTPS_PROXY"] = PROXY

# 目标组件（控制台所需）
SEED = [
    "sidebar", "placeholders-and-vanish-input", "timeline", "text-generate-effect",
    "sparkles", "card-spotlight", "animated-modal", "multi-step-loader",
    "animated-tooltip", "tabs", "input", "background-beams", "meteors",
    "background-grid-with-dots", "stateful-button", "bento-grid", "typewriter-effect",
    "cover", "hero-highlight", "grid-background",
]

npm_deps = set()
written = set()      # 已写出文件相对 src 的路径
queue = list(SEED)
errors = []

ui_import_re = re.compile(r'@/components/ui/([\w\-/]+\.tsx?)')

def fetch_json(name):
    url = f"{BASE}/{name}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def write_file(rel_path, content):
    full = os.path.join(OUT, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    written.add(rel_path)

while queue:
    name = queue.pop(0)
    try:
        data = fetch_json(name)
    except Exception as e:
        errors.append(f"{name}: {e}")
        continue
    for dep in (data.get("dependencies") or []):
        if dep in ("motion",):  # 已安装
            continue
        npm_deps.add(dep)
    for f in (data.get("files") or []):
        path = f.get("path")
        content = f.get("content") or ""
        if path.startswith("components/"):
            rel = "components/" + path[len("components/"):]  # 已是 components/...
        else:
            rel = path
        # 映射到 src/
        rel = rel if rel.startswith("components/") else os.path.join("components", rel)
        write_file(rel, content)
        # 递归解析其内部依赖的 @/components/ui/*
        for m in ui_import_re.findall(content):
            comp = m.split("/")[-1].rsplit(".", 1)[0]
            already = [w.split("/")[-1].rsplit(".", 1)[0] for w in written]
            if comp not in already and comp not in [x.split("/")[-1] for x in queue] and comp not in SEED:
                queue.append(comp)
    print(f"[ok] {name} -> {len(data.get('files',[]))} file(s)")

print("\n=== npm deps to install ===")
print(" ".join(sorted(npm_deps)))
print("\n=== errors ===")
for e in errors:
    print(e)
print("\n=== written files ===")
for w in sorted(written):
    print(w)
