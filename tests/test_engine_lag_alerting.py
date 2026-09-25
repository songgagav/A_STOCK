# -*- coding: utf-8 -*-
"""引擎数据落后必须**可告警** (2026-09-25, 实测缺口)。

## 为什么需要这一组守卫

2026-09-25 实测: 厂商引擎最新数据停在 **2026-09-22**, 而应到 **09-24**
⇒ 落后 **2 个交易日**。系统行为**完全正确**(每个回执的
`target_plan.data_lag_days` 依次 1 -> 2, 门禁 `DEGRADED`, 选股仍在跑但用旧截面),
**但没有任何告警**, 因为两条现有规则都盖不住这段窗口:

| 规则 | 为什么盖不住 |
|---|---|
| `DataSourceHalt` | 只在 `astock_datasource_allow == 0` 时响; 而门禁的降级判据(落后 > 发布宽限 1 天)只让它到 **DEGRADED**、`allow` 仍为 1 |
| `TableStaleDaily` | 阈值 **5 个自然日**, 且实测 `daily_bars` 才 3.08 天 ⇒ 要等到第 6 天 |

于是「厂商连续几天不发布数据」——一个**正在静默降级**的过程——没有任何告警。
这组用例锁住补齐它的三件事: 快照带出 `lag_trading_days`、指标暴露它、规则按
**交易日**阈值 1 判。

## 为什么阈值与单位都要单独断言

阈值若写成自然日, 3 个自然日里可能只含 2 个交易日(周末), 同一件事会**忽早忽晚**地报;
阈值若写成 0, 则 A 股盘后 1~4 小时才发布数据的**正常延迟**也会叫 —— 噪声告警会
训练人忽略频道(本仓 `TableStale24h` 330 次 firing 的教训)。
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


class TestSnapshotCarriesLag:
    """落后天数必须进入健康快照 —— 否则指标层无从暴露。"""

    def test_gather_writes_lag_trading_days(self):
        """`gather()` 必须把 `freshness().lag_trading_days` 写进快照。

        它是本项**唯一可告警的量化值**: `freshness_ok` 是布尔的, 无法表达
        "落后 2 天"与"落后 3 天"的区别, 而门禁恰好按天数分档。
        """
        import health_state as H
        src = inspect.getsource(H.gather)
        assert "lag_trading_days" in src, (
            "gather() 没有把 lag_trading_days 写进快照 —— "
            "指标层将只能拿到布尔的 freshness_ok, 无法按天数告警")
        assert 'snap["lag_trading_days"]' in src

    def test_freshness_actually_returns_it(self):
        """`freshness()` 必须真的返回该字段(文档说了不算, 要看返回值)。

        用假 engine_day 触发一次真实计算: 传一个很早的日期, 落后必然 > 0。
        """
        import datetime as dt
        import engine_bars_sync as E
        f = E.freshness("20260101")
        assert "lag_trading_days" in f, f"freshness() 返回里没有该字段: {list(f)}"
        lag = f.get("lag_trading_days")
        assert isinstance(lag, int) and lag > 0, f"2026-01-01 的落后天数应 > 0, 实际 {lag}"


class TestMetricIsExposed:
    """指标名与"读不到时不记 0"两个语义都要锁。"""

    def test_metric_names_exist(self):
        import metrics_server as M
        names = [v._name for k, v in vars(M).items()
                 if k.startswith("_g_") and hasattr(v, "_name")]
        assert "astock_engine_lag_days" in names, names
        assert "astock_engine_lag_read_ok" in names, names

    def test_refresh_missing_field_sets_read_ok_zero_not_zero_lag(self):
        """**关键**: 快照缺该字段时必须报"不可读", **不能记 0**。

        记 0 会让"读不到"显示成"已追平" —— 那正是本仓反复修的形态:
        监控失效与指标健康必须可区分。
        """
        import metrics_server as M
        src = inspect.getsource(M._refresh_engine_lag)
        assert "_g_engine_lag_read_ok.set(0)" in src
        # 缺字段分支里必须在 return 之前就 set read_ok=0, 且**不得** set lag=0
        i_none = src.find("if lag is None")
        assert i_none > 0, "没有处理'字段缺失'的分支"
        branch = src[i_none:i_none + 400]
        assert "_g_engine_lag_read_ok.set(0)" in branch
        assert "_g_engine_lag.set(0" not in branch, (
            "缺字段分支把 lag 记成了 0 —— 那会让'读不到'显示成'已追平'")

    def test_refresh_is_wired_into_the_loop(self):
        import metrics_server as M
        src = inspect.getsource(M._refresh)
        assert "_refresh_engine_lag()" in src, "指标没有被接进刷新循环 —— 写了也不会更新"


#: 用 `find_spec` 而不是 `import yaml`: 与 `test_alert_rules_single_source.py` 同一做法 ——
#: 这样**不需要 yaml 的那几条仍然真跑**(模块级 `importorskip` 会把整个文件跳掉)。
#: 实测本项目跑测试的 `.venv314`/`.venv310` 都**没有** PyYAML, 只有生产解释器有,
#: 故这 4 条的 skip 是**常态**; 改完 `ops/alert_rules.yml` 后请用生产解释器手工核一次。
_HAS_YAML = __import__("importlib.util", fromlist=["util"]).find_spec("yaml") is not None
_needs_yaml = pytest.mark.skipif(
    not _HAS_YAML,
    reason="需要 PyYAML; 本项目 .venv314/.venv310 都没有(只有生产解释器有)")


def _load_rules():
    import yaml
    p = os.path.join(_REPO, "ops", "alert_rules.yml")
    d = yaml.safe_load(open(p, encoding="utf-8"))
    return {r["alert"]: r for g in d["groups"] for r in g["rules"]}


class TestAlertRule:
    """规则必须按**交易日**、阈值 1、且有 for: 与可读性守卫。"""

    @_needs_yaml
    def test_engine_data_lag_rule_exists_with_trading_day_threshold(self):
        rules = _load_rules()
        assert "EngineDataLag" in rules, "缺 EngineDataLag 规则"
        expr = " ".join(str(rules["EngineDataLag"]["expr"]).split())
        assert expr == "astock_engine_lag_days > 1", (
            f"判据应为「落后**交易日**数 > 1」: {expr!r}\n"
            "· > 0 会把 A 股盘后 1~4 小时的**正常发布延迟**也报成告警(噪声);\n"
            "· 若改用 `astock_db_lastday_ts` 则变成**自然日**口径, 周末会导致忽早忽晚")
        assert rules["EngineDataLag"].get("for"), (
            "缺 `for:` —— 健康快照每 ~5 分钟发布一次, 单次探针抖动不该叫")

    @_needs_yaml
    def test_lag_alert_is_warning_not_critical(self):
        """告警级别应是 warning: 此时系统**仍在正常工作**(还能选股), 只是用旧数据。

        用 critical 会与"已经停手"的 `DataSourceHalt` 混为一谈 ——
        而这两件事的处置完全不同(warning: 盯厂商; critical: 已停手, 需人工介入)。
        """
        rules = _load_rules()
        assert rules["EngineDataLag"]["labels"]["severity"] == "warning"

    @_needs_yaml
    def test_unreadable_guard_exists(self):
        """告警源自身失效必须显式暴露(与其它 Unreadable 规则同一纪律)。"""
        rules = _load_rules()
        assert "EngineDataLagUnreadable" in rules, (
            "缺 EngineDataLagUnreadable —— 旧版快照没有 lag_trading_days 字段时, "
            "EngineDataLag 会静默哑掉, 而'没有告警'与'没有在监控'必须可区分")
        expr = " ".join(str(rules["EngineDataLagUnreadable"]["expr"]).split())
        assert expr == "astock_engine_lag_read_ok == 0", expr

    @_needs_yaml
    def test_description_says_where_to_look(self):
        """description 必须说清**去哪儿查** —— 否则收到告警也不知道做什么。"""
        rules = _load_rules()
        desc = str(rules["EngineDataLag"]["annotations"]["description"])
        assert "daily_summary.json" in desc or "engine_bars_sync" in desc, desc
        assert "target_plan" in desc or "section_as_of" in desc, (
            "应指出用 target_plan 的 section_as_of / data_lag_days 确认"
            "『用哪天的数据在做决策』")


class TestKnownStructuralFalsePositiveIsLabelled:
    """`DrlEnvMissing` 必须被**标注为已知结构性误报** (用户 2026-09-25 要求)。

    ## 为什么它是结构性误报

    `drl_degrade.probe_runtime()` 探的是 **metrics_server 自己那个解释器**
    (`sys.executable`, 生产 VM 的 CPython 3.10: 有 `h5i_db`、无 `torch`),
    而 DRL 训练实际跑在 **`TRAE_PY`** 上 —— 实测 09-24 的
    `drl_train_heartbeat.json` 是 `{phase: "done", ok: true, total_timesteps: 800}`,
    **训练是成功的**。即该指标描述的是**读数那个解释器**, 不是**训练那个解释器**。

    ## 为什么选择"标注已知"而不是 silence 或改判据

    用户给出的三个选项与本仓立场:
      · **标记已知状态** ✅ —— 与 `TableStaleDaily` 对 valuation 的处理一致;
      · **静音** ❌ —— 会连"TRAE_PY 真的缺依赖"这一真故障一起盖掉,
        而且本仓原话是「用 silence 盖掉更糟: 它让你以为问题被处理了」;
      · **保持现状** ⚠️ —— 持续 firing 会训练人忽略它(本仓
        `TableStale24h` 330 次 firing 的教训: 长期无人处理的告警 = 没有告警)。

    故: **保留 warning, 把"已知"与"核对办法"写进注解**, 并给出
    「看 drl_train_heartbeat 判断是否真故障」这个可执行步骤。
    """

    @_needs_yaml
    def test_drlenvmissing_is_labelled_known_structural(self):
        rules = _load_rules()
        assert "DrlEnvMissing" in rules, "规则不见了"
        r = rules["DrlEnvMissing"]
        # 判据**不得**改动 —— 只标注, 不替用户做 P0-DRLDEP 决策
        expr = " ".join(str(r["expr"]).split())
        assert expr == "astock_drl_env_ok == 0", f"判据被改了: {expr!r}"
        # 级别保持 warning(**不静音**: 它同时覆盖 TRAE_PY 真缺依赖)
        assert r["labels"]["severity"] == "warning"
        summary = str(r["annotations"]["summary"])
        desc = str(r["annotations"]["description"])
        assert "已知结构性误报" in summary, summary
        assert "P0-DRLDEP" in desc, "未指向已登记的决策项"
        assert "drl_train_heartbeat" in desc, (
            "必须给出**可执行的核对办法**(看心跳判断是否真故障), "
            "否则「已知」就只是一句免责声明")
        assert "silence" in desc, (
            "应说明为何不用 silence 盖掉 —— 否则后人会「顺手」把它静音")
        assert "probe_runtime" in desc or "metrics_server" in desc, (
            "应说清误报的机理(探针探的是哪个解释器)")
