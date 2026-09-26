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

from doc_section import code_block_bounds  # noqa: E402  按**结构**定界, 取代 src[i:i+N] 的魔数窗口

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
        branch = src[i_none:code_block_bounds(src, i_none)]
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


class TestTwoTierLagAlertsAreCrossReferenced:
    """`EngineDataLag` 与 `TableStaleEngineFed` 必须**互相点名**并说明"不是两件事"。

    ## 为什么需要这条守卫(用户 2026-09-25 要求)

    两条规则看的是**同一个指标** `astock_engine_lag_days`, 只是阈值不同
    (`>1` 与 `>2`)。故它们**同时 firing 是同一个故障在升级**, 不是两个故障。

    但只看告警清单的人**没法知道这一点** —— 两条不同的 `alertname`、
    两条不同的 summary, 很自然会被读成"有两件事要查", 于是**分别排查两次**。
    这正是本仓 DISC-2 ⑤ 的形态: 信息在传递链里丢了(丢的是"它们是同一个指标")。

    **为什么放在两条 description 里而不是只在文档里**: 收到告警的人**先看 description**,
    不一定去翻 `docs/`。告警文本是那件事发生的**现场**。
    """

    @_needs_yaml
    def test_both_directions_name_each_other(self):
        rules = _load_rules()
        for a, other in (("EngineDataLag", "TableStaleEngineFed"),
                         ("TableStaleEngineFed", "EngineDataLag")):
            assert a in rules, f"缺规则 {a}"
            desc = str(rules[a]["annotations"]["description"])
            assert other in desc, (
                f"{a} 的 description 没有点名 {other} —— "
                "收到告警的人会以为这是两个独立故障")

    @_needs_yaml
    def test_both_say_it_is_one_problem_not_two(self):
        rules = _load_rules()
        for a in ("EngineDataLag", "TableStaleEngineFed"):
            desc = str(rules[a]["annotations"]["description"])
            assert ("两件事" in desc) or ("不是两个" in desc) or ("同一个" in desc), (
                f"{a} 未说明『不是两件事 / 是同一个故障』")
            assert ("重复" in desc) or ("分别排查" in desc) or ("升级" in desc), (
                f"{a} 未说明『不要分别排查 / 这是升级过程』")

    @_needs_yaml
    def test_the_two_thresholds_are_kept_distinct(self):
        """两级阈值不得被"顺手"改成相同 —— 相同就等于重复报同一件事。"""
        rules = _load_rules()
        e1 = " ".join(str(rules["EngineDataLag"]["expr"]).split())
        e2 = " ".join(str(rules["TableStaleEngineFed"]["expr"]).split())
        assert e1 == "astock_engine_lag_days > 1", e1
        assert e2 == "astock_engine_lag_days > 2", e2
        assert e1 != e2, "两条判据变得完全相同 —— 会重复报同一件事"


class TestDrlEnvMissingIsATrueAlertNotAFalsePositive:
    """`DrlEnvMissing` **不是**误报 —— 它曾于 2026-09-25 被错标, 09-26 更正。

    本类替换原先的 `TestKnownStructuralFalsePositiveIsLabelled`。**改了断言方向**:
    从"必须标注为已知误报"改成"不得再被标成误报, 且更正理由必须留痕"。

    ## 我错在哪 (这条比规则本身值钱, 故原样写进 docstring)

    09-25 我的理由是"探针探错了环境": 我以为 `probe_runtime()`(跑在 metrics_server 里)
    探的解释器与 DRL 训练用的解释器是**两个**。**这个前提是错的**:

      · `daemon.py:35` `PY = sys.executable`; 第 38-40 行若 `TRAE_PYTHON` 存在则覆盖;
      · metrics_server 与 run_daily **都由这个同一个 `PY` 启动**;
      · 生产守护由 `scripts/start_daemon.ps1` 用 **`.venv310`** 启动, 而 `P0-DRLDEP`
        已在 **2026-09-20 修复** ⇒ h5i_db 与 torch **同处一个解释器**。

    ⇒ 探针探的解释器**就是**训练用的解释器, 没有"探错"这回事。
    实测 `astock_drl_env_ok=1`、`drl_degrade.probe.missing=[]`、`drl_train.ok=true`。

    ## 为什么必须更正而不是"顺手留着"

    一条标着"已知误报"的**真**告警, 比没有告警更坏: 将来 `TRAE_PY` 真缺依赖时,
    值班人会**照着那段注释把它忽略掉**。这是 ⑥ 号形态的反向 ——
    不是"降级无告警", 而是"**告警在, 但被文字解释掉了**"。

    ## 为什么这个错误很难自查(值得写成用例)

    当时的"证据"是**真的**: 09-24 的 `drl_train_heartbeat` 确实是
    `{phase: done, ok: true}`。但那条证据**同时兼容两种解释**:
      (a) 探针探错了解释器(我选的) —— 与心跳并存, 看似被心跳"证实";
      (b) 探针在对的解释器上、环境本来就齐备 —— 与心跳并存, 也完全一致。
    我拿一条**两种假设都能解释**的证据, 当成了对其中一种的确认。
    ⇒ **判据**: 一条证据若能同时支持两个互斥假设, 它就不构成对任一假设的确认;
      必须去找**能区分**两者的那个观测(本例: 查 `PY` 的来源, 而不是看训练成不成功)。
    """

    @_needs_yaml
    def test_rule_is_unchanged_and_still_a_real_warning(self):
        """判据与级别**不得**被这次更正改动 —— 错的只是文字, 不是逻辑。"""
        rules = _load_rules()
        assert "DrlEnvMissing" in rules, "规则不见了"
        r = rules["DrlEnvMissing"]
        expr = " ".join(str(r["expr"]).split())
        assert expr == "astock_drl_env_ok == 0", f"判据被改了: {expr!r}"
        assert r["labels"]["severity"] == "warning", (
            "必须保持 warning —— 它是真告警, 不是需要降级的误报")

    @_needs_yaml
    def test_no_longer_labelled_a_false_positive(self):
        """**关键断言**: summary 里不得再出现"已知结构性误报"这类免责话术。"""
        rules = _load_rules()
        summary = str(rules["DrlEnvMissing"]["annotations"]["summary"])
        assert "结构性误报" not in summary, (
            "summary 仍标着误报 —— 值班人会照着它忽略真故障: " + summary)
        assert "已知" not in summary or "已知状态" not in summary, (
            "summary 不得再用「已知状态」把告警解释掉: " + summary)

    @_needs_yaml
    def test_correction_is_documented_with_the_wrong_premise(self):
        """更正必须写明**当初错在哪个前提**, 否则后人会重新推出同一个结论。"""
        rules = _load_rules()
        desc = str(rules["DrlEnvMissing"]["annotations"]["description"])
        assert "TRAE_PYTHON" in desc, "必须点名 TRAE_PYTHON 这个真正的解释器来源"
        assert "start_daemon.ps1" in desc or ".venv310" in desc, (
            "必须给出「守护实际用哪个解释器」的可查证依据")

    @_needs_yaml
    def test_gives_an_actionable_check_not_a_disclaimer(self):
        """必须给可执行核对步骤, 而不是一句「可能是误报」。"""
        rules = _load_rules()
        desc = str(rules["DrlEnvMissing"]["annotations"]["description"])
        assert "probe_runtime" in desc, "应说清探针是什么"
        assert "drl_train_heartbeat" in desc, (
            "必须保留交叉核对办法(看心跳判断读数与训练是否同源)")
        assert "不要" in desc, "应显式提醒**不要**因历史上的误标而忽略它"

    @_needs_yaml
    def test_the_interpreter_single_source_is_the_mechanism(self):
        """必须写明机制: **单一 `PY` 来源** ⇒ 所有子进程天然同源。

        这是本条更正的核心事实。若只写"我查过了, 是对的", 后人无法复核。
        """
        rules = _load_rules()
        desc = str(rules["DrlEnvMissing"]["annotations"]["description"])
        assert "共用" in desc or "同一个解释器" in desc, (
            "必须点明 metrics_server 与训练共用解释器这一机制")

