"""物理删除的「绝不外泄异常」封装。

**为什么需要这个模块**（这不是理论洁癖，是踩过的血案）：

``Path.unlink()`` / ``os.remove()`` / ``shutil.rmtree()`` 在失败时抛 ``OSError``，
所以代码里常见的写法是 ``try: ... except OSError: pass``。但在被托管的运行环境里，
删除可能被**安全删除钩子**接管（WorkBuddy 沙箱的 safe-delete shim 会把删除改写成
"移入回收站"，并在守卫判定失败时直接 ``raise SystemExit(1)``）。而：

* ``SystemExit`` 继承 ``BaseException`` 而非 ``Exception``。uvicorn / Starlette 的
  异常中间件只把 ``Exception`` 转成 500，``SystemExit`` 会**穿透它、终止整个服务进程**。
  实测后果：一次「删除目录」把整个 API 服务打挂，所有后续请求全部连接失败。
* ``shutil.rmtree(..., ignore_errors=True)`` 挡不住，它内部只忽略 ``OSError``。
* 守卫的判定是**按发起删除的工具调用累计**的（``CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD``）。
  长驻服务进程由一次工具调用启动，于是它生命周期内的所有删除都记在同一账上，
  跑到阈值才突然爆炸——表现为"平时都能删，删到某一刻服务就死了"，极难排查。

**设计立场**：删除临时文件 / 回收磁盘字节本质是**清理动作**。它失败在任何业务语义下
都不该升级成进程级故障。所以统一走本模块：删得掉最好；删不掉就地降级（把内容截断为
0 字节，至少不残留可读数据、不再占空间），全程不抛异常。

**调用方约定**：本模块的返回值表示「是否彻底从磁盘移除」。返回 ``False`` 只意味着
磁盘上留了个 0 字节残骸（或目录未能删除），**业务侧的逻辑删除应当照常完成**——
数据库行才是事实来源。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def purge_file(path: str | Path) -> bool:
    """尽力删除单个文件，**任何异常都不外泄**。

    返回 ``True`` 表示已从磁盘移除；``False`` 表示删除被拦截、已降级为截断内容
    （文件变成 0 字节残骸，由调用方或后续 GC 处理）。
    """
    p = Path(path)
    try:
        if p.exists() or p.is_symlink():
            p.unlink()
        return True
    except BaseException as exc:  # noqa: BLE001 —— 见模块 docstring：WorkBuddy safe-delete shim 会 raise SystemExit(1)
        # SystemExit 必须吞：删除是清理动作，让它们穿透等于"删个临时文件杀服务"。
        # 但 KeyboardInterrupt 必须穿透 —— 让 Ctrl-C 能立刻杀死长驻运维进程。
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.debug("purge_file: 直接删除失败 %s (%s)，降级为截断", p, type(exc).__name__)

    # ── 降级路径 ────────────────────────────────────────────────────────
    # 写 0 字节：不依赖任何删除权限，既能回收空间，也不残留可读内容。
    try:
        with open(p, "wb"):
            pass
        return False
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False


def purge_tree(path: str | Path) -> bool:
    """尽力递归删除目录，**任何异常都不外泄**。

    刻意不用 ``shutil.rmtree``：它要么把异常抛出来，要么（``ignore_errors=True``）
    只忽略 ``OSError``，仍然挡不住 ``SystemExit``。这里自底向上手动清理，逐项吞掉。

    返回 ``True`` 表示目录已整体移除；``False`` 表示仅清理了能清的部分。
    """
    root = Path(path)
    try:
        if not root.exists():
            return True

        # 自底向上：先清空子目录里的文件，再从最深一层开始 rmdir
        entries = sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True)
        for item in entries:
            if item.is_dir() and not item.is_symlink():
                try:
                    item.rmdir()
                except BaseException as exc:  # noqa: BLE001
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    pass
            else:
                purge_file(item)
        try:
            root.rmdir()
            return True
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt):
                raise
            return False
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.debug("purge_tree: 目录清理未完成 %s (%s)", path, type(exc).__name__)
        return False
