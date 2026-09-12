"""``python_analysis`` tool – sandboxed statistical / visualization execution.

Execution model (defence in depth, suitable for a skeleton; for true isolation
run the worker inside a Docker/gVisor sandbox as DeepAnalyze does):
1. AST pre-scan rejects imports of network / OS / shell modules.
2. Code runs in a subprocess with CPython ``-I`` (isolated, no cwd import,
   no user site) and a hard wall-clock timeout.
3. ``DA_WORKDIR`` points at a per-session scratch dir; artifact CSVs/PNGs the
   code writes there are returned to the caller.

The code receives ``data_csv`` (path produced by ``sql_query``) and may read it
with pandas/numpy/matplotlib which are pre-installed in the worker env.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ...config import get_settings

BLOCKED_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "requests",
    "urllib", "http", "ftplib", "smtplib", "telnetlib", "sqlite3",
    "ctypes", "glob", "io", "pickle", "marshal", "builtins",
}

# Dynamic escapes that bypass the import scan (``__import__("os")`` etc.).
# Not a hard boundary – see the module docstring – but closes the trivial path.
BLOCKED_CALLS = {"__import__", "eval", "exec", "compile"}


def _guard(code: str) -> str | None:
    """Return an error string if the code imports a blocked module."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"代码语法错误: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in BLOCKED_MODULES:
                    return f"禁止导入模块: {top}"
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in BLOCKED_MODULES:
                return f"禁止导入模块: {top}"
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in BLOCKED_CALLS:
                return f"禁止动态执行调用: {name}"
    return None


def _build_prelude(data_csv: str, workdir: str) -> str:
    return (
        "import pandas as pd, numpy as np\n"
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        f"DATA_CSV = {data_csv!r}\n"
        f"WORKDIR = {workdir!r}\n"
        "if DATA_CSV:\n    try:\n        df = pd.read_csv(DATA_CSV)\n    except Exception:\n        df = None\n"
        "else:\n    df = None\n"
        "import json as _json\n"
    )


def _resolve_env_csv(params: dict[str, Any], wd: Path) -> str:
    data_csv = params.get("data_csv")
    if data_csv:
        return str(data_csv)
    csvs = sorted(wd.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    return str(csvs[-1]) if csvs else ""


def _failure_error(stderr: str, stdout: str = "") -> str:
    """非零退出时的可读原因（自纠错回注依赖它；不能是 None）。"""
    tail = (stderr or "").strip() or (stdout or "").strip()
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    return "执行失败：" + (" | ".join(lines[-3:])[:400] if lines else "未知错误（无 stderr）")


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _run_docker(code: str, wd: Path, env_csv: str, settings) -> dict[str, Any]:
    """受限 Docker 沙箱执行（DeepAnalyze docker_executor 同款隔离清单）。"""
    if not shutil.which("docker"):
        return {"ok": False, "error": "docker 不可用，无法使用 python_sandbox_mode=docker"}

    extra_mounts: list[str] = []
    # CSV 在 wd 内 → /work 下相对路径；否则额外 ro 挂载 /inputs
    base = wd.resolve()
    if env_csv:
        p = Path(env_csv).resolve()
        try:
            rel = p.relative_to(base)
            csv_container = f"/work/{rel.as_posix()}"
        except ValueError:
            extra_mounts += ["-v", f"{p.parent}:/inputs:ro"]
            csv_container = f"/inputs/{p.name}"
    else:
        csv_container = ""

    name = f"da_py_{uuid.uuid4().hex[:8]}"
    script = _build_prelude(csv_container, "/work") + code
    (wd / "analysis.py").write_text(script, encoding="utf-8")

    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--network", "none",
        "--memory", "512m", "--cpus", "1.0", "--pids-limit", "128",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--user", "1000:1000",
        "-v", f"{base}:/work:rw",
        *extra_mounts,
        "-e", "DA_WORKDIR=/work", "-e", "MPLBACKEND=Agg",
        "-e", "PYTHONIOENCODING=utf-8", "-e", "PYTHONUTF8=1",
        "-e", "MPLCONFIGDIR=/tmp/mpl",
        "-w", "/work",
        settings.python_sandbox_image,
        "python", "/work/analysis.py",
    ]

    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=settings.python_max_exec_s)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["docker", "stop", "-t", "1", name], capture_output=True, timeout=10)
        except Exception:
            pass
        return {"ok": False, "error": f"执行超时 ({settings.python_max_exec_s}s)", "rows": []}

    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    artifacts = [str(p) for p in wd.iterdir() if p.suffix in (".png", ".csv", ".json")]

    out: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "execution_time_ms": int((time.time() - started) * 1000),
        "artifacts": artifacts,
    }
    if not out["ok"]:
        out["error"] = _failure_error(stderr, stdout)  # 失败必须带可读原因
    if stdout:
        try:
            out["parsed"] = json.loads(stdout)
        except json.JSONDecodeError:
            out["parsed"] = None
    return out


def _run_subprocess(code: str, wd: Path, env_csv: str, settings) -> dict[str, Any]:
    # 子进程 cwd=wd，故脚本要读的文件路径必须绝对化：sql_query 物化出的 csv_path 是
    # 相对项目根的（data/artifacts/<sid>/s1.csv），相对 wd 解析会读不到 → df 静默为 None
    wd_abs = wd.resolve()
    abs_csv = str(Path(env_csv).resolve()) if env_csv else ""
    (wd_abs / "analysis.py").write_text(
        _build_prelude(abs_csv, str(wd_abs)) + code, encoding="utf-8")

    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(wd_abs / "analysis.py")],
            cwd=str(wd_abs),
            capture_output=True,
            timeout=settings.python_max_exec_s,
            env={**os.environ, "DA_WORKDIR": str(wd_abs), "MPLBACKEND": "Agg",
                 "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时 ({settings.python_max_exec_s}s)", "rows": []}

    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    artifacts = [str(p) for p in wd.iterdir() if p.suffix in (".png", ".csv", ".json")]

    out: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "execution_time_ms": int((time.time() - started) * 1000),
        "artifacts": artifacts,
    }
    if not out["ok"]:
        out["error"] = _failure_error(stderr, stdout)  # 失败必须带可读原因
    # try to parse stdout as json for structured hand-off to the Analyst
    if stdout:
        try:
            out["parsed"] = json.loads(stdout)
        except json.JSONDecodeError:
            out["parsed"] = None
    return out


def run(params: dict[str, Any], workdir: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    code = params.get("code") or ""
    if not code:
        return {"ok": False, "error": "缺少 code 参数"}
    if not settings.python_sandbox_enabled:
        return {"ok": False, "error": "python 沙箱已禁用"}

    err = _guard(code)
    if err:
        return {"ok": False, "error": err}

    wd = Path(workdir or tempfile.mkdtemp(prefix="da_py_")).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    env_csv = _resolve_env_csv(params, wd)

    mode = settings.python_sandbox_mode
    if mode == "docker":
        return _run_docker(code, wd, env_csv, settings)
    if mode == "auto":
        if _docker_available():
            return _run_docker(code, wd, env_csv, settings)
        return _run_subprocess(code, wd, env_csv, settings)
    return _run_subprocess(code, wd, env_csv, settings)
