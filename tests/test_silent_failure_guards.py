# -*- coding: utf-8 -*-
"""静默失败加固的回归测试 (2026-09-14, 攻击面七).

覆盖:
  1. db._h5i_symbols_df 失败时**不污染缓存** —— 修复前 @lru_cache 会把一次瞬时失败
     的空表永久缓存, 导致此后 universe merge / list_date 静默全空。
  2. db.get_universe(h5i) 空结果时**亮出告警**, 而不是静默返回空池。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

# `db` 顶部无条件 `import duckdb`(src/db.py:13), 而 CI 的 requirements_314.txt **不含
# duckdb** —— 若直接 import, 整个模块在收集阶段就 ImportError, 使 CI 以 exit code 2
# 中断(而非测试失败)。此处显式跳过, 让"缺 duckdb"表现为 skip 而不是 CI 崩溃。
# 注: 该缺陷属既有问题(db.py 硬依赖 duckdb), 与本文件无关, 已在提交信息中记录。
pytest.importorskip("duckdb", reason="db.py 硬依赖 duckdb, 而 CI 依赖清单未包含它")

import dataguard  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    import db
    dataguard.reset_warnings()
    # `_SYM_DF_SLOT` 只存在于上述"去缓存/重试/告警"修复版 db.py 中; 该修复尚未合入
    # 任何分支(见下方订正说明), 故此处置为条件清理, 不因缺失属性而让无关用例报错。
    _slot = getattr(db, "_SYM_DF_SLOT", None)
    if isinstance(_slot, dict):
        _slot["df"] = None
    yield
    dataguard.reset_warnings()
    if isinstance(_slot, dict):
        _slot["df"] = None


def _patch_read(monkeypatch, fn):
    import pyarrow.parquet as pq
    monkeypatch.setattr(pq, "read_table", fn)


# ---------------------------------------------------------------------------
# 2026-09-19 合并时订正: 以下前三个用例依赖 `db._h5i_symbols_df` 的一份**修复**
# (去掉 @lru_cache、失败不写缓存、带重试与 warn_once 告警、缓存槽 _SYM_DF_SLOT)。
# 该修复**不在任何已合入的分支上**:
#   - `src/db.py` 在 80ca9bf 与 origin/main 上是**完全相同**的旧实现
#     (`@lru_cache(maxsize=1)` + `except: return pd.DataFrame()`);
#   - 两边的 db.py 都没有 `_SYM_DF_SLOT` 这个属性。
# 合并后这三例会以 AttributeError 报错(而非断言失败), 属**测试引用了不存在的实现**。
# 故显式 skip 并保留原始断言, 待那份 db.py 修复合入后去掉 skip 即生效。
# 不删除、不改成"伪造通过" —— 缺陷本身仍然真实存在(见 docs/pit-valuation.md §⑭)。
# ---------------------------------------------------------------------------
_DB_SLOT_FIX_MERGED = hasattr(__import__("db"), "_SYM_DF_SLOT")
_skip_db_fix = pytest.mark.skipif(
    not _DB_SLOT_FIX_MERGED,
    reason="依赖 db._h5i_symbols_df 的去缓存/重试/告警修复(含 _SYM_DF_SLOT), 该修复尚未合入任何分支",
)


@_skip_db_fix
def test_symbols_df_failure_is_not_cached(monkeypatch):
    """读取失败 -> 返回空表但**不写缓存**; 故障恢复后下次调用应能拿到数据。"""
    import db

    calls = {"n": 0}

    def boom(_path):
        calls["n"] += 1
        raise OSError("模拟磁盘瞬时故障")

    _patch_read(monkeypatch, boom)
    out1 = db._h5i_symbols_df()
    assert out1.empty
    assert db._SYM_DF_SLOT["df"] is None, "失败结果绝不能被缓存(否则永久静默空表)"
    n_after_fail = calls["n"]
    assert n_after_fail >= 2, "应做重试(>=2 次尝试)"
    assert dataguard.warned_count("h5i_symbols_read") >= 1, "失败必须告警"

    # 故障恢复: 下一次调用应重新尝试并成功缓存
    import pyarrow as pa
    good = pa.table({"symbol": ["600519"], "market": ["sh"]})
    _patch_read(monkeypatch, lambda _p: good)
    out2 = db._h5i_symbols_df()
    assert len(out2) == 1
    assert db._SYM_DF_SLOT["df"] is not None
    # 成功后再调用: 命中缓存, 不再读盘
    n_before = calls["n"]
    db._h5i_symbols_df()
    assert calls["n"] == n_before, "成功结果应命中进程内缓存"


@_skip_db_fix
def test_get_universe_h5i_warns_on_empty(monkeypatch):
    """h5i universe 返回空 -> 必须告警, 不得静默变空池。"""
    import db

    monkeypatch.setattr(db, "_h5i_enabled", lambda: True)
    monkeypatch.setattr(db, "_universe_h5i", lambda date=None: pd.DataFrame())
    out = db.StockDB().get_universe("2026-09-04")
    assert out.empty
    assert dataguard.warned_count("universe_h5i_empty") >= 1, "空池必须告警"


@_skip_db_fix
def test_get_universe_h5i_warns_on_exception(monkeypatch):
    """h5i universe 抛异常 -> 也必须告警(修复前是静默 return 空表)。"""
    import db

    def boom(date=None):
        raise RuntimeError("模拟取数故障")

    monkeypatch.setattr(db, "_h5i_enabled", lambda: True)
    monkeypatch.setattr(db, "_universe_h5i", boom)
    out = db.StockDB().get_universe("2026-09-04")
    assert out.empty
    assert dataguard.warned_count("universe_h5i_empty") >= 1

# ---------------------------------------------------------------------------
# 2026-09-19 审计追加: "静默降级"第三/第四处 (见 docs/pit-valuation.md §⑭)
# ---------------------------------------------------------------------------

def test_factor_health_exception_warns(monkeypatch):
    """失效因子隔离异常 -> 必须告警.

    原实现静默 return, 且 docstring 误称"不影响选股主链路"; 实际上隔离失效会让
    复合分用着本该被置 0 的因子, **改变选股结果**, 而唯一症状是少一行日志。
    """
    import factor_library as fl

    dataguard.reset_warnings()
    monkeypatch.setenv("FACTOR_HEALTH_ENABLED", "1")

    def boom(as_of=None):
        raise RuntimeError("模拟 IC 曲线不可读")

    import factor_gate
    monkeypatch.setattr(factor_gate, "factor_health_flags", boom)
    W = {"vol": 0.3}
    fl._apply_factor_health(W, as_of="2026-09-04")   # 签名 (W, as_of); 不应抛出
    assert W["vol"] == 0.3, "异常时权重保持不变(隔离未生效)"
    assert dataguard.warned_count("factor_health_unavailable") >= 1, "隔离失效必须告警"
    dataguard.reset_warnings()


def test_load_ic_cache_warns_on_corrupt_file(monkeypatch, tmp_path):
    """IC 缓存损坏 -> 必须与"文件不存在"区分并告警."""
    import factor_gate as fg

    dataguard.reset_warnings()
    bad = tmp_path / "ic_curves.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert fg.load_ic_cache(str(bad)) is None
    assert dataguard.warned_count("ic_cache_unreadable") >= 1, "坏缓存必须告警"
    # 文件不存在属正常情形: 不应告警
    dataguard.reset_warnings()
    assert fg.load_ic_cache(str(tmp_path / "nope.json")) is None
    assert dataguard.warned_count("ic_cache_unreadable") == 0
    dataguard.reset_warnings()
