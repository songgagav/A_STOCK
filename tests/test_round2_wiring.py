# -*- coding: utf-8 -*-
"""第二轮接线 (虚拟盘 / run_daily / 收益评估) 的**接线存在性**回归测试。

为什么要有这个文件: 接线代码写在 `run_daily.run_daily()` 的函数体里, 无法在不跑
整条日更链路的前提下被单测覆盖。但"接线被误删/改名"是本仓真实发生过的一类事故
(如 `_plan_to_targets` 丢字段、f_ml 回退链断链)。故这里用**源码级断言**锁住:

  · 三个新步骤确实写在 run_daily 里;
  · 三个步骤都能**独立容错**(有 try/except, 不会拖垮主链路);
  · 开关存在于 config, 且 0 表示关闭;
  · 引擎侧确实调用了卖侧闸门与死手开关;
  · 收益门槛与回看窗口的一致性(见 test_factor_hypothesis_eval 的同名断言)。
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


def _src(mod_name: str) -> str:
    import importlib
    m = importlib.import_module(mod_name)
    return inspect.getsource(m)


@pytest.fixture(scope="module")
def _run_daily_src():
    return _src("run_daily")


class TestRunDailySteps:
    @pytest.fixture(autouse=True)
    def src(self, _run_daily_src):
        self._src_text = _run_daily_src

    def test_step_is_wired(self):
        for step in ("portfolio_backtest", "regime_scenarios", "factor_hypotheses"):
            assert f'"{step}"' in self._src_text, f"run_daily 里找不到步骤 {step}(接线被误删?)"

    def test_step_is_independently_fault_tolerant(self):
        """每一步都必须在自己那段里被 try/except 包住 —— 回测/研究步骤失败
        绝不允许影响选股与持仓归档主链路。"""
        for step in ("portfolio_backtest", "regime_scenarios", "factor_hypotheses"):
            i = self._src_text.find(f'"{step}"')
            assert i > 0
            window = self._src_text[max(0, i - 1500):i]
            assert "try:" in window and "except Exception" in window, \
                f"步骤 {step} 缺少独立容错"

    def test_modules_are_importable(self):
        for mod in ("portfolio_live", "factor_hypothesis_eval", "vnpy_backtest"):
            __import__(mod)

    def test_regime_step_avoids_data_tail(self):
        """多场景回测必须避开数据末尾 —— 末尾几天必然报『未来数据不足』,
        那是日期选取问题而不是策略问题, 会让该步骤天天假失败。

        [2026-09-23 修] 本用例原先用一个**固定长度的字符窗口**
        (`_src_text[i-2500:i+800]`) 去框这段代码。我在该步骤里补了注释说明
        "持有期与 lookback 必须解耦"之后, 窗口被注释挤出边界 ⇒ 用例失败。
        **那是用例的脆弱, 不是代码的错**: 判据不该依赖"附近有多少字"。
        改为用**语句锚点**取范围: 从 `run_regime_batch(` 到该调用结束。
        """
        i = self._src_text.find("run_regime_batch(")
        assert i > 0, "找不到 run_regime_batch 调用"
        window = self._src_text[i:i + 700]
        assert "forward=True" in window, "多场景步骤未走前向窗口"
        # 前向持有期必须**显式**传入: 不传就走 PAPER 默认值, 而日期选取用的是
        # 局部变量 —— 两者漂移就会出现"选取按 A 留、校验按 B 判"。
        assert "forward_days=_fd" in window, "未显式传前向持有期"
        # 日期选取必须按**持有期**退让, 且上界要排除 `_fd + 1` 天
        # (只排除末端一天的话, pick 里最后几天仍缺未来数据 —— 2026-09-23 踩过)。
        assert "-(_fd + 1)" in self._src_text, (
            "决策日选取未按前向持有期退让: 应形如 `_all[:-(_fd + 1)]`")
        assert "[:- 1]" not in window and "[:-1]" not in window, (
            "仍在使用 `[:-1]`(只排除末端一天)—— 那不足以让最后几个决策日凑齐未来数据")


class TestConfigSwitches:
    def test_windows_present_and_zero_means_off(self):
        from config import PAPER
        for k in ("portfolio_bt_days", "vnpy_regime_days", "hypothesis_days"):
            assert k in PAPER, f"缺开关 {k}"
            assert int(PAPER[k]) >= 0

    def test_sell_gate_defaults_on(self):
        from config import PAPER
        assert bool(PAPER.get("sell_gate")) is True
        assert float(PAPER.get("sell_cash_tolerance")) > 0

    def test_eval_thresholds_have_no_hidden_defaults(self):
        """收益门槛必须显式配置 —— 缺键时 thresholds_from_config 抛错。"""
        import factor_hypothesis_eval as FE
        from config import FACTOR_HYPOTHESIS_EVAL
        assert {"min_ic", "min_icir", "min_obs_days"} <= set(FACTOR_HYPOTHESIS_EVAL)
        assert FE.thresholds_from_config()["min_obs_days"] == FACTOR_HYPOTHESIS_EVAL["min_obs_days"]


class TestLiveWiring:
    def test_realtime_engine_calls_sell_gate(self):
        s = _src("realtime_engine")
        assert "live_gates" in s and "apply_to_engine" in s

    def test_realtime_engine_beats_deadman(self):
        s = _src("realtime_engine")
        assert "deadman_switch" in s and 'beat("realtime_engine"' in s

    def test_daemon_beats_deadman(self):
        s = _src("daemon")
        assert "deadman_switch" in s and 'beat("daemon"' in s

    def test_daemon_beat_is_time_gated(self):
        """守护主循环每轮约 15s; **写账本的 beat 必须做时间闸门**, 否则账本每天多出数千行。

        [2026-09-22 改] 原实现是"在 `deadman_switch` 附近 900 字符窗口里找字面量
        `>= 60`" —— 断言的是**代码长什么样**, 而不是**行为对不对**, 所以我把
        `beat()` 挪进 `_check_deadman()` 之后它就假红了(实际闸门仍在, 且从 60s
        放宽到 1800s, 比原来更保守)。

        正确的断言分两层:
          · **写账本**的 `beat("daemon", ...)` 必须带时间闸门(否则账本膨胀);
          · **只读**的 `verdict()` 不需要闸门 —— 它不写盘, 便宜, 且
            "每个自然分钟问一次"正是死手开关该有的节奏(守护停摆 3 分钟即视为失联)。
        """
        s = _src("daemon")
        # 找出 beat("daemon" 所在的那一行, 断言它受某个时间差比较控制
        i = s.find('beat("daemon"')
        assert i > 0, "daemon 未 beat 死手开关"
        window = s[max(0, i - 700):i + 200]
        assert (">= 60" in window or ">= 60.0" in window
                or "_dm_last" in window or "DEADMAN_EVERY" in window), \
            f"beat 未见时间闸门, 账本会膨胀; 窗口内容: {window[-260:]!r}"

    def test_daemon_evaluates_deadman_verdict_independently(self):
        """**死手开关的判定必须由守护自己独立执行, 且写进日志。**

        为什么不能用 `health_state.gather()` 里那次采集代替: 那是**发布者**在做,
        而发布者就是守护自己, 且只在 5 分钟看护块里跑 —— 用一个"由被监控者自己执行、
        还要等 5 分钟"的判据去发现"被监控者已经不在了", 原理上不成立。
        日志则是**离线可读**的: 告警链整条死掉, 事后翻日志仍能看到那一刻的状态。
        """
        s = _src("daemon")
        assert "def _check_deadman" in s, "缺少独立的死手开关判定函数"
        assert "verdict()" in s, "守护没有求值 verdict(只 beat 不问结论 = 今天那个缺陷)"
        i = s.find("_check_deadman()")
        assert i > 0, "_check_deadman() 没有被主循环调用"
        # 必须写日志, 否则"没消息"与"没在查"无法区分
        blk = s[s.find("def _check_deadman"):s.find("def _check_deadman") + 2200]
        assert "_log(" in blk, "判定结果没有写进日志 —— 失效又会变静默"

    def test_paperbook_trailing_stop_present(self):
        s = _src("paper_book")
        assert "trailing_stop" in s and "_last_trailing_hits" in s

    def test_backtest_engine_trailing_stop_present(self):
        s = _src("backtest_engine")
        assert "trailing_stop" in s and "trailing_exits" in s


class TestPlanToTargetsPassthrough:
    def test_freshness_fields_are_not_dropped(self):
        """**回归锁**: `_plan_to_targets` 曾把 plan 里已有的 data_lag_days /
        section_as_of 丢掉, 使交易前清单的新鲜度项永远只能 skip。"""
        s = _src("realtime_engine")
        assert "data_lag_days" in s and "section_as_of" in s
        i = s.find("def _plan_to_targets")
        assert i > 0
        body = s[i:i + 2500]
        assert "data_lag_days" in body, "section_as_of/data_lag_days 未透传"
