# -*- coding: utf-8 -*-
"""回填**自动触发**判据的守卫 (2026-09-25, 用户清单第 1、2 项)。

## 判据(用户指定): A 且 B

| 判据 | 含义 |
|---|---|
| **A** 引擎缺口 | `engine_day < expected_day` |
| **B** 存储缺口 | `h5i_watermark < expected_day` |

**两个单条件各自都会误触发**, 故必须**同时**成立:

- 只有 A(引擎缺口但 h5i 已补过) ⇒ **已经补过了, 再补是重复动作**。
  这正是 2026-09-25 补完 09-23/09-24 之后的状态, **也是用户给的验收条件**。
- 只有 B(h5i 落后但引擎已追平) ⇒ 那是**摄入链路**的问题,
  拿替代源补会**掩盖主源故障** —— 该修的是摄入, 不是换个源把水位推上去。

## 另外两条刻意的"不触发"

- **默认关闭**(`BACKFILL_ENABLED=0`): 回填写生产行情库, 属不可逆动作;
- **弱日历 ⇒ 不可判定**: 日历强度非 `official` 时, "最后一个已收盘交易日"会退化成
  "最后一个有数据的日子" ⇒ 落后恒为 0 ⇒ A 恒假、永不触发, **而报告一切正常**。
  这是 DISC-2 ⑥ 的形状, 故显式判为"不可判定"而不是安静地返回"不需要补"。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import backfill_trigger as BT  # noqa: E402


class TestTheAcceptanceCriterion:
    """用户给的验收: `engine_covered_days=09-22, h5i=09-24` 时**不触发**。"""

    def test_already_backfilled_does_not_trigger(self):
        """**核心验收**: A 真(引擎 09-22)但 B 假(h5i 已 09-24) ⇒ 不触发。

        若判据写成"只要引擎有缺口就补", 这里会每轮盘后都去拉一次全市场
        5000+ 只(约 25 分钟), 纯属浪费且反复覆盖同一批数据。
        """
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-24",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False
        assert r["action"] == "no_gap"
        assert r["criteria"] == {"A_engine_gap": True, "B_h5i_gap": False}
        assert any("已经补过了" in x for x in r["reasons"]), r["reasons"]

    def test_both_gaps_trigger(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is True and r["action"] == "trigger"
        assert r["criteria"] == {"A_engine_gap": True, "B_h5i_gap": True}
        assert r["missing_days"] == ["20260923", "20260924"], r["missing_days"]

    def test_only_storage_gap_does_not_trigger(self):
        """B 真 A 假 ⇒ 不触发 —— 那是摄入链路问题, 补了会掩盖主源故障。"""
        r = BT.decide(engine_day="2026-09-24", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False
        assert any("摄入链路" in x for x in r["reasons"]), r["reasons"]

    def test_caught_up_does_not_trigger(self):
        r = BT.decide(engine_day="2026-09-24", h5i_watermark="2026-09-24",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False and r["action"] == "no_gap"


class TestDefaultOff:
    def test_disabled_by_default(self):
        """**默认关闭**, 且关闭时不做任何推断(disabled 是第一个分支)。"""
        assert BT.is_enabled({}) is False, "默认必须是关闭"
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=False)
        assert r["action"] == "disabled" and r["triggered"] is False
        assert r["missing_days"] == [], "关闭时不该算出待补日"

    @pytest.mark.parametrize("val,expect", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("no", False),
        (" 1 ", True),          # 前后空白应容忍
        ("2", False),           # 只认显式的正数标志, 不认任意真值
    ])
    def test_env_parsing_is_strict(self, val, expect):
        assert BT.is_enabled({BT.ENABLED_ENV: val}) is expect, val

    def test_lookback_parsing_and_bad_values(self):
        assert BT.lookback_days({BT.LOOKBACK_ENV: "3"}) == 3
        # 坏值不该炸, 也不该变成无穷大
        assert BT.lookback_days({BT.LOOKBACK_ENV: "abc"}) == BT.DEFAULT_LOOKBACK_DAYS
        assert BT.lookback_days({BT.LOOKBACK_ENV: "-5"}) == 0


class TestWeakCalendarIsUndecidable:
    """弱日历必须判为**不可判定**, 而不是安静地"不需要补"。"""

    def test_non_official_calendar_refuses(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", calendar_strength="data_derived")
        assert r["action"] == "refuse_weak_calendar"
        assert r["triggered"] is False
        txt = " ".join(r["reasons"])
        assert "不可判定" in txt, "必须点明这是**不可判定**, 不是不需要补"
        assert "退化" in txt or "恒为 0" in txt, "应说清机理: 落后会恒为 0"

    def test_official_calendar_proceeds(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", calendar_strength="official")
        assert r["action"] == "trigger"


class TestMissingValuesAreNotGuessed:
    """缺信息时**不补**(不据缺失做动作) —— 与 DISC-1「宁可 None 不猜」同一立场。"""

    @pytest.mark.parametrize("engine_day,h5i_wm,expected", [
        (None, "2026-09-24", "2026-09-24"),
        ("2026-09-22", None, "2026-09-24"),
        ("2026-09-22", "2026-09-24", None),
        ("bad", "2026-09-24", "2026-09-24"),           # 格式不对也算取不到
        ("2026-09-22", "2026-09-24", "not-a-date"),
    ])
    def test_cannot_decide(self, engine_day, h5i_wm, expected):
        r = BT.decide(engine_day=engine_day, h5i_watermark=h5i_wm,
                      expected_day=expected, enabled=True)
        assert r["action"] == "cannot_decide", (engine_day, h5i_wm, expected)
        assert r["triggered"] is False

    def test_d8_normalizes_and_rejects(self):
        assert BT._d8("2026-09-24") == "20260924"
        assert BT._d8("2026/09/24") == "20260924"
        assert BT._d8("20260924") == "20260924"
        assert BT._d8(None) is None
        assert BT._d8("bad") is None
        assert BT._d8("2026-09") is None


class TestMissingDaysUsesTheCalendarNotGuessing:
    """待补交易日必须**取自官方日历**, 不猜。"""

    def test_returns_trading_days_between_watermark_and_expected(self):
        """真实日历上, 09-22 -> 09-24 之间应是 09-23 与 09-24。"""
        got = BT.missing_days("2026-09-22", "2026-09-24")
        assert got == ["20260923", "20260924"], got

    def test_watermark_equals_expected_returns_empty(self):
        assert BT.missing_days("2026-09-24", "2026-09-24") == []

    def test_watermark_ahead_returns_empty(self):
        assert BT.missing_days("2026-09-25", "2026-09-24") == []

    def test_bad_inputs_return_empty_not_raise(self):
        assert BT.missing_days(None, "2026-09-24") == []
        assert BT.missing_days("2026-09-22", None) == []
        assert BT.missing_days("bad", "2026-09-24") == []

    def test_lookback_zero_returns_empty(self):
        """`lookback=0` 表示不回溯 —— 返回空(而不是忽略该限制)。"""
        assert BT.missing_days("2026-09-22", "2026-09-24", lookback=0) == []

    def test_lookback_limits_span(self):
        """很大的 lookback 应把很久以前的空洞都算进来; 很小的只留最近几天。"""
        wide = BT.missing_days("2026-09-01", "2026-09-24", lookback=365)
        narrow = BT.missing_days("2026-09-01", "2026-09-24", lookback=3,
                                 today="2026-09-25")
        assert len(wide) > len(narrow), (len(wide), len(narrow))
        assert all(d >= "20260922" for d in narrow), narrow


class TestSwitchFileBecauseEnvAloneIsNotEnough:
    """开关键必须能通过**文件**打开 (2026-09-25 实测踩到)。

    ## 为什么光有环境变量不够

    `run_daily` 由**守护进程**(Windows 服务)以 `subprocess.Popen(cmd, cwd=_BASE, ...)`
    拉起, **没有传 `env=`** ⇒ 它继承的是**守护进程的环境**。
    于是: 在交互式 shell 里设 `$env:BACKFILL_ENABLED=1` 再手工跑 `run_daily`
    **有效**; 但**守护自动拉起的那次完全看不到** —— 而我们要的恰恰是自动触发。

    没有文件这条路, 唯一的开启方式就变成"改服务的环境变量并重启服务",
    既难验证也容易被忘掉(**改完以为开了, 实际没生效** —— 本仓最忌讳的形状)。

    ## 取值优先级(与 `factor_gate` 同一约定)

        环境变量 > 配置文件(data/backfill_switch.json) > 代码默认值(关)
    """

    def _write(self, tmp_path, text, encoding="utf-8"):
        p = os.path.join(str(tmp_path), "sw.json")
        with open(p, "w", encoding=encoding) as f:
            f.write(text)
        return p

    def test_no_file_no_env_means_disabled(self, tmp_path):
        assert BT.is_enabled({}, switch_fp=os.path.join(str(tmp_path), "none.json")) is False

    def test_file_can_enable(self, tmp_path):
        p = self._write(tmp_path, '{"enabled": true}')
        assert BT.is_enabled({}, switch_fp=p) is True

    def test_env_overrides_file(self, tmp_path):
        """环境变量优先 —— 它让"手工跑一次带开关"不必改文件。"""
        p = self._write(tmp_path, '{"enabled": true}')
        assert BT.is_enabled({BT.ENABLED_ENV: "0"}, switch_fp=p) is False
        p2 = self._write(tmp_path, '{"enabled": false}')
        assert BT.is_enabled({BT.ENABLED_ENV: "1"}, switch_fp=p2) is True

    def test_bom_must_be_tolerated(self, tmp_path):
        """**必须容忍 UTF-8 BOM** —— PowerShell 写这个文件一定会带 BOM。

        实测: 普通 `utf-8` 读带 BOM 的文件会抛 `Unexpected UTF-8 BOM`,
        被 `except` 吞掉 ⇒ **文件明明写着 `enabled: true`, 却读成"没配"**,
        开关静默保持关闭。这与 `metrics_server` 里 `fusion_health.json`
        踩过的是**同一个坑**。
        """
        p = os.path.join(str(tmp_path), "bom.json")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + '{"enabled": true}'.encode("utf-8"))
        assert BT._load_switch_file(p) == {"enabled": True}, "带 BOM 的文件读不出来"
        assert BT.is_enabled({}, switch_fp=p) is True

    def test_bad_file_shapes_fall_back_to_disabled(self, tmp_path):
        """坏文件/非 dict 一律当"没配" ⇒ **关**。

        **绝不因为读不到就当成开启** —— 那会让一个手滑的坏文件变成"自动写生产库"。
        方向性很重要: 宁可少补一次(人工可补), 不可误补一次(不可逆)。
        """
        assert BT.is_enabled({}, switch_fp=self._write(tmp_path, "{bad")) is False
        assert BT.is_enabled({}, switch_fp=self._write(tmp_path, "[1,2,3]")) is False
        assert BT.is_enabled({}, switch_fp=self._write(tmp_path, "null")) is False
        assert BT.is_enabled({}, switch_fp=self._write(tmp_path, '{"enabled": "maybe"}')) is False
        assert BT.is_enabled({}, switch_fp=self._write(tmp_path, "{}")) is False

    def test_lookback_from_file(self, tmp_path):
        p = self._write(tmp_path, '{"lookback_days": 3}')
        assert BT.lookback_days({}, switch_fp=p) == 3
        # 环境变量仍优先
        assert BT.lookback_days({BT.LOOKBACK_ENV: "7"}, switch_fp=p) == 7

    def test_switch_path_is_under_data_and_not_in_git(self):
        """开关文件放在 `data/`(gitignored)—— 它含机器本地状态, 不该进版本库。"""
        assert BT.SWITCH_FP.replace("\\", "/").endswith("data/backfill_switch.json"), \
            BT.SWITCH_FP
        gi = os.path.join(_REPO, ".gitignore")
        assert os.path.isfile(gi)
        txt = open(gi, encoding="utf-8").read()
        assert "data/" in txt, "data/ 应被 gitignore(开关文件是本地状态)"


class TestRunDailyWiring:
    """接线必须存在, 且**默认关闭**这个语义要在源码里看得到。"""

    def test_run_daily_has_the_step(self):
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        assert 'report["steps"]["backfill_trigger"]' in src, (
            "run_daily 没有把判定结果写进回执 —— 那它就不可见")
        assert "import backfill_trigger" in src
        assert "BACKFILL_ENABLED" in src or "_BT.is_enabled()" in src, (
            "必须走 is_enabled(), 即默认关闭")

    def test_run_daily_uses_engine_bars_sync_baseline(self):
        """判据 B 的 h5i 水位应复用 `engine_bars_sync` 刚报的 `h5i_max_before`。

        为什么强调这一点: 那是**本次摄入前**的水位, 语义正好是判据 B 要的
        "存储缺口"; 若改用摄入**之后**的水位, 会把刚补上的算成"已追平"而漏触发。
        """
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        i = src.find("backfill_trigger")
        assert i > 0
        block = src[max(0, i - 200):i + 2600]
        assert "h5i_max_before" in block, "未复用 h5i_max_before 作判据 B 的输入"

    def test_gate_halt_skips_backfill(self):
        """门禁 HALT 时**不补** —— 数据源不可信时换个源再抓只会灌进不可信数据。"""
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        i = src.find("_bf[\"run\"]")
        assert i > 0
        block = src[max(0, i - 500):i]
        assert "ds_allow" in block, "补数没有被门禁 HALT 拦住"

    def test_interpreter_probe_is_by_import_not_by_path(self):
        """找取数解释器必须**真的 import 一次**, 不能只看文件存在。

        只看路径正是本仓 DISC-1 禁止的"看着像就算" —— 一个存在的解释器未必装了
        baostock, 而失败会以"取数中途报错"的形式出现, 比"找不到解释器"难查得多。
        """
        import inspect
        src = inspect.getsource(BT.__dict__.get("__name__") and __import__(
            "run_daily")._backfill_interpreter)
        assert "import baostock" in src, "应按 import 实测, 而不是按路径判"
        assert "returncode" in src, "应检查 import 的返回码"
