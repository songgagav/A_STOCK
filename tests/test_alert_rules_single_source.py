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
        assert "TableStaleDaily" in names, "缺日报类阈值"
        assert "TableStaleQuarterly" in names, "缺季报类阈值"
        # 两条的阈值必须不同(否则等于没分档)
        exprs = {r["alert"]: str(r["expr"]) for g in d["groups"] for r in g["rules"]}
        for a in ("TableStaleDaily", "TableStaleQuarterly"):
            assert "> 5" in exprs[a] or "> 120" in exprs[a], f"{a} 阈值可疑: {exprs[a][:80]}"
        assert "> 5" in exprs["TableStaleDaily"], "日报类阈值应为 5 天"
        assert "> 120" in exprs["TableStaleQuarterly"], "季报类阈值应为 120 天"

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
