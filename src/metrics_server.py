# -*- coding: utf-8 -*-
"""metrics_server.py -- Prometheus /metrics 端点 (表健康 + 更新状态).

端口: 9101 (Prometheus scrape target)
指标:
  astock_db_rows{table}              表/文件行数
  astock_db_lastday_ts{table}         最后更新日的 epoch 秒 (供 24h 滞后告警)
  astock_db_na_pct{table,column}      关键列空值率 %
  astock_db_dup_pct{table}            (ts,symbol) 重复对占比 %
  astock_update_running               Celery/manual 全量更新是否在跑 (0/1)
采集周期: 60s, 每次同步追加 db_stats_history.jsonl (趋势历史).

用法: python src/metrics_server.py [--port 9101]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

import db_stats  # noqa: E402
import tasks_db  # noqa: E402

from prometheus_client import Gauge, start_http_server  # noqa: E402

_g_rows = Gauge("astock_db_rows", "table/file row count", ["table"])
_g_last = Gauge("astock_db_lastday_ts", "last updated day epoch seconds",
                ["table"])
_g_na = Gauge("astock_db_na_pct", "null ratio percent of key column",
              ["table", "column"])
_g_dup = Gauge("astock_db_dup_pct", "dup (ts,symbol) pair percent",
               ["table"])
_g_run = Gauge("astock_update_running", "full db update running flag")

# ---- 策略绩效指标 (来源: state.json + dashboard.read_riskops + performance_report) ----
_g_eq = Gauge("astock_strategy_equity", "paper account equity")
_g_cash = Gauge("astock_strategy_cash", "paper account cash")
_g_pos = Gauge("astock_strategy_positions", "open position count")
_g_pnl = Gauge("astock_strategy_daily_pnl_pct", "daily pnl percent")
_g_sharpe = Gauge("astock_strategy_sharpe", "rolling sharpe (60d)")
_g_sortino = Gauge("astock_strategy_sortino", "sortino ratio")
_g_maxdd = Gauge("astock_strategy_max_dd", "max drawdown pct (negative)")
_g_var95 = Gauge("astock_strategy_var95", "daily var95 pct")
_g_win = Gauge("astock_strategy_win_rate", "win rate pct")
_g_totret = Gauge("astock_strategy_total_return", "total return pct since init")

# ---- 融合口径健康度 (来源: data/fusion_health.json, 由 factor_fusion.fusion_or_fml 落盘) ----
# why: 2026-09-19 前 fusion 覆盖跌破门槛时静默退化到 used=equal(无融合加成), 无任何
# 可观测信号; 这三个指标 + ops/alert_rules.yml 的 FusionDegraded 规则负责把它变成告警。
_g_fusion_used = Gauge("astock_fusion_used", "fusion_or_fml 当前口径 (1=生效)",
                       ["kind"])
_g_fusion_degraded = Gauge("astock_fusion_degraded", "融合降级标志 (1=已降级)")
_g_fusion_cov = Gauge("astock_fusion_coverage", "最近一次融合覆盖率 (0~1)")
_g_fusion_checked = Gauge("astock_fusion_checked_ts",
                          "最近一次融合口径检查 epoch 秒")
# [2026-09-19 上线前复验发现] 健康度文件**读失败**必须显式暴露:
#   原实现读失败只 print 一行, 而 gauge 保留上一次的值 -> 若上次是"健康", 告警永远不会触发
#   (实测: PowerShell 的 Set-Content -Encoding UTF8 会写 BOM, 触发
#   "Unexpected UTF-8 BOM" -> 指标永久停在 degraded=0)。1=读成功, 0=读失败。
_g_fusion_read_ok = Gauge("astock_fusion_health_read_ok",
                          "融合健康度文件读取成功标志 (0=读失败, 指标可能已过期)")
FUSION_HEALTH_FP = os.path.join(_BASE, "data", "fusion_health.json")
FUSION_KINDS = ("fusion", "fml_fallback", "equal")

# ---- DRL 降级状态 (来源: data/drl/current_model.json 指针 + data/drl_degrade_events.jsonl) ----
# why (用户决策 D, 2026-09-20): DRL 训练暂不在生产运行(无解释器同时具备 h5i_db 与 torch),
# 要求"响亮地置 L3、每日复盘告警可见"。这四个指标 + ops/alert_rules.yml 的
# DrlHalted 规则负责把"DRL 已暂停交易、当日未生成新信号"变成可告警的事实。
_g_drl_level = Gauge("astock_drl_degrade_level",
                     "DRL 降级级别 (0=正常 1=保留旧模型 2=回退上一版 3=暂停交易)")
_g_drl_blocked = Gauge("astock_drl_plan_blocked",
                       "DRL 当日 target_plan 是否被阻断 (1=阻断, 未生成新信号)")
_g_drl_events = Gauge("astock_drl_degrade_events",
                      "DRL 降级账本累计事件数 (含恢复事件)")
_g_drl_env_ok = Gauge("astock_drl_env_ok",
                      "DRL 运行环境齐备标志 (0=缺依赖 => 显式 L3)")
_g_drl_read_ok = Gauge("astock_drl_degrade_read_ok",
                       "DRL 降级状态读取成功标志 (0=读失败, 指标可能已过期)")


def _to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _refresh_strategy() -> None:
    try:
        import json as _json
        import os as _os
        # state.json 实时持仓/权益
        sp = _os.path.join(_BASE, "data", "state.json")
        eq = ca = ps = None
        if _os.path.exists(sp):
            st = _json.load(open(sp, encoding="utf-8"))
            eq = _to_f(st.get("equity"))
            ca = _to_f(st.get("cash"))
            ps = sum(1 for v in (st.get("positions") or {}).values()
                     if v.get("qty", 0) > 0)
        _g_eq.set(eq if eq is not None else -1)
        _g_cash.set(ca if ca is not None else -1)
        _g_pos.set(ps if ps is not None else -1)
        # 绩效序列指标 (read_riskops)
        import dashboard as _dash
        rk = _dash.read_riskops()
        m = rk.get("metrics") or {}
        _g_pnl.set(_to_f(m.get("daily_pnl_pct")) if m.get("daily_pnl_pct") is not None else -1)
        _g_sharpe.set(_to_f(m.get("sharpe_w60")) if m.get("sharpe_w60") is not None else -1)
        _g_sortino.set(_to_f(m.get("sortino")) if m.get("sortino") is not None else -1)
        _g_maxdd.set(_to_f(m.get("max_dd")) if m.get("max_dd") is not None else -1)
        _g_var95.set(_to_f(m.get("var95")) if m.get("var95") is not None else -1)
        _g_win.set(_to_f(m.get("win_rate")) if m.get("win_rate") is not None else -1)
        # performance_report total_return
        pp = _os.path.join(_BASE, "data", "performance_report.json")
        tr = None
        if _os.path.exists(pp):
            pr = _json.load(open(pp, encoding="utf-8"))
            tr = _to_f((pr.get("metrics") or {}).get("total_return"))
        _g_totret.set(tr if tr is not None else -1)
    except Exception as e:  # noqa: BLE001
        print("strategy metrics err:", str(e)[:160], flush=True)


def _epoch(val) -> float | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val)).timestamp()
    except Exception:
        try:
            return datetime.strptime(str(val), "%Y-%m-%d").timestamp()
        except Exception:
            return None


def _refresh_fusion() -> None:
    """读 factor_fusion 落盘的融合口径健康度 -> Prometheus 指标.

    文件不存在时**不设值**(指标缺失比报一个假的 0 更安全, 后者会让告警静默)。
    """
    if not os.path.exists(FUSION_HEALTH_FP):
        return
    try:
        # utf-8-sig: 兼容带 BOM 的文件(Windows 工具如 PowerShell Set-Content -Encoding UTF8
        # 会写 BOM)。用纯 utf-8 会抛 "Unexpected UTF-8 BOM" -> 指标永久失效。
        with open(FUSION_HEALTH_FP, encoding="utf-8-sig") as f:
            h = json.load(f)
    except Exception as e:  # noqa: BLE001
        _g_fusion_read_ok.set(0)      # 显式暴露"读失败", 不留下健康假象
        print("fusion health read err:", str(e)[:160], flush=True)
        return
    _g_fusion_read_ok.set(1)
    used = str(h.get("used") or "")
    for k in FUSION_KINDS:
        _g_fusion_used.labels(k).set(1 if used == k else 0)
    _g_fusion_degraded.set(1 if h.get("degraded") else 0)
    cov = h.get("coverage")
    _g_fusion_cov.set(float(cov) if isinstance(cov, (int, float)) else -1.0)
    ep = _epoch(h.get("checked_at"))
    _g_fusion_checked.set(ep if ep is not None else -1.0)


def _refresh_drl_degrade() -> None:
    """读 DRL 降级状态 -> Prometheus 指标（用户决策 D: L3 必须**可告警**）。

    数据源是 `drl_degrade` 的**轻量**读取器（指针 `current_model.json` + 账本），
    不 import torch / h5i_db，故在**任何**解释器下都能取值。

    与 `_refresh_fusion` 同样的原则: 拿不到状态时**不设值**（指标缺失优于假的 0，
    后者会让"L3 暂停"看起来像"一切正常"）。
    """
    try:
        import drl_degrade as _dd
    except Exception as e:  # noqa: BLE001
        print("drl_degrade import err:", str(e)[:160], flush=True)
        return
    try:
        cur = _dd.current_level()
    except Exception as e:  # noqa: BLE001
        _g_drl_read_ok.set(0)          # 显式暴露"读失败", 不留下健康假象
        print("drl degrade read err:", str(e)[:160], flush=True)
        return
    _g_drl_read_ok.set(1)
    lv = int(cur.get("level") or 0)
    _g_drl_level.set(lv)
    _g_drl_blocked.set(1 if cur.get("blocked_plan") else 0)
    # 是否**曾经**降级过(L1+): 与"当前级别"分开, 便于区分"恢复过"与"从未降级"
    try:
        _g_drl_events.set(_dd.event_count())
    except Exception:  # noqa: BLE001
        pass
    # 运行环境齐备性(决策 D 的根因): 0 = 缺依赖 => L3 显式暂停
    try:
        _g_drl_env_ok.set(1 if _dd.probe_runtime().get("ok") else 0)
    except Exception:  # noqa: BLE001
        _g_drl_env_ok.set(0)


_g_ds_allow = Gauge("astock_datasource_allow",
                    "数据源健康门禁: 1=允许摄入 0=应停止摄入")
_g_ds_level = Gauge("astock_datasource_level",
                    "数据源健康门禁档位 (0=OK 1=DEGRADED 2=HALT 3=UNKNOWN)")
_g_ds_read_ok = Gauge("astock_datasource_read_ok",
                      "数据源门禁结论可读性 (0=读失败 => 告警源已失效)")

# [2026-09-25] 引擎数据落后 —— **按交易日**计的量化值。
#
# 为什么必须有这个指标(实测出来的缺口, 不是预防性添加):
# 厂商引擎停在 2026-09-22, 而当时应到 09-24 ⇒ 落后 **2 个交易日**。
# 而现有两条规则都盖不住这段窗口:
#   · `DataSourceHalt` 只在 `astock_datasource_allow == 0` 时响, 而门禁的降级判据
#     (落后 > 发布宽限 1 天)只让它到 DEGRADED, `allow` 仍为 1 ⇒ **不响**;
#   · `TableStaleDaily` 阈值是 **5 个自然日** 且实测 daily_bars 才 3.08 天 ⇒ 要等到第 6 天。
# 于是「厂商连续几天不发布数据」——一个**正在静默降级**的过程——完全没有告警。
# 交易日语义很关键: 3 个**自然日**里可能只含 2 个交易日(周末), 用自然日算会忽早忽晚。
_g_engine_lag = Gauge("astock_engine_lag_days",
                      "厂商引擎数据落后**交易日**数 (0=已追平; 越大越旧)")
_g_engine_lag_read_ok = Gauge("astock_engine_lag_read_ok",
                              "引擎落后结论可读性 (0=读失败 => 告警源失效)")

# [2026-09-22 死手开关接线] 该模块此前**只被喂 tick、从不被求值**
# (全仓检索 `deadman_switch.verdict` 零命中), 于是本仓唯一一个
# "失联本身即是证据"的机制恰恰是唯一没接线的那个。
# 当天实测: daemon 消失 4.6 小时(机器 16:08 重启), 全系统零告警。
_g_dm_overdue = Gauge("astock_deadman_overdue",
                      "死手开关: 是否有组件超过 3× 周期没有 tick (1=有)")
_g_dm_unknown = Gauge("astock_deadman_unknown",
                      "死手开关: 账本为空/已失效 (1=这套监控从未生效)")
_g_dm_read_ok = Gauge("astock_deadman_read_ok",
                      "死手开关结论可读性 (0=读失败 => 告警源已失效)")


def _refresh_deadman() -> None:
    """暴露死手开关的结论 (2026-09-22 批次)。

    与 `_refresh_datasource` 同一条理由: 告警链是夜里唯一会叫的人。
    而这一项尤其重要 —— 其它监控(心跳/看门狗/健康快照)都要求**监测者自己还活着**,
    只有它判的是"本该出现的 tick 没出现", 失联本身即是证据。当天正是
    "监测者与被监测者一起消失"的情形, 别的机制覆盖不到。
    """
    try:
        import health_state as _HS
        pub = _HS.read_published()
    except Exception as e:  # noqa: BLE001
        _g_dm_read_ok.set(0)
        print("deadman read err:", str(e)[:160], flush=True)
        return
    dm = ((pub or {}).get("observed") or {}).get("deadman")
    if not isinstance(dm, dict) or not dm.get("level"):
        # 快照里没有该字段: 旧版本快照。记"不可读", **不记健康**
        _g_dm_read_ok.set(0)
        _g_dm_overdue.set(0)
        _g_dm_unknown.set(0)
        return
    _g_dm_read_ok.set(1)
    lvl = str(dm.get("level"))
    _g_dm_overdue.set(1 if lvl == "OVERDUE" else 0)
    _g_dm_unknown.set(1 if lvl == "UNKNOWN" else 0)


def _refresh_engine_lag() -> None:
    """暴露厂商引擎的数据落后**交易日**数 (2026-09-25)。

    与 `_refresh_datasource` 同样的取材方式(读**健康快照**, 不在这里重跑探测):
    快照由守护进程每 ~5 分钟发布一次, 已经算好 `lag_trading_days`。

    命名取舍: 用 `astock_engine_lag_days` 而不是复用 `astock_datasource_*` ——
    门禁那三个 gauge 说的是**该不该停手**(策略层结论), 这个说的是**数据有多旧**
    (事实层量化值)。两者语义不同, 混用会让"落后 2 天"看起来像"已经停手"。
    """
    try:
        import health_state as _HS
        pub = _HS.read_published()
    except Exception as e:  # noqa: BLE001
        _g_engine_lag_read_ok.set(0)
        print("engine lag read err:", str(e)[:160], flush=True)
        return
    obs = (pub or {}).get("observed") or {}
    lag = obs.get("lag_trading_days")
    if lag is None:
        # 快照没有该字段: 可能是旧版本快照, 或探针失败(那份快照里 engine_day 也没有)。
        # 记"不可读", **不记 0** —— 记 0 会让"读不到"显示成"已追平"。
        _g_engine_lag_read_ok.set(0)
        return
    try:
        _g_engine_lag.set(float(lag))
    except Exception:  # noqa: BLE001
        _g_engine_lag_read_ok.set(0)
        return
    _g_engine_lag_read_ok.set(1)


def _refresh_datasource() -> None:
    """暴露数据源健康门禁的结论 (2026-09-22 批次)。

    为什么要有指标而不是只在面板显示: 告警链(Prometheus -> Alertmanager -> alert_hook)
    是**夜里唯一会叫的人**。门禁的价值是"数据源死了要响亮停手", 若它只在面板上可见,
    收盘后/夜间就没人知道 —— 而那正是故障通常发生的时候。

    读的是**健康快照**(发布者已算好), 不在这里重跑判定: 面板与指标服务每 3 秒轮询,
    重跑引擎探针(实测 1.71s)会拖垮它们。
    """
    try:
        import health_state as _HS
        pub = _HS.read_published()
    except Exception as e:  # noqa: BLE001
        _g_ds_read_ok.set(0)          # 显式暴露"读失败", 不留健康假象
        print("datasource read err:", str(e)[:160], flush=True)
        return
    ds = ((pub or {}).get("observed") or {}).get("datasource") or {}
    lvl = ds.get("level")
    if lvl is None:
        # 快照里没有该字段: 可能是旧版本发布的快照 —— 记为"不可读", 不记为健康
        _g_ds_read_ok.set(0)
        _g_ds_level.set(3)
        _g_ds_allow.set(1)
        return
    _g_ds_read_ok.set(1)
    _g_ds_level.set({"OK": 0, "DEGRADED": 1, "HALT": 2, "UNKNOWN": 3}.get(str(lvl), 3))
    _g_ds_allow.set(0 if lvl == "HALT" else 1)


def _refresh() -> None:
    try:
        stats = db_stats.collect_once()
        for s in stats:
            nm = s.get("name", "")
            _g_rows.labels(nm).set(s.get("rows") if s.get("rows") is not None else -1)
            ep = _epoch(s.get("last_day"))
            _g_last.labels(nm).set(ep if ep is not None else -1)
            for col, v in (s.get("na") or {}).items():
                _g_na.labels(nm, col).set(v if v is not None else -1)
            _g_dup.labels(nm).set(s.get("dup_pairs")
                                  if s.get("dup_pairs") is not None else -1)
        st = tasks_db._read_state()
        _g_run.set(1 if st.get("running") else 0)
        _refresh_strategy()
        _refresh_fusion()
        db_stats.append_history(stats)  # 60s 一条, 足够趋势分辨率
    except Exception as e:  # noqa: BLE001
        print("refresh err:", str(e)[:200], flush=True)
    # 独立 try: DRL 指标失败**不得**连带跳过 db_stats 历史落盘（上面那步已执行）
    try:
        _refresh_drl_degrade()
    except Exception as e:  # noqa: BLE001
        print("drl degrade refresh err:", str(e)[:200], flush=True)
    # 同理独立: 数据源门禁指标失败不得连带影响上面两项
    try:
        _refresh_datasource()
    except Exception as e:  # noqa: BLE001
        print("datasource refresh err:", str(e)[:200], flush=True)
    # 同理独立: 死手开关指标失败不得连带影响上面三项
    try:
        _refresh_deadman()
    except Exception as e:  # noqa: BLE001
        print("deadman refresh err:", str(e)[:200], flush=True)
    # 同理独立: 引擎落后指标失败不得连带影响上面四项
    try:
        _refresh_engine_lag()
    except Exception as e:  # noqa: BLE001
        print("engine lag refresh err:", str(e)[:200], flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--interval", type=int, default=60)
    a = ap.parse_args()
    start_http_server(a.port)
    print("metrics endpoint: http://localhost:%d/metrics  (interval=%ds)"
          % (a.port, a.interval), flush=True)
    _refresh()
    while True:
        time.sleep(a.interval)
        _refresh()


if __name__ == "__main__":
    main()
