# -*- coding: utf-8 -*-
"""DRL 降级链（DRL-4）的回归（2026-09-19）.

被测: `drl_degrade.resolve()` 四级链 + `drl_train` 的两条失败路径接线。
**轻量**: 只 import `drl_degrade` / `dataguard` / `config`, 不 import `drl_train`
（后者拖入 torch, 会打挂 CI core job）。对 `drl_train` 的接线用**源码级断言**验证
（同 `test_engine_date_override.py` 的做法）—— 断言的是"不存在静默失败路径"这个结构不变量。

必须**构造触发条件**, 不能只看代码跑通:
  · L0 部署: 训练成功 -> level=0, 指针指向当日, **不写降级事件**
  · L0 恢复: 原指针不可用 + 训练成功 -> level=0 且记一条 `recovered_from` 事件（可逆性）
  · L1 保留: 指针可用 + 训练失败 -> level=1, 生效权重 **== 指针版本权重**, 告警 WARNING
  · L2 回退: 指针不可用/缺失 + 存在实盘有效版本 -> level=2, 告警 ERROR, 指针改指回退版本
  · L3 暂停: 无任何有效版本 -> halt=True, **blocked_plan=True**, 告警 CRITICAL
  · 连续性: L2 写过指针后, 次日训练失败应走 L1（而非再 L2）

★ 本文件最重要的一组用例: `TestLegacyArtifactGuard`
  生产 `data/drl/` **混放**实盘版本与回测遗留目录。实测真实布局（2026-09-19）:
  32 个 8 位目录 = 12 个有实盘标记 + 20 个无标记; 无标记的 20 个里**有 7 个**满足
  朴素"可用"判据（model.zip + ok=True）—— 6 个是 2015-2021 回测遗留, 第 7 个是
  **缺标记的实盘日 20260825**（保守方向偏差, 见 `drl_degrade` 模块 docstring）。
  若只按日期倒序扫, 在近端版本全不可用时会**静默回退到 2021 年的回测模型** ——
  比 L3 告警危险得多的失败模式。这批用例锁死该行为, 并可用
  `scripts/preflight_drl_degrade_realdata.py` 在**真实生产目录上**复现同一结论。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import config  # noqa: E402  (只 import os, 极轻量)
import dataguard  # noqa: E402
import drl_degrade as D  # noqa: E402  (轻量模块, 无 torch 依赖 -> core CI 也能跑)

FACTORS = ("signal", "trend", "govern", "liquidity", "vol", "mom_rev")


def _w(scale=1.0):
    return {k: scale / len(FACTORS) for k in FACTORS}


def _mk_version(root, day, *, live=True, model=True, weights="default",
                ok=True, meta_raw=None, subdirs=None):
    """在 `<root>/drl/<day>/` 造一个版本目录（模拟生产布局）。"""
    d = os.path.join(root, "drl", str(day))
    os.makedirs(d, exist_ok=True)
    if live:
        with open(os.path.join(d, D.LIVE_MARKER_NAME), "w", encoding="utf-8") as f:
            f.write("{}")
    if model:
        with open(os.path.join(d, "model.zip"), "wb") as f:
            f.write(b"PK\x03\x04dummy")
    for s in (subdirs or []):
        os.makedirs(os.path.join(d, s), exist_ok=True)
    if meta_raw is not None:
        text = meta_raw
    else:
        w = _w() if weights == "default" else weights
        text = json.dumps({"ok": ok, "day": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                           "final_weights": w})
    with open(os.path.join(d, "train_meta.json"), "w", encoding="utf-8") as f:
        f.write(text)
    return d


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例: 数据根指向 tmp（指针+账本都落 tmp, 不污染生产）+ 清告警计数。"""
    dataguard.reset_warnings()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    for v in ("DRL_DEGRADE_LEDGER", "DRL_VALIDATION_LEDGER", "DRL_MODEL_POINTER",
              "DRL_LIVE_MARKER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(D, "FALLBACK_LOOKBACK_DAYS", 45)
    yield tmp_path
    dataguard.reset_warnings()


def _events(tmp):
    p = os.path.join(str(tmp), D.EVENT_LEDGER_NAME)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _pointer(tmp):
    p = os.path.join(str(tmp), "drl", D.POINTER_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _set_pointer(tmp, day):
    os.makedirs(os.path.join(str(tmp), "drl"), exist_ok=True)
    with open(os.path.join(str(tmp), "drl", D.POINTER_NAME), "w", encoding="utf-8") as f:
        json.dump({"day": str(day), "source": "test"}, f)


# =====================================================================
# 结构性判据
# =====================================================================

class TestStructuralUsability:
    def test_usable_requires_marker_model_meta_weights(self, tmp_path):
        d = _mk_version(tmp_path, "20260905")
        assert D.version_usable("20260905") is True, d

    def test_missing_model_zip_not_usable(self, tmp_path):
        _mk_version(tmp_path, "20260905", model=False)
        assert D.version_usable("20260905") is False

    def test_meta_not_ok_not_usable(self, tmp_path):
        _mk_version(tmp_path, "20260905", ok=False)
        assert D.version_usable("20260905") is False

    def test_empty_final_weights_not_usable(self, tmp_path):
        """ok=True 但 final_weights 为空 -> 不可用（否则会把空权重部署出去）。"""
        _mk_version(tmp_path, "20260905", weights={})
        assert D.version_usable("20260905") is False

    def test_corrupt_meta_not_usable(self, tmp_path):
        _mk_version(tmp_path, "20260905", meta_raw="{not json")
        assert D.version_usable("20260905") is False

    def test_missing_meta_not_usable(self, tmp_path):
        d = _mk_version(tmp_path, "20260905")
        os.remove(os.path.join(d, "train_meta.json"))
        assert D.version_usable("20260905") is False

    @pytest.mark.parametrize("bad", ["", "2026090", "202609051", "abcdefgh", None])
    def test_bad_day_format_not_usable(self, tmp_path, bad):
        _mk_version(tmp_path, "20260905")
        assert D.version_usable(bad) is False
        assert D.is_live_version(bad) is False

    @pytest.mark.parametrize("form", ["20260905", "2026-09-05"])
    def test_hyphenated_day_is_normalized(self, tmp_path, form):
        """`resolve()` 收的是 "YYYY-MM-DD", 故带横线形式必须被接受（内部归一化）。"""
        _mk_version(tmp_path, "20260905")
        assert D.is_live_version(form) is True
        assert D.version_usable(form) is True


# =====================================================================
# ★ 遗留回测产物防线（本文件的核心）
# =====================================================================

class TestLegacyArtifactGuard:
    """生产实测: 遗留目录 0/17 有实盘标记, 实盘目录 12/12 有 —— 零重叠。"""

    def test_legacy_dir_satisfies_naive_but_not_live(self, tmp_path):
        """遗留目录满足朴素判据（有 model.zip + ok=True）, 但不是实盘版本。"""
        _mk_version(tmp_path, "20181019", live=False, subdirs=["ensemble"])
        assert D.is_live_version("20181019") is False
        assert D.version_usable("20181019") is False, "遗留回测产物绝不能算可用版本"
        assert D.version_usable("20181019", require_live=False) is True, \
            "朴素判据下确实是 True —— 正说明必须叠加实盘判据"

    def test_liveness_guard_alone_is_sufficient(self, tmp_path, monkeypatch):
        """**决定性用例**: 把回退窗口放到无限大, 只剩实盘判据也绝不能选中遗留目录。"""
        monkeypatch.setattr(D, "FALLBACK_LOOKBACK_DAYS", 999999)
        assert D.latest_valid("20260909") is None

        # 真实的 6 个朴素可用遗留目录（值取自生产实测; 第 7 个是缺标记的实盘日 20260825）
        for day in ("20181019", "20190628", "20200323", "20200630", "20210210", "20211231"):
            _mk_version(tmp_path, day, live=False, subdirs=["ensemble"])
        # 一个实盘版本, 但模型文件缺失（= 指针那天坏了）
        _mk_version(tmp_path, "20260908", model=False)

        assert D.latest_valid("20260909") is None, "不得回退到 2021 年回测模型"
        r = D.resolve("2026-09-09", train_ok=False)
        assert r["halt"] is True, "实盘版本全不可用 -> 必须 L3 暂停, 而不是用遗留模型"
        assert r["level"] == D.LEVEL_HALT
        assert r["source_day"] is None

        ev = _events(tmp_path)
        assert len(ev) == 1 and ev[0]["level"] == D.LEVEL_HALT
        assert ev[0]["skipped_not_live"] == [
            "20211231", "20210210", "20200630", "20200323", "20190628", "20181019"]
        assert ev[0]["scanned"] == 7

    def test_real_production_layout(self, tmp_path, monkeypatch):
        """照生产实测布局复刻 (2026-09-19 快照), 走**默认窗口** 45 天。"""
        monkeypatch.delenv("DRL_LIVE_MARKER", raising=False)
        legacy = ["20181019", "20190628", "20200323", "20200630", "20210210", "20211231"]
        for day in legacy:
            _mk_version(tmp_path, day, live=False, subdirs=["ensemble"])
        # 实盘: 08-25~09-05 有模型; 08-31 / 09-07 / 09-08 训练失败（只有标记, 无模型）
        for day in ("20260825", "20260826", "20260827", "20260828", "20260830",
                    "20260901", "20260902", "20260903", "20260904", "20260905"):
            _mk_version(tmp_path, day)
        for day in ("20260831", "20260907", "20260908"):
            _mk_version(tmp_path, day, model=False)

        # 09-09 训练失败, 无指针 -> L1 无指针可用 -> L2 回退到最近的实盘可用版本 20260905
        r = D.resolve("2026-09-09", train_ok=False)
        assert r["level"] == D.LEVEL_FALLBACK
        assert r["source_day"] == "20260905", "必须回退到最近的**实盘**版本"
        assert r["effective_weights"] == _w()
        assert _pointer(tmp_path)["day"] == "20260905"

        ev = _events(tmp_path)[0]
        assert ev["level"] == D.LEVEL_FALLBACK
        assert ev["window_days"] == 45
        # 遗留目录全在窗口外（5 年前）-> 连"非实盘"都不需要出手
        assert set(ev["skipped_out_of_window"]) == set(legacy)
        assert ev["skipped_not_live"] == []

    def test_in_window_legacy_is_rejected_by_liveness(self, tmp_path):
        """窗口内的遗留目录（近 45 天内）必须被**实盘判据**挡掉。"""
        _mk_version(tmp_path, "20260901", live=False)   # 在窗口内, 但不是实盘版本
        assert D.latest_valid("20260909") is None
        r = D.resolve("2026-09-09", train_ok=False)
        assert r["halt"] is True
        assert _events(tmp_path)[0]["skipped_not_live"] == ["20260901"]

    def test_live_version_is_selected(self, tmp_path):
        _mk_version(tmp_path, "20260904", live=False)
        _mk_version(tmp_path, "20260905", live=True)
        assert D.latest_valid("20260909") == "20260905"

    def test_window_excludes_old_live_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "FALLBACK_LOOKBACK_DAYS", 10)
        _mk_version(tmp_path, "20260701")          # 70 天前, 实盘但超窗
        _mk_version(tmp_path, "20260905")
        assert D.latest_valid("20260909") == "20260905"
        monkeypatch.setattr(D, "FALLBACK_LOOKBACK_DAYS", 1)
        assert D.latest_valid("20260909") is None

    def test_marker_name_is_env_overridable(self, tmp_path, monkeypatch):
        _mk_version(tmp_path, "20260905", live=False)
        monkeypatch.setenv("DRL_LIVE_MARKER", "train_meta.json")
        assert D.is_live_version("20260905") is True
        assert D.version_usable("20260905") is True

    def test_exclude_day_and_before_day_bounds(self, tmp_path):
        _mk_version(tmp_path, "20260905")
        _mk_version(tmp_path, "20260908")
        assert D.latest_valid("20260909", exclude_day="20260908") == "20260905"
        assert D.latest_valid("20260908") == "20260905", "必须严格早于 before_day"
        assert D.latest_valid("20260906") == "20260905"


# =====================================================================
# 四级链
# =====================================================================

class TestLevel0Deploy:
    def test_success_deploys_and_writes_pointer_without_event(self, tmp_path):
        r = D.resolve("2026-09-05", train_ok=True, final_weights=_w())
        assert r["ok"] is True and r["halt"] is False
        assert r["level"] == D.LEVEL_OK
        assert r["effective_weights"] == _w()
        assert r["source_day"] == "20260905"
        assert r["recovered_from"] is None
        assert _pointer(tmp_path)["day"] == "20260905"
        assert _events(tmp_path) == [], "L0 正常部署不该写降级事件（否则账本被正常日淹没）"

    def test_recovery_is_recorded(self, tmp_path):
        """可逆性: 上一轮处于降级(指针 level>0) + 训练成功 -> 记一条 level=0 恢复事件。"""
        D.save_pointer("20260904", "fallback", level=D.LEVEL_FALLBACK)
        r = D.resolve("2026-09-05", train_ok=True, final_weights=_w(1.2))
        assert r["level"] == D.LEVEL_OK
        assert r["recovered_from"] == "20260904"
        assert r["recovered_from_level"] == D.LEVEL_FALLBACK
        assert _pointer(tmp_path)["day"] == "20260905"
        assert _pointer(tmp_path)["level"] == D.LEVEL_OK
        ev = _events(tmp_path)
        assert len(ev) == 1
        assert ev[0]["level"] == D.LEVEL_OK
        assert ev[0]["recovered_from"] == "20260904"
        assert ev[0]["recovered_from_level"] == D.LEVEL_FALLBACK
        assert ev[0]["severity"] == "INFO"
        assert ev[0]["blocked_plan"] is False

    def test_recovery_detected_from_level_not_from_file_proxy(self, tmp_path):
        """★ 关键: L1 保留旧模型时原版本文件**完好**, 但那确实处于降级 —— 必须记恢复。

        旧实现用"原指针版本文件是否不可用"作判据(弱代理), 这种情形会**漏记恢复**,
        可逆性轨迹断裂。现改为读指针里显式记录的 level。
        """
        _mk_version(tmp_path, "20260904")          # 文件完好
        _set_pointer(tmp_path, "20260904")
        assert D.resolve("2026-09-05", train_ok=False)["level"] == D.LEVEL_RETAIN
        assert _pointer(tmp_path)["day"] == "20260904", "L1 不得换模型"
        assert _pointer(tmp_path)["level"] == D.LEVEL_RETAIN, "但必须记下'正处于降级'"
        assert D.version_usable("20260904") is True, "旧模型文件确实完好"

        r = D.resolve("2026-09-08", train_ok=True, final_weights=_w(1.1))
        assert r["recovered_from"] == "20260904"
        assert r["recovered_from_level"] == D.LEVEL_RETAIN
        assert [e["level"] for e in _events(tmp_path)] == [D.LEVEL_RETAIN, D.LEVEL_OK]

    def test_success_but_no_weights_falls_back_into_chain(self, tmp_path):
        """train_ok=True 但权重为空 -> 不得当 L0 部署（否则部署空权重）。"""
        _mk_version(tmp_path, "20260904")
        _set_pointer(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=True, final_weights={})
        assert r["level"] == D.LEVEL_RETAIN
        assert r["source_day"] == "20260904"


class TestLevel1Retain:
    def test_retain_current_model(self, tmp_path):
        _mk_version(tmp_path, "20260904")
        _set_pointer(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["halt"] is False
        assert r["level"] == D.LEVEL_RETAIN
        assert r["source_day"] == "20260904"
        assert r["effective_weights"] == _w(), "生效权重必须 == 指针版本权重"
        assert _pointer(tmp_path)["day"] == "20260904", "L1 不得改动指针"
        assert dataguard.warned_count(D.LEVEL_WARN_KEY[1]) == 1
        ev = _events(tmp_path)
        assert len(ev) == 1 and ev[0]["level"] == D.LEVEL_RETAIN
        assert ev[0]["severity"] == "WARNING"
        assert ev[0]["effective_source_day"] == "20260904"

    def test_fail_reason_is_recorded_verbatim(self, tmp_path):
        """上游失败原因必须原样进账本（否则事后无法区分"数据不足"与"崩溃"）。"""
        _mk_version(tmp_path, "20260904")
        _set_pointer(tmp_path, "20260904")
        D.resolve("2026-09-05", train_ok=False, fail_reason="数据不足 (<15 日): 3 行")
        assert "数据不足 (<15 日): 3 行" in _events(tmp_path)[0]["trigger"]

    def test_pointer_to_legacy_version_is_not_retained(self, tmp_path):
        """指针（被人为/历史地）指向遗留目录 -> L1 不得认它, 必须继续往下走。"""
        _mk_version(tmp_path, "20260904", live=False)
        _set_pointer(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["level"] == D.LEVEL_HALT, "遗留目录不能被当作'当前模型'保留"


class TestLevel2Fallback:
    def test_fallback_when_pointer_missing(self, tmp_path):
        _mk_version(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["level"] == D.LEVEL_FALLBACK
        assert r["source_day"] == "20260904"
        assert r["effective_weights"] == _w()
        assert _pointer(tmp_path)["day"] == "20260904"
        assert dataguard.warned_count(D.LEVEL_WARN_KEY[2]) == 1
        ev = _events(tmp_path)[0]
        assert ev["level"] == D.LEVEL_FALLBACK and ev["severity"] == "ERROR"
        assert ev["window_days"] == 45
        assert ev["scanned"] >= 1
        assert _pointer(tmp_path)["level"] == D.LEVEL_FALLBACK

    def test_fallback_when_pointer_version_broken(self, tmp_path):
        _mk_version(tmp_path, "20260904", model=False)   # 指针那天模型坏了
        _mk_version(tmp_path, "20260903")
        _set_pointer(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["level"] == D.LEVEL_FALLBACK
        assert r["source_day"] == "20260903"
        assert _pointer(tmp_path)["day"] == "20260903"

    def test_chain_continuity_l2_then_l1(self, tmp_path):
        """L2 写过指针后, 次日训练再失败应走 L1（保留当前）而非再 L2 —— 链是连续的。"""
        _mk_version(tmp_path, "20260904")
        d1 = D.resolve("2026-09-05", train_ok=False)
        assert d1["level"] == D.LEVEL_FALLBACK
        d2 = D.resolve("2026-09-06", train_ok=False)
        assert d2["level"] == D.LEVEL_RETAIN
        assert d2["source_day"] == "20260904"
        assert [e["level"] for e in _events(tmp_path)] == [D.LEVEL_FALLBACK, D.LEVEL_RETAIN]


class TestLevel3Halt:
    def test_halt_blocks_plan(self, tmp_path):
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["halt"] is True
        assert r["level"] == D.LEVEL_HALT
        assert r["effective_weights"] is None
        assert r["source_day"] is None
        assert dataguard.warned_count(D.LEVEL_WARN_KEY[3]) == 1
        ev = _events(tmp_path)
        assert len(ev) == 1
        assert ev[0]["severity"] == "CRITICAL"
        assert ev[0]["blocked_plan"] is True, "L3 的核心语义 = 阻断当日 plan"
        assert "暂停交易" in ev[0]["action"]

    def test_empty_drl_root_does_not_raise(self, tmp_path):
        assert not os.path.isdir(os.path.join(str(tmp_path), "drl"))
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["level"] == D.LEVEL_HALT


# =====================================================================
# 可逆性 / 账本 / 验证记录
# =====================================================================

class TestReversibility:
    def test_full_degrade_then_recover_trajectory(self, tmp_path):
        """L2 回退 -> L1 保留 -> L0 恢复, 账本轨迹连续可查（用户设计要点①）。"""
        _mk_version(tmp_path, "20260904")
        seq = [
            D.resolve("2026-09-05", train_ok=False),                        # L2 回退
            D.resolve("2026-09-06", train_ok=False),                        # L1 保留
            D.resolve("2026-09-07", train_ok=True, final_weights=_w(1.1)),  # L0 恢复
        ]
        assert [s["level"] for s in seq] == [D.LEVEL_FALLBACK, D.LEVEL_RETAIN, D.LEVEL_OK]
        assert seq[2]["recovered_from"] == "20260904"
        assert seq[2]["recovered_from_level"] == D.LEVEL_RETAIN
        levels = [e["level"] for e in _events(tmp_path)]
        assert levels == [D.LEVEL_FALLBACK, D.LEVEL_RETAIN, D.LEVEL_OK], "三级别都要留痕"
        assert _pointer(tmp_path)["day"] == "20260907"
        assert _pointer(tmp_path)["level"] == D.LEVEL_OK

    def test_no_degrade_after_successful_deploy_keeps_pointer(self, tmp_path):
        D.resolve("2026-09-05", train_ok=True, final_weights=_w())
        D.resolve("2026-09-08", train_ok=True, final_weights=_w(0.9))
        assert _pointer(tmp_path)["day"] == "20260908"
        assert _pointer(tmp_path)["level"] == D.LEVEL_OK
        assert _events(tmp_path) == [], "连续正常部署不得写事件(否则账本被正常日淹没)"

    def test_halt_then_recover_records_level3_source(self, tmp_path):
        """L3 暂停 -> 次日训练成功: 恢复事件必须指明是从 L3 恢复的。"""
        assert D.resolve("2026-09-05", train_ok=False)["halt"] is True
        assert _pointer(tmp_path)["level"] == D.LEVEL_HALT
        r = D.resolve("2026-09-08", train_ok=True, final_weights=_w())
        assert r["recovered_from_level"] == D.LEVEL_HALT
        assert [e["level"] for e in _events(tmp_path)] == [D.LEVEL_HALT, D.LEVEL_OK]


class TestLedger:
    def test_every_event_is_one_valid_json_line(self, tmp_path):
        _mk_version(tmp_path, "20260904")
        for day in ("2026-09-05", "2026-09-06", "2026-09-07"):
            D.resolve(day, train_ok=False)
        p = os.path.join(str(tmp_path), D.EVENT_LEDGER_NAME)
        with open(p, encoding="utf-8") as f:
            raw = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(raw) == 3
        for ln in raw:
            rec = json.loads(ln)
            for k in ("at", "day", "level", "level_name", "severity", "trigger",
                      "action", "effective_source_day", "blocked_plan"):
                assert k in rec, f"账本缺字段 {k}: {rec}"
            assert rec["day"].isdigit() and len(rec["day"]) == 8, "账本日必须是 8 位无横线"

    def test_ledger_path_env_override(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "custom_events.jsonl")
        monkeypatch.setenv("DRL_DEGRADE_LEDGER", custom)
        D.resolve("2026-09-05", train_ok=False)
        assert os.path.isfile(custom)
        assert not os.path.exists(os.path.join(str(tmp_path), D.EVENT_LEDGER_NAME))

    def test_pointer_env_override(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "elsewhere" / "ptr.json")
        monkeypatch.setenv("DRL_MODEL_POINTER", custom)
        D.resolve("2026-09-05", train_ok=True, final_weights=_w())
        assert os.path.isfile(custom)

    def test_record_event_never_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        """账本路径不可写（父路径是文件）时, 必须静默降级而不是抛异常打断主链路。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        monkeypatch.setenv("DRL_DEGRADE_LEDGER", str(blocker / "sub" / "events.jsonl"))
        rec = D.record_event(D.LEVEL_RETAIN, "x", "y", "2026-09-05")   # 不得抛异常
        assert rec["level"] == D.LEVEL_RETAIN

    def test_resolve_survives_unwritable_ledger_and_pointer(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker2"
        blocker.write_text("i am a file", encoding="utf-8")
        monkeypatch.setenv("DRL_DEGRADE_LEDGER", str(blocker / "sub" / "events.jsonl"))
        monkeypatch.setenv("DRL_MODEL_POINTER", str(blocker / "sub" / "ptr.json"))
        _mk_version(tmp_path, "20260904")
        r = D.resolve("2026-09-05", train_ok=False)
        assert r["ok"] is True and r["level"] == D.LEVEL_FALLBACK
        assert r["source_day"] == "20260904"

    def test_record_validation_records_numbers_only(self, tmp_path):
        D.record_validation("2026-09-05", {"val_ic": 0.031, "val_sharpe": 0.82,
                                           "note": "text"})
        p = os.path.join(str(tmp_path), D.VALIDATION_LEDGER_NAME)
        rec = json.loads(open(p, encoding="utf-8").read().strip())
        assert rec["threshold_applied"] is False, \
            "用户明确要求本批次**不得**施加'验证不通过'阈值（METHOD-1）"
        assert rec["metrics"]["val_ic"] == pytest.approx(0.031)
        assert rec["metrics"]["note"] == "text"

    def test_validation_metrics_do_not_trigger_degrade(self, tmp_path):
        """只记录数值: 就算验证指标很差, 也不得改变分级（阈值待标定）。"""
        _mk_version(tmp_path, "20260904")
        _set_pointer(tmp_path, "20260904")
        D.record_validation("2026-09-05", {"val_ic": -9.9, "val_sharpe": -5.0})
        r = D.resolve("2026-09-05", train_ok=True, final_weights=_w())
        assert r["level"] == D.LEVEL_OK, "验证数值不得参与分级"


# =====================================================================
# drl_train 接线（源码级, 避免 import torch）
# =====================================================================

class TestNoSilentFailurePathsInTrain:
    """断言结构不变量: 训练失败的两条路径都必须经过降级链, 而不是静默 return。"""

    @staticmethod
    def _src():
        with open(os.path.join(_SRC, "drl_train.py"), encoding="utf-8") as f:
            return f.read()

    def test_import_present(self):
        assert "import drl_degrade" in self._src()

    def test_helper_defined_and_used_twice(self):
        src = self._src()
        assert "def _degrade_on_failure(" in src
        assert src.count("_degrade_on_failure(") == 3, \
            "1 处定义 + 2 处调用（数据不足 / 外层 except）"

    def test_every_failure_return_carries_degrade(self):
        src = self._src()
        assert src.count('"degrade": _degrade_on_failure') == 1       # 数据不足
        assert src.count('"degrade": _dec}') == 1                     # 外层 except
        assert src.count('"degrade": ') == 2

    def test_data_insufficient_path_goes_through_chain(self):
        src = self._src()
        i = src.index("数据不足 (<15 日)")
        assert "_degrade_on_failure" in src[i:i + 700]

    def test_outer_except_replaces_bare_failure_return(self):
        src = self._src()
        i = src.index("DRL 训练异常")
        seg = src[i:i + 900]
        assert "_degrade_on_failure" in seg and '"degrade"' in seg

    def test_resolve_is_called_before_target_plan(self):
        """L3 要阻断当日 plan, 决策必须早于 _build_target_plan。"""
        src = self._src()
        assert src.index("_dec = drl_degrade.resolve(") < src.index(
            "plan = _build_target_plan(")

    def test_halt_skips_plan_construction(self):
        src = self._src()
        i = src.index('if _dec.get("halt"):')
        seg = src[i:i + 400]
        assert "blocked_by_degrade" in seg
        assert "_build_target_plan" not in seg, "L3 分支内不得构建 plan"
