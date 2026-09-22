# -*- coding: utf-8 -*-
"""pytest 根 conftest: 把 src/ 加入 sys.path, 使 tests 可用扁平模块名 import
(config / factor_gate / ... 均位于 src/)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# [2026-09-23] 运行时数据目录**永不是**测试来源。
#
# 事由(真实事故, 非预防性): `data/_quarantine/tmp_scripts_20260922/_tmp_fork_test.py`
# 是一次性排查脚本, 名字恰好匹配 pytest 默认的 `*_test.py` ⇒ `pytest -q`(不带路径)时
# 试图 import 它 ⇒ `.venv314` 没有 `h5i_db` ⇒ **收集中断, 2301 个测试一个都没跑**。
# 更险的是: 它是**顶层脚本**(无 test_ 函数), 若 import 成功会在**收集阶段就真执行**
# (建临时库 / create_fork / restore / shutil.rmtree) —— 收集不该有副作用。
#
# `pytest.ini` 的 `testpaths = tests` 是**真正的修复**, 它挡住了"不带路径"这个入口。
#
# ⚠️ 本条 `collect_ignore_glob` 的能力边界(实测, 别当成万能):
#   它对**递归发现**有效, 但**显式把路径传进来时无效** ——
#   `pytest data/_quarantine/.../_tmp_fork_test.py` 仍会 import 并报 h5i_db 错。
#   这是 pytest 的语义(显式参数优先于 conftest 的 collect_ignore)。
#   故它只是**第二层兜底**, 不替代 `testpaths`。
# 只忽略 `data/` —— 不用宽泛的 `*_tmp*` 模式, 免得将来误伤合法用例名。
collect_ignore_glob = ["data/*"]
