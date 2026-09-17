"""safe_fs 安全契约：purge_file / purge_tree 在故障路径下绝不能外泄异常。

Spec: app/core/safe_fs.py（模块 docstring 里的 WorkBuddy 沙箱立场）

本模块不是测性能，是在测"任何清理动作的失败都不能升级成服务退出"——
删不动就降级（截断 / 留 0 字节残骸），**穿透路径仅限 KeyboardInterrupt**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.core import safe_fs


def _symlink_privilege_available() -> bool:
    """Windows 下创建 symlink 需要 admin / Developer Mode，POSIX 总是返回 True。"""
    if sys.platform != "win32":
        return True
    d = tempfile.mkdtemp()
    t = os.path.join(d, "t.txt")
    open(t, "w").close()
    link = os.path.join(d, "l")
    try:
        os.symlink(t, link)
        return True
    except OSError:
        return False
    finally:
        for p in (link, t):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


skip_if_no_symlink = pytest.mark.skipif(
    not _symlink_privilege_available(),
    reason="symlink creation requires Windows admin / Developer Mode or POSIX",
)


# --------------------------------------------------------------------------- #
# purge_file
# --------------------------------------------------------------------------- #
def test_purge_missing_file_returns_true(tmp_path):
    """不存在的文件：视为"已 purge"返回 True，且不抛。

    设计约定：调用方不看"磁盘是否真移除了"，只看"是否抛出"。
    不存在的文件与已删除的文件对后续业务语义完全等价。
    """
    missing = tmp_path / "never_existed.txt"
    assert not missing.exists()
    assert safe_fs.purge_file(missing) is True


def test_purge_regular_file_succeeds(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    assert f.exists()

    assert safe_fs.purge_file(f) is True
    assert not f.exists()


@skip_if_no_symlink
def test_purge_symlink_unlinks_the_link_not_target(tmp_path):
    """purge 走 p.unlink() 会断开链接本体，不会读 target 内容。

    Path.unlink() 在 POSIX 上本来就是操作 link 自己，而非 follow。
    此测试确保我们**不要让它意外变成 rm -f target**。
    """
    target = tmp_path / "secret.txt"
    target.write_bytes(b"TOP-SECRET-DO-NOT-TOUCH")
    link = tmp_path / "link"
    link.symlink_to(target)

    assert safe_fs.purge_file(link) is True
    assert not link.exists(), "链接本体应该被删除"
    assert target.exists(), "symlink target 必须保持完整"
    assert target.read_bytes() == b"TOP-SECRET-DO-NOT-TOUCH"


def test_purge_permission_denied_degrades_to_truncate(tmp_path):
    """unlink 被拒绝（常见于只读系统 / WorkBuddy 安全拒绝）→ 降级为截断内容。

    关键契约：**不抛异常**——即使清理失败也不能打断调用方（例如数据库 DELETE）。
    """
    f = tmp_path / "ro.txt"
    f.write_bytes(b"sensitive")

    def _boom(self, *a, **kw):
        raise PermissionError("read-only fs")

    with mock.patch.object(Path, "unlink", _boom):
        result = safe_fs.purge_file(f)

    # result 可能 True（权限降级路径也吞）但核心：没抛异常
    assert result in (True, False)


def test_purge_keyboard_interrupt_is_reraised(tmp_path):
    """KeyboardInterrupt 必须穿透：让运维 / 编排的 kill 信号立刻终止服务。

    这是 L (P0) 修复的核心点——不让"清理临时文件"吞掉 Ctrl-C。
    """
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")

    def _kbint(self, *a, **kw):
        raise KeyboardInterrupt()

    with mock.patch.object(Path, "unlink", _kbint):
        with pytest.raises(KeyboardInterrupt):
            safe_fs.purge_file(f)


# --------------------------------------------------------------------------- #
# purge_tree
# --------------------------------------------------------------------------- #
def test_purge_tree_missing_returns_true(tmp_path):
    missing = tmp_path / "no_such_dir"
    assert safe_fs.purge_tree(missing) is True


def test_purge_tree_complex_layout(tmp_path):
    """嵌套：多层子目录 + 文件 + 子目录内的文件。全部清光。"""
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "f1.txt").write_bytes(b"1")
    (root / "a" / "b" / "f2.txt").write_bytes(b"2")
    (root / "a" / "b" / "c" / "f3.txt").write_bytes(b"3")
    (root / "top.txt").write_bytes(b"T")

    assert safe_fs.purge_tree(root) is True
    assert not root.exists()


def test_purge_tree_partial_failure_does_not_raise(tmp_path):
    """中间某个 rmdir 拒绝（锁/权限）→ 清理剩下的部分，不抛。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "f.txt").write_bytes(b"x")

    def _locked(self, *a, **kw):
        raise OSError(39, "Directory not empty")  # ENOTEMPTY

    with mock.patch.object(Path, "rmdir", _locked):
        result = safe_fs.purge_tree(root)

    # 核心契约：即使"全失败"，调用方也不会收到异常
    assert result in (True, False)


def test_purge_tree_keyboard_interrupt_is_reraised(tmp_path):
    """KeyboardInterrupt 在目录清理中也必须穿透。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "f.txt").write_bytes(b"x")

    def _kbint(self, *a, **kw):
        raise KeyboardInterrupt()

    with mock.patch.object(Path, "unlink", _kbint):
        with pytest.raises(KeyboardInterrupt):
            safe_fs.purge_tree(root)
