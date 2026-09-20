# -*- coding: utf-8 -*-
"""STOCKDB_ROOT 路径守卫的回归测试 (2026-09-20, 登记册 P2-LAKEROOT).

背景
----
`free_stockdb_sync._lake_root()` 过去只做"取值"而不校验"解析出的路径是否真的存在",
后果是**静默的**:

  * 09-05 该步骤 `scanned=5548`(正常);
  * 09-08 复盘时只剩 `ok=false, scanned=0` 加一句
    `parquet_refresh.error='kline_parts 无文件'` —— 看起来像"今天没数据",
    实际是**湖根解析错了**。

即: 主数据摄入路径依赖一个**未文档化、未持久化**的环境变量(STOCKDB_ROOT),
一旦丢失(换终端/换任务/重启), 摄入会静默变成 0 行。
修复把"解析出的路径不存在"变成**一次响亮的告警**。

本测试锁定四条契约
------------------
  1. 根不存在      -> 告警 stockdb_root_missing, 且**仍然返回**该路径(不抛异常、不改语义);
  2. 根在但缺子目录 -> 告警 stockdb_kline_parts_missing;
  3. 根与 kline_parts 都在 -> **完全静默**(不能把正常态报成异常, 否则告警会被忽略);
  4. 诊断本身失败  -> 绝不影响主链路(_lake_root 仍返回路径)。

注: `_lake_root()` 在模块导入期被调用一次(用于计算 FREE_STOCKDB_DIR /
KLINE_PARTS_DIR), 因此每个用例用 importlib.reload 重放该导入期调用。
"""
from __future__ import annotations

import glob
import importlib
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

dataguard = pytest.importorskip("dataguard")  # noqa: F841  轻量模块, 无第三方依赖


@pytest.fixture(autouse=True)
def _clean_warnings():
    dataguard.reset_warnings()
    yield
    dataguard.reset_warnings()


def _reload_with_root(monkeypatch, root):
    """在指定 STOCKDB_ROOT 下重新导入 free_stockdb_sync(重放导入期的 _lake_root 调用).

    注意: 首次 `import` 与随后的 `reload` **各触发一次** `_lake_root()`。
    若不清零, 计数会从 2 起算, 断言就失去分辨力(实测初版正是如此: 期望 1 得到 2)。
    故此处先热导入一次, 再 reset_warnings, 使**恰好一次**调用落在本次断言窗口内。
    """
    if root is None:
        monkeypatch.delenv("STOCKDB_ROOT", raising=False)
    else:
        monkeypatch.setenv("STOCKDB_ROOT", root)
    import free_stockdb_sync
    importlib.reload(free_stockdb_sync)  # 热导入: 吞掉首次 import 的那一次调用
    dataguard.reset_warnings()
    return importlib.reload(free_stockdb_sync)


# ---------------------------------------------------------------------------
# 1) 根目录不存在 -> 响亮告警, 但仍返回该路径
# ---------------------------------------------------------------------------
def test_missing_root_warns_but_still_returns(monkeypatch, tmp_path, capsys):
    missing = str(tmp_path / "no_such_lake")
    mod = _reload_with_root(monkeypatch, missing)

    # 语义不变: 守卫只做诊断, 不改变返回值(调用方仍拿到同一个路径)
    assert mod.FREE_STOCKDB_DIR == missing
    assert mod.KLINE_PARTS_DIR == os.path.join(missing, "kline_parts")

    # 响亮: 计数 + stderr 里能看见"是路径问题", 而不是让人去猜"为什么今天没数据"
    assert dataguard.warned_count("stockdb_root_missing") == 1
    err = capsys.readouterr().err
    assert missing in err
    assert "STOCKDB_ROOT" in err
    assert "0" in err or "0 个分片" in err or "无产出" in err


def test_missing_root_reports_source_of_value(monkeypatch, tmp_path, capsys):
    """告警必须写清路径**来源**(env vs 默认), 否则无法定位是谁没设变量。"""
    missing = str(tmp_path / "no_such_lake")
    _reload_with_root(monkeypatch, missing)
    assert "env:STOCKDB_ROOT" in capsys.readouterr().err


def test_default_root_reports_default_source(monkeypatch, capsys):
    """未设环境变量时, 回落到 <repo>/data/stockdb 并标注来源=默认。"""
    mod = _reload_with_root(monkeypatch, None)
    assert mod.FREE_STOCKDB_DIR.endswith(os.path.join("data", "stockdb"))
    err = capsys.readouterr().err
    # 该默认目录在干净检出里不存在(甚至 data/ 本身也不存在) -> 应告警且标明"默认"
    if not os.path.isdir(mod.FREE_STOCKDB_DIR):
        assert dataguard.warned_count("stockdb_root_missing") == 1
        assert "默认" in err


# ---------------------------------------------------------------------------
# 2) 根存在但缺 kline_parts -> 另一条告警(区分"根没了"与"根在了但湖空了")
# ---------------------------------------------------------------------------
def test_root_without_kline_parts_warns_distinctly(monkeypatch, tmp_path):
    root = tmp_path / "lake"
    root.mkdir()
    mod = _reload_with_root(monkeypatch, str(root))

    assert mod.FREE_STOCKDB_DIR == str(root)
    assert dataguard.warned_count("stockdb_kline_parts_missing") == 1
    # 两条告警必须互斥, 否则运维分不清是"根错"还是"湖空"
    assert dataguard.warned_count("stockdb_root_missing") == 0


# ---------------------------------------------------------------------------
# 3) 健康湖 -> 完全静默(告警的价值取决于它不误报)
# ---------------------------------------------------------------------------
def test_healthy_root_is_completely_silent(monkeypatch, tmp_path, capsys):
    root = tmp_path / "lake"
    (root / "kline_parts").mkdir(parents=True)
    mod = _reload_with_root(monkeypatch, str(root))

    assert mod.KLINE_PARTS_DIR == str(root / "kline_parts")
    assert dataguard.warned_count("stockdb_root_missing") == 0
    assert dataguard.warned_count("stockdb_kline_parts_missing") == 0
    assert capsys.readouterr().err == "", "健康路径不得产生任何告警输出"


# ---------------------------------------------------------------------------
# 4) warn_once 语义: 重复调用只打印一次, 但**每次都计数**(便于审计频次)
# ---------------------------------------------------------------------------
def test_warn_once_dedupes_print_but_keeps_counting(monkeypatch, tmp_path, capsys):
    missing = str(tmp_path / "no_such_lake")
    mod = _reload_with_root(monkeypatch, missing)
    capsys.readouterr()  # 吃掉导入期那次输出

    mod._lake_root()
    mod._lake_root()

    assert dataguard.warned_count("stockdb_root_missing") == 3, "计数必须累计(1 次导入 + 2 次显式)"
    assert capsys.readouterr().err == "", "同一 key 不得重复刷屏"


# ---------------------------------------------------------------------------
# 5) 诊断失败绝不影响主链路(这是"加固"不能引入新故障模式的前提)
# ---------------------------------------------------------------------------
def test_diagnostic_failure_never_breaks_main_path(monkeypatch, tmp_path):
    missing = str(tmp_path / "no_such_lake")
    mod = _reload_with_root(monkeypatch, missing)

    def _boom(*a, **k):
        raise RuntimeError("诊断自身炸了")

    monkeypatch.setattr(dataguard, "warn_once", _boom)
    assert mod._lake_root() == missing, "告警失败不得冒泡到取数主链路"


# ---------------------------------------------------------------------------
# 6) 与生产湖的一致性(本机存在 E:\\A_stockDB 时才跑)
# ---------------------------------------------------------------------------
_PROD_LAKE = r"E:\A_stockDB"
_prod_present = os.path.isdir(os.path.join(_PROD_LAKE, "kline_parts"))


@pytest.mark.skipif(not _prod_present, reason="本机无生产行情湖 E:\\A_stockDB\\kline_parts")
def test_production_lake_root_is_accepted_silently(monkeypatch, capsys):
    """真实部署路径不得误报 —— 这条用例是"告警不误报"的现场证据。"""
    mod = _reload_with_root(monkeypatch, _PROD_LAKE)
    assert mod.FREE_STOCKDB_DIR == _PROD_LAKE
    assert dataguard.warned_count("stockdb_root_missing") == 0
    assert dataguard.warned_count("stockdb_kline_parts_missing") == 0
    assert capsys.readouterr().err == ""
    shards = glob.glob(os.path.join(mod.KLINE_PARTS_DIR, "*.parquet"))
    assert shards, "生产湖根下应能扫到 *.parquet 分片"
