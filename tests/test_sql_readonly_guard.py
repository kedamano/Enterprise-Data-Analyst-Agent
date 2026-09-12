"""E4/01 只读守卫加固回归：``guard_readonly_sql`` 的绕过面。

背景（真实缺口）：守卫原本只禁 DML/DDL 关键字，**漏掉 MySQL 的文件与副作用原语**。
在真实 MySQL 8.0.26 上实测确认以下语句全部被放行（即"只读"承诺不成立）：

  - ``SELECT ... INTO OUTFILE '/path'``  → 把结果写服务器文件系统（任意文件写）
  - ``SELECT LOAD_FILE('/etc/passwd')``  → 读服务器任意文件
  - ``SELECT SLEEP(30)`` / ``BENCHMARK(...)`` → 资源耗尽 DoS
  - ``CALL proc()`` / ``HANDLER t OPEN`` / ``LOCK TABLES`` → 写/绕过/阻塞

另有**拆分绕过**：``INTO/*x*/OUTFILE``、``INTO\\n OUTFILE`` 让朴素正则漏检；
以及 MySQL **可执行注释** ``/*!50000 ... */``（内容会被真实执行）。

本文件把这些契约固化。守卫在"去注释 + 折叠空白"后的文本上匹配，故拆分写法可拦截。
"""
from __future__ import annotations

import pytest

from app.core.tools.sql_tool import guard_readonly_sql

# 必须拦截：文件系统写/读、DoS、存储过程、锁表、可执行注释、拆分绕过
_ATTACKS = [
    "SELECT * FROM t INTO OUTFILE '/tmp/x.csv'",
    "SELECT * FROM t INTO DUMPFILE '/tmp/x'",
    "SELECT * FROM t INTO/*bypass*/OUTFILE '/tmp/x'",     # 注释拆分绕过
    "SELECT * FROM t INTO\n   OUTFILE '/tmp/x'",          # 换行拆分绕过
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT SLEEP(30)",
    "SELECT BENCHMARK(100000000, MD5('a'))",
    "SELECT 1 INTO @x",
    "CALL do_write()",
    "EXECUTE stmt",
    "HANDLER t OPEN",
    "LOCK TABLES t WRITE",
    "UNLOCK TABLES",
    "LOAD DATA INFILE '/etc/passwd' INTO TABLE t",
    "SET GLOBAL general_log=1",
    "SET @a = 1",
    "SELECT 1 /*!50000 UNION SELECT password FROM mysql.user*/",  # 可执行注释
    "SELECT /*x*//*! SLEEP(5) */ 1",
    "SELECT 1; DROP TABLE t",                             # 多语句
    "DROP TABLE t",
    "UPDATE t SET a=1",
    "SELECT * FROM t FOR UPDATE",
]

# 必须放行：正常只读查询（含可能被关键字误杀的多行/注释/标识符）
_LEGIT = [
    "SELECT region, SUM(revenue) FROM fact_sales GROUP BY region",
    "WITH r AS (SELECT 1 AS a) SELECT a FROM r",
    "SELECT called_count, handler_name FROM t",           # 含 call/handler 前缀的列名
    "SELECT call, handler FROM t",                        # 列名恰为 call/handler
    "SELECT * FROM t LIMIT 10 OFFSET 5",
    "SELECT a FROM t -- 只读注释\nWHERE a > 1",
    "SELECT setting FROM config",
    "SELECT * FROM t WHERE note = 'locked tables'",
    "SELECT COUNT(*) FROM information_schema.tables",
]


@pytest.mark.parametrize("sql", _ATTACKS)
def test_attack_is_blocked(sql):
    err = guard_readonly_sql(sql)
    assert err, f"应被拦截但放行了: {sql}"
    assert "只读" in err


@pytest.mark.parametrize("sql", _LEGIT)
def test_legit_query_passes(sql):
    assert guard_readonly_sql(sql) is None, f"合法只读查询被误拦: {sql}"


def test_comment_splitting_does_not_bypass():
    """核心回归：注释/空白拆分的 INTO OUTFILE 必须被拦下。"""
    assert guard_readonly_sql("SELECT * FROM t INTO/*x*/OUTFILE '/p'")
    assert guard_readonly_sql("SELECT * FROM t INTO /*x*/ OUTFILE '/p'")
    assert guard_readonly_sql("SELECT * FROM t\nINTO\tOUTFILE '/p'")


def test_mysql_executable_comment_rejected():
    """MySQL 版本注释内容会被服务端真实执行 → 直接拒。"""
    assert guard_readonly_sql("/*!50000 SELECT SLEEP(5)*/")
    assert guard_readonly_sql("SELECT 1 /*! ,SLEEP(5) */")
