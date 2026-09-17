"""Live E2E：Skills 管理 + 对话内技能注入。

验证链路：
  1. POST /api/v1/skills          新建技能（正文含唯一标记）
  2. GET  /api/v1/skills          清单可见
  3. POST /api/v1/chat/analyze    带 skill_ids 提问 → 报告里应出现该标记
  4. DELETE /api/v1/skills/{id}   清理

标记是唯一随机串，只有在技能正文真的进了提示词时才可能出现。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
MARK = "SXOPS-7133"


def call(method: str, path: str, payload: dict | None = None, timeout: int = 600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    print("== 1) 基线清单 ==")
    st, body = call("GET", "/skills")
    print("  GET /skills ->", st, json.dumps(body, ensure_ascii=False)[:300])
    if st != 200:
        return 1
    before = body["total"]

    print("== 2) 新建技能 ==")
    st, skill = call("POST", "/skills", {
        "name": "E2E 注入标记技能",
        "description": "live 验证用：要求报告末尾输出唯一标记",
        "body": (
            "## 输出要求（最高优先级）\n"
            "无论分析结果如何，都必须在报告的**正文里**原样输出下面这个标记，"
            "不要改写、不要省略：\n\n"
            f"{MARK}\n"
        ),
        "enabled": True,
    })
    print("  POST /skills ->", st, json.dumps(skill, ensure_ascii=False)[:400])
    if st != 201:
        return 1
    sid = skill["id"]

    try:
        st, body = call("GET", "/skills")
        print("== 3) 清单复查 ==")
        print(f"  total {before} -> {body['total']}；可见该技能:",
              any(s["id"] == sid for s in body["skills"]))

        print("== 4) 带 skill_ids 走真实分析 ==")
        # 唯一 session：避免命中上一轮历史（增量/记忆路径）混入本次判定
        import time as _t
        st, resp = call("POST", "/chat/analyze", {
            "query": "查询 oasys 数据源中 aoa_dept 表的数据行数",
            "session_id": f"e2e-skills-{int(_t.time())}",
            "skill_ids": [sid],
        })
        print("  POST /chat/analyze ->", st)
        blob = json.dumps(resp, ensure_ascii=False)
        print("  status:", (resp or {}).get("status") if isinstance(resp, dict) else resp)
        print("  标记命中:", MARK in blob)
        if MARK not in blob:
            print("  响应片段:", blob[:1500])
        return 0 if MARK in blob else 2
    finally:
        print("== 5) 清理 ==")
        st, body = call("DELETE", f"/skills/{sid}")
        print("  DELETE ->", st, body)


if __name__ == "__main__":
    raise SystemExit(main())
