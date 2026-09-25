# -*- coding: utf-8 -*-
"""告警规则的**单一事实源**守卫 (2026-09-22 批次)。

## 为什么要立这条

2026-09-22 接线死手开关告警时发现: 我把 `DeadmanOverdue` 等 3 条规则写进了
`A_stock_rotation/ops/alert_rules.yml`(13 条规则), 但查 Prometheus 的规则 API
只看到 **1 条**。追下去发现:

- Prometheus 实际读的是 `obs-stack/alert_rules.yml` —— 一份 **443 字节、
  只有 1 条规则、最后修改于 2026-09-14** 的**陈旧副本**;
- 因为 `obs-stack/prometheus.yml` 写的是相对路径 `- "alert_rules.yml"`,
  按 Prometheus 的 cwd(`obs-stack/`)解析, 于是永远读那份旧副本;
- 仓内真正的规则文件 **从未被读过**。

**后果**: 8 天里所有告警规则的改动(融合健康 3 条 / DRL 降级 9 条 /
数据源门禁 2 条 / 死手开关 3 条)**全部是空的** —— 文件写对了、YAML 校验过了、
登记册也记了, 但 Prometheus 里根本不存在这些规则, 因此**永远不会告警**。

这是本仓反复出现的『看起来做了 vs 实际生效』, 而且这次踩在**告警链自己身上**:
**不会响的告警, 与没有告警, 外表完全一样。**

## 不变量

1. 仓内 `ops/` 下告警规则只允许**一份**(陈旧副本是本次事故的直接成因);
2. 若本机存在 `obs-stack/prometheus.yml`, 它的 `rule_files` 必须指向**仓内那份的
   绝对路径**, 不允许是相对路径(相对路径会随启动者的 cwd 漂移, 而**漂移的告警
   配置没有任何症状**)。
"""
from __future__ import annotations

import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库**外层**目录(obs-stack 与 A_stock_rotation 平级)
_OUTER = os.path.dirname(_REPO)
_PROM_YML = os.path.join(_OUTER, "obs-stack", "prometheus.yml")

#: 用 `find_spec` 而不是 `import yaml`: 这样**不需要 yaml 的那条用例仍会真跑**。
#: 模块级 `pytest.importorskip("yaml")` 会把整个文件跳掉(实测 .venv314 与 .venv310
#: 都变成 "1 skipped"), 于是"仓内只允许一份规则文件"这条**最关键的守卫**
#: 在没有 yaml 的解释器上等于不存在 —— 而它恰恰是最需要常年生效的一条。
#:
#: 实测: 本项目跑测试的 `.venv314`/`.venv310` **都没有 PyYAML**, 只有生产解释器
#: (`%APPDATA%\TRAE SOLO CN\...\vm\tools\python\python.exe`)有。所以下面 4 条的
#: skip 是**常态而非异常**。它们不是没价值 —— 改完 `ops/alert_rules.yml` 或
#: `prometheus.yml` 后, 请用生产解释器手工核一次(2026-09-22 已核: 13 条规则 / 3 组,
#: 且 rule_files 指向仓内那份)。若要让它常年自动生效, 给 .venv314 装上 PyYAML 即可。
_HAS_YAML = __import__("importlib.util", fromlist=["util"]).find_spec("yaml") is not None
_needs_yaml = pytest.mark.skipif(
    not _HAS_YAML,
    reason="需要 PyYAML; 本项目 .venv314/.venv310 都没有(只有生产解释器有) —— "
           "改 alert_rules.yml 后请用生产解释器手工核一次")


def _load_rules() -> dict:
    """把 `ops/alert_rules.yml` 读成 `{alert 名: 规则 dict}`。

    ## 这个函数曾经**不存在**, 而三个用例在调它 (2026-09-26 修复)

    三个用例(`test_engine_fed_tables_use_trading_day_threshold` /
    `test_every_daily_table_is_in_exactly_one_staleness_rule` /
    `test_filter_logic_accepts_per_table_thresholds`)都写了 `rules = _load_rules()`,
    但**本文件从未定义过它** —— 于是它们一跑就 `NameError`。

    **它们从来没跑过**: 三者都挂 `@_needs_yaml`, 而 `.venv314`/`.venv310` 都没有 PyYAML,
    于是**恒为 skip**, 而 skip 在报告里长得像通过(DISC-2 ② 形态) ——
    里面藏着一个 ① 形态的缺陷(断言引用了不存在的东西)。

    **发现方式**: 2026-09-26 把生产解释器的 `yaml` 包桥接进 `.venv310` 后跑全量,
    这三条立刻变红。**这不是回归, 是它们第一次被执行。**

    故本函数必须**存在且被注释保住** —— 若将来有人"清理未使用的函数"而删掉它,
    那三个用例会重新退化成 NameError(skip 状态下不可见)。
    返回整个规则 dict(而不只是 expr), 因为调用方要用 `.get("for")` 与
    `["annotations"]["description"]`。
    """
    import yaml
    fp = os.path.join(_REPO, "ops", "alert_rules.yml")
    d = yaml.safe_load(open(fp, encoding="utf-8"))
    out = {}
    for g in d["groups"]:
        for r in g["rules"]:
            name = r["alert"]
            # 重名会让"恰好一条覆盖"这类断言失去意义(后者静默覆盖前者)
            assert name not in out, f"规则名重复: {name} —— 后一条会覆盖前一条"
            out[name] = r
    assert len(out) >= 13, (
        f"只读到 {len(out)} 条规则, 少于此前的基线 —— 可能改坏了 YAML 结构")
    return out


class TestSingleSourceOfTruthForAlertRules:
    def test_repo_has_exactly_one_alert_rules_file(self):
        """仓内只允许一份告警规则文件。**这条不需要 yaml, 永远真跑**。

        陈旧副本是本次事故的直接成因: 两份文件都存在、都合法、都不报错,
        但只有一份会被读到 —— 而**没有任何机制会告诉你读的是哪份**。
        """
        found = []
        for dp, dns, fns in os.walk(_REPO):
            dns[:] = [d for d in dns if d not in ("__pycache__", ".git", "_merge_workspace")]
            for fn in fns:
                if fn == "alert_rules.yml":
                    found.append(os.path.relpath(os.path.join(dp, fn), _REPO))
        assert len(found) == 1, (
            f"仓内存在 {len(found)} 份 alert_rules.yml: {found} —— "
            "必须只留一份(ops/), 否则会重演 2026-09-22『改的那份从来没被读过』")

    @_needs_yaml
    def test_the_canonical_file_parses_and_has_all_groups(self):
        """仓内那份必须是合法 YAML, 且包含已知的规则组。

        锁组名而不锁条数: 条数每次加规则都会变, 那是噪声; 组名代表**能力域**,
        少一个组就意味着某个域的告警整块消失。
        """
        import yaml
        fp = os.path.join(_REPO, "ops", "alert_rules.yml")
        assert os.path.isfile(fp), fp
        d = yaml.safe_load(open(fp, encoding="utf-8"))
        groups = {g.get("name") for g in d["groups"]}
        assert {"astock_db_stale", "astock_fusion_health", "astock_drl_degrade"} <= groups, groups
        for g in d["groups"]:
            for r in g["rules"]:
                assert r.get("alert"), f"{g['name']} 里有规则缺 alert 名"
                assert r.get("expr"), f"{r.get('alert')} 缺 expr"
                assert (r.get("labels") or {}).get("severity"), f"{r.get('alert')} 缺 severity"

    @_needs_yaml
    def test_no_single_threshold_for_all_tables(self):
        """**告警阈值必须按更新节奏分档**, 不能一刀切。

        2026-09-22 实测: 原规则 `TableStale24h` 用**单一 24h 阈值**打所有表, 而
        `financials` 是**季度**表(最后一日 2026-06-30, 84.9 天前) —— 它**必然永久超阈**,
        于是该规则自 09-08 起 **firing 330 次**, 长期无人处理。
        **长期无人处理的告警等于没有告警**: 它训练人忽略这个频道 ——
        与 obs_stack 那条常亮 OVERDUE 同源。

        真实节奏: 日报类(每交易日) vs 季报类(约 90 天)。故必须分开。
        """
        import yaml
        fp = os.path.join(_REPO, "ops", "alert_rules.yml")
        d = yaml.safe_load(open(fp, encoding="utf-8"))
        names = {r["alert"] for g in d["groups"] for r in g["rules"]}
        assert "TableStale24h" not in names, \
            "TableStale24h 回来了 —— 用 24h 判季度表必然永久误报"
        assert "TableStaleDaily" in names, "缺数据商节奏类阈值"
        assert "TableStaleQuarterly" in names, "缺季报类阈值"
        # 两条的阈值必须不同(否则等于没分档)
        exprs = {r["alert"]: str(r["expr"]) for g in d["groups"] for r in g["rules"]}
        for a in ("TableStaleDaily", "TableStaleQuarterly"):
            assert "> 5" in exprs[a] or "> 120" in exprs[a], f"{a} 阈值可疑: {exprs[a][:80]}"
        assert "> 5" in exprs["TableStaleDaily"], "数据商节奏类阈值应为 5 天"
        assert "> 120" in exprs["TableStaleQuarterly"], "季报类阈值应为 120 天"

    @_needs_yaml
    def test_engine_fed_tables_use_trading_day_threshold(self):
        """[2026-09-25 用户清单第 6、7 项] 引擎喂数的表必须用**交易日**阈值。

        ## 起因(实测, 不是推演)

        `daily_bars` 原先只由 `TableStaleDaily`(5 个**自然日**)覆盖 ——
        而厂商引擎停更时它才 **3.08 自然日**, 要等到第 6 天才响;
        可 **落后 2 个交易日**就已经是"厂商漏发"了。5 个自然日 ≈ 3~4 个交易日,
        这段窗口里选股一直用旧截面(见 docs/stockdb-source-status.md §6.10)。

        ## 判据不是"把阈值调小", 而是**按更新机制归类**

        这决定了"谁负责更新":
          · **引擎/专属 sync 写入**(daily_bars / northbound_money / margin_daily):
            有**自己的摄入路径**, 每个交易日都该推进 ⇒ **交易日**口径;
          · **上游数据商节奏**(valuation 等): 保留自然日宽松口径, 别逼成噪声。

        **为何交易日口径重要**: 3 个自然日里可能只含 2 个交易日(周末),
        按自然日算会让同一件事**忽早忽晚**地报。

        **为何本组阈值是 >2 而不是 >1**: `EngineDataLag` 已在 >1 时专门报,
        此处作**第二级兜底**; 若也用 >1, 同一件事**报两遍** ——
        而重复告警与噪声告警一样会训练人忽略频道。
        """
        rules = _load_rules()
        assert "TableStaleEngineFed" in rules, "缺引擎喂数类的交易日阈值规则"
        expr = " ".join(str(rules["TableStaleEngineFed"]["expr"]).split())
        assert expr == "astock_engine_lag_days > 2", (
            f"应用**交易日**口径的 `astock_engine_lag_days > 2`: {expr!r}\n"
            "· 若改用 `astock_db_lastday_ts / 86400` 则退回自然日口径, 周末会忽早忽晚;\n"
            "· 若改成 >1 则与 EngineDataLag 重复报同一件事")
        assert rules["TableStaleEngineFed"].get("for"), "缺 for:"
        assert rules["TableStaleEngineFed"]["labels"]["severity"] == "warning"
        desc = str(rules["TableStaleEngineFed"]["annotations"]["description"])
        assert "EngineDataLag" in desc, "应说明与 EngineDataLag 的分工(避免重复报)"
        assert "回填" in desc or "baostock" in desc.lower(), (
            "应提醒『回填不改选股口径』—— 否则收到告警的人会以为回填能解决它")

    @_needs_yaml
    def test_every_daily_table_is_in_exactly_one_staleness_rule(self):
        """每张日报表必须**恰好**被一条 staleness 规则覆盖 —— 不重不漏。

        **为什么单锁这条**: 2026-09-25 把日报类拆成"引擎喂数 / 数据商节奏"两组时,
        最容易犯的错是**两边都留** `daily_bars`(于是同一件事报两遍),
        或者**两边都删**(于是它不再被任何规则覆盖)。这两种错**都不会报错**,
        只会静默地多报或少报 —— 正是本仓 DISC-2 的形态。

        本用例把"分组"这件事变成一个可机械核对的账:
        `TableStaleDaily` 与 `TableStaleEngineFed` 的 filter/语义必须**互斥且完整**。
        """
        rules = _load_rules()
        d = rules["TableStaleDaily"]
        expr = " ".join(str(d["expr"]).split())
        # 数据商节奏组: 只含那三张已知滞后的表 —— 不能含引擎喂数的表
        for t in ("valuation", "valuation_snapshot", "money_flow_estimate"):
            assert t in expr, f"数据商节奏组应覆盖 {t}: {expr[:120]}"
        for t in ("daily_bars", "northbound_money", "margin_daily"):
            assert t not in expr, (
                f"{t} 是**引擎喂数**的表, 不该留在数据商节奏组(应由 "
                f"TableStaleEngineFed 的交易日口径覆盖): {expr[:120]}")
        # 引擎喂数组用**同一条指标**判所有表, 故它不按表名分组 ——
        # 这是刻意的: 它们的更新节奏**相同**(都由引擎决定), 无需再分档。
        fed = " ".join(str(rules["TableStaleEngineFed"]["expr"]).split())
        assert "astock_engine_lag_days" in fed, fed

    @_needs_yaml
    def test_filter_logic_accepts_per_table_thresholds(self):
        """`test_no_single_threshold_for_all_tables` 的判据不能被"顺手"放宽。

        它的本意是**禁止一刀切**, 而不是"只允许两条规则"。2026-09-25 拆成三条
        (数据商 / 引擎喂数 / 季报)之后, 那条守卫仍须通过 ——
        即: **分档可以更多, 但不能退回单一阈值**。

        [2026-09-25 自查] 本用例第一版**漏了 `@_needs_yaml`** —— 它调用
        `_load_rules()`, 而那个函数顶层 `import yaml`。于是在 `.venv314`(无 yaml)
        下它**不是 skip 而是 ERROR**, 把全量测试从"0 failed"变成"1 failed"。
        这正是本仓 DISC-2 的形态: 新加的守卫自己成了破坏源。
        **凡是用到 yaml 的用例, 必须挂 `@_needs_yaml`。**
        """
        rules = _load_rules()
        stale = [n for n in rules if n.startswith("TableStale")]
        assert len(stale) >= 3, (
            f"staleness 规则应至少三档(数据商节奏/引擎喂数/季报), 实际 {stale}")
        # 三档的判据必须**不是**同一个表达式(否则等于没分档)
        exprs = {" ".join(str(rules[n]["expr"]).split()) for n in stale}
        assert len(exprs) == len(stale), (
            f"存在判据完全相同的 staleness 规则 —— 等于没分档: {sorted(exprs)}")

    @_needs_yaml
    def test_every_rule_has_severity_and_description(self):
        """告警必须可行动: 有 severity, 且 description 说得清**去哪儿查**。"""
        import yaml
        fp = os.path.join(_REPO, "ops", "alert_rules.yml")
        d = yaml.safe_load(open(fp, encoding="utf-8"))
        for g in d["groups"]:
            for r in g["rules"]:
                name = r["alert"]
                assert (r.get("labels") or {}).get("severity"), f"{name} 缺 severity"
                ann = r.get("annotations") or {}
                assert ann.get("summary"), f"{name} 缺 summary"
                assert ann.get("description"), f"{name} 缺 description"

    @_needs_yaml
    def test_deadman_and_datasource_rules_exist(self):
        """2026-09-22 这批规则的**存在性**必须锁住。

        它们此前写进了文件却没进 Prometheus —— 所以这里断言的是"它们在文件里",
        而下面那条断言"Prometheus 读的就是这个文件"。
        """
        import yaml
        fp = os.path.join(_REPO, "ops", "alert_rules.yml")
        d = yaml.safe_load(open(fp, encoding="utf-8"))
        names = {r["alert"] for g in d["groups"] for r in g["rules"]}
        for want in ("DeadmanOverdue", "DeadmanNeverArmed", "DeadmanUnreadable",
                     "DataSourceHalt", "DataSourceGateUnreadable"):
            assert want in names, f"缺规则 {want}; 现有: {sorted(names)}"

    @_needs_yaml
    def test_scrape_target_is_itself_monitored(self):
        """**告警链自己被抓取这件事, 必须也有告警。**

        2026-09-23 00:31 实测到这条缺口(不是推演): 手工停掉 metrics_server 换代码时,
        9101 停止被抓, `astock_db_lastday_ts` 等序列消失, 于是 00:32:51 alert_hook
        记下三条 **`resolved`** —— 而那三张表(valuation / valuation_snapshot /
        money_flow_estimate)一张都没更新。

        为什么这比"某条规则写错"危险: 数据源死掉以后本系统发出的信号是**由红转绿**。
        Alertmanager 把"firing -> 序列消失"解释为 resolved, 于是人收到的唯一一条
        消息是"故障已恢复" —— 这正是本仓明令禁止的**假恢复**
        (『宁可响亮停手, 不可静默降级』)。

        判据用 `up` 而不是某个 `astock_*` 指标: `up` 由 Prometheus 自己生成,
        **不经过 metrics_server** —— 否则就是"要求被监控者还活着才能报它死了"。
        """
        import yaml
        fp = os.path.join(_REPO, "ops", "alert_rules.yml")
        d = yaml.safe_load(open(fp, encoding="utf-8"))
        rules = {r["alert"]: str(r["expr"]) for g in d["groups"] for r in g["rules"]}
        assert "MetricsTargetDown" in rules, (
            "缺 MetricsTargetDown —— 9101 死掉时, 本文件其余规则会**全部静默失效**, "
            "且已 firing 的告警会被报成 resolved(假恢复)")
        expr = " ".join(rules["MetricsTargetDown"].split())
        assert expr.startswith("up{"), (
            f"MetricsTargetDown 的判据必须是 `up{{...}}`(Prometheus 自产, 不依赖 9101), "
            f"实际: {expr[:100]!r}")
        assert "== 0" in expr, f"判据应判 up == 0, 实际: {expr[:100]!r}"
        # 必须有 for: 否则主动重启(正常运维)也会叫 —— 与 FAILS_TO_HALT 同一条纪律
        for g in d["groups"]:
            for r in g["rules"]:
                if r["alert"] == "MetricsTargetDown":
                    assert r.get("for"), (
                        "MetricsTargetDown 缺 `for:` —— 主动重启 metrics_server 是正常运维, "
                        "无延迟会把它变成噪声告警(长期噪声等于没有告警)")

    def test_no_alert_rules_copy_outside_the_repo(self):
        """**仓外**也不允许留告警规则副本。不需要 yaml, 永远真跑。

        为什么单列一条(上面那条走 `os.walk(_REPO)` 覆盖不到这里):
        2026-09-22 的事故副本恰恰在**仓外**的 `obs-stack/alert_rules.yml`
        (443 字节 / 1 条规则 / 最后改于 09-14), 于是"仓内只允许一份"那条守卫
        **全程没管到它**。那份副本已于 2026-09-23 删除, 这条守卫防止它再长回来 ——
        `obs-stack/` 正是 Prometheus 的 cwd, 规则文件放那里**一定会被读到**。
        """
        if not os.path.isdir(_OUTER):
            pytest.skip("外层目录不存在")
        strays = []
        for dp, dns, fns in os.walk(_OUTER):
            dns[:] = [d for d in dns if d not in ("__pycache__", ".git", "node_modules")]
            # 仓内那份是**唯一合法**的; 其它 git checkout 副本(_merge_workspace)不算陷阱,
            # 因为它们不在 Prometheus 的搜索路径上 —— 但 obs-stack 下必须是空的。
            if os.path.abspath(dp).lower().startswith(os.path.abspath(_REPO).lower()):
                continue
            for fn in fns:
                if fn != "alert_rules.yml":
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, _OUTER).replace("\\", "/")
                if rel.startswith("_merge_workspace/"):
                    continue
                strays.append(rel)
        assert not strays, (
            f"仓外存在告警规则副本: {strays} —— obs-stack/ 是 Prometheus 的 cwd, "
            "放那里的规则文件会被读到, 而**没有任何机制会告诉你读的是哪份**")


class TestPrometheusActuallyReadsIt:
    """**核心**: Prometheus 必须指向仓内那一份, 而不是任何副本。"""

    @_needs_yaml
    def test_prometheus_points_at_the_repo_file_by_absolute_path(self):
        if not os.path.isfile(_PROM_YML):
            pytest.skip(f"本机没有 {_PROM_YML} —— 观测栈未部署在同一层目录")
        import yaml
        d = yaml.safe_load(open(_PROM_YML, encoding="utf-8"))
        files = d.get("rule_files") or []
        assert files, "prometheus.yml 没有任何 rule_files => 一条告警都不会加载"
        canonical = os.path.join(_REPO, "ops", "alert_rules.yml").replace("\\", "/").lower()
        hits = [f for f in files
                if str(f).replace("\\", "/").lower() == canonical]
        assert hits, (
            f"prometheus.yml 的 rule_files={files} 没有指向仓内的单一事实源 "
            f"{canonical} —— 2026-09-22 的事故就是它指向了 obs-stack/ 下的陈旧副本, "
            "导致 8 天里所有规则改动全是空的")

    @_needs_yaml
    def test_no_relative_rule_paths(self):
        """相对路径必须禁止: 它随启动者的 cwd 漂移, 而漂移没有症状。"""
        if not os.path.isfile(_PROM_YML):
            pytest.skip("观测栈未部署")
        import yaml
        d = yaml.safe_load(open(_PROM_YML, encoding="utf-8"))
        for f in d.get("rule_files") or []:
            assert os.path.isabs(str(f)), (
                f"rule_files 里出现相对路径 {f!r} —— 它会按 Prometheus 的 cwd 解析, "
                "而 daemon 是用 cwd=obs-stack 拉起的, 人手工跑可能在别处")
