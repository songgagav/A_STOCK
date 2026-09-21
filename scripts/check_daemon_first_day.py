# -*- coding: utf-8 -*-
"""daemon 首日观察清单（可执行核验）—— 2026-09-21 服务化后的第一个交易日.

为什么要有脚本而不是一张表
--------------------------
清单里的每一项都必须是**当场量出来的值**，而不是"我觉得应该没问题"。本仓反复吃过的亏
就是"看起来只是今天没数据"。故本脚本逐项取实测值，并对**该时点应当成立什么**给出判据。

两条**预期更正**（用户给的清单里这两条与实际调度不符，此处以代码为准）
--------------------------------------------------------------------
1. **09:25 不会产出 target_plan**。`daemon.py` 的选股在**盘后 19:10**（`run_daily` 全量模式）；
   09:25 是**信号冻结截止**（登记册 `P0-FREEZE-0925`，**尚未实现**）。故 09:25 的观察项应为
   "盘中引擎在跑 + 今日所用计划是否齐备"，而不是"今日计划已产出"。
2. **今日消费的是「前一交易日」的计划**。今日 2026-09-21 消费
   `data/drl/20260918/target_plan.json` —— 实测**不存在**，因为守护自 09-09 缺岗到 09-20，
   从未生成过它。**今天的第一份新计划会在 19:10 生成**（供 09-22 消费）。

`section_as_of` 的正确预期
--------------------------
`section_as_of` 只可能等于 **2026-09-21**（即"数据已追上当天"）在 **19:10 那次**成立 ——
厂商在收盘后 1–4 小时才发布当日日线，`daemon.py` 正是为此把选股从 15:05 挪到 19:10。
**09:25 时它必然是 09-18**（最后已收盘交易日），那是正确行为，不是故障。

用法
  python scripts/check_daemon_first_day.py              # 按当前时刻自动判断时点
  python scripts/check_daemon_first_day.py --phase close
退出码: 0 = 观察项均符合该时点预期; 1 = 有不符合项; 2 = 环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

SERVICE = "AStockDaemon"
TODAY = dt.date(2026, 9, 21)


def _d8(d) -> str:
    return d.strftime("%Y%m%d")


def _read_json(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


_SVC_STATE = {"1": "STOPPED", "2": "START_PENDING", "3": "STOP_PENDING", "4": "RUNNING",
              "5": "CONTINUE_PENDING", "6": "PAUSE_PENDING", "7": "PAUSED"}


def _sc_query(name=SERVICE):
    try:
        o = subprocess.run(["sc.exe", "queryex", name], capture_output=True, text=True,
                           timeout=20).stdout
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    state = pid = None
    for ln in o.splitlines():
        s = ln.strip()
        if s.startswith("STATE"):
            # 形如 "STATE : 4  RUNNING" —— 取**词**而非数字(初版取到 "4", 把 RUNNING 判成 FAIL)
            toks = s.split(":", 1)[-1].split()
            state = _SVC_STATE.get(toks[0], toks[0] if toks else None)
            if len(toks) > 1:
                state = toks[1]
        elif s.startswith("PID"):
            pid = s.split(":", 1)[-1].strip()
    return {"ok": bool(state), "state": state, "pid": pid, "raw": o.strip()}


def _daemon_procs():
    """尽力而为: 服务以 LocalSystem 运行时, 非提权会话**读不到命令行**, 故返回 None 表示"不可判定"."""
    try:
        ps = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-Process python -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.Id }"],
            capture_output=True, text=True, timeout=25).stdout
        ids = [int(x) for x in ps.split() if x.strip().isdigit()]
    except Exception:  # noqa: BLE001
        return None
    if not ids:
        return []
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*daemon.py*' } | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)\" }"],
            capture_output=True, text=True, timeout=30).stdout
        rows = [l.strip() for l in out.splitlines() if "|" in l]
        return rows
    except Exception:  # noqa: BLE001
        return None


def collect() -> dict:
    import trading_calendar as TC
    out = {"now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    out["is_trading_day"] = TC.is_trading_day(TODAY)
    out["prev_trading_day"] = str(TC.latest_calendar_day(TODAY - dt.timedelta(days=1)))
    out["service"] = _sc_query()
    out["daemon_procs"] = _daemon_procs()

    st = _read_json(os.path.join(_REPO, "logs", "daemon_state.json"))
    out["daemon_state"] = st

    hp = os.path.join(_REPO, "data", "health", "premarket.json")
    out["premarket_artifact"] = (
        {"path": hp, "exists": True,
         "mtime": dt.datetime.fromtimestamp(os.path.getmtime(hp)).strftime("%Y-%m-%d %H:%M:%S")}
        if os.path.isfile(hp) else {"path": hp, "exists": False})

    cal = _read_json(os.path.join(_REPO, "data", "trade_calendar.json"))
    if cal:
        out["calendar"] = {"n": cal.get("n"), "last": cal.get("last"),
                           "updated": cal.get("updated")}

    try:
        import h5i_sync as H
        out["h5i_max_date"] = str(H.max_bar_date(force=True))
    except Exception as e:  # noqa: BLE001
        out["h5i_max_date"] = f"<读取失败 {type(e).__name__}: {e}>"

    try:
        import engine_bars_sync as E
        p = E.engine_available()
        p["freshness"] = E.freshness(p.get("day"))
        out["engine"] = p
    except Exception as e:  # noqa: BLE001
        out["engine"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- 计划链 ----
    consume = os.path.join(_REPO, "data", "drl", _d8(TC.latest_calendar_day(TODAY - dt.timedelta(days=1))),
                           "target_plan.json")
    own = os.path.join(_REPO, "data", "drl", _d8(TODAY), "target_plan.json")
    def _plan(p):
        j = _read_json(p)
        if not j:
            return {"path": p, "exists": False}
        return {"path": p, "exists": True, "section_as_of": j.get("section_as_of"),
                "data_lag_days": j.get("data_lag_days"), "source": j.get("source"),
                "generated_at": j.get("generated_at"), "universe_size": j.get("universe_size")}
    out["plan_consumed_today"] = _plan(consume)
    out["plan_generated_today"] = _plan(own)

    # ---- DRL 护栏产物（train_metrics / post_train_validation）----
    # ★ 位置**不是** data/drl/<day>/：实测本机 16 份 data/drl/*/train_meta.json **全都**
    #   没有这两个键，而 data/drl_factor_value/20260904/train_meta.json 有。
    #   即这两个护栏属于 **drl_factor_value 那条流水线**。故此处两处都查、不预设。
    drl = []
    for sub in ("drl", "drl_factor_value"):
        fp = os.path.join(_REPO, "data", sub, _d8(TODAY), "train_meta.json")
        j = _read_json(fp)
        if not j:
            drl.append({"where": sub, "path": fp, "exists": False})
            continue
        tm = j.get("train_metrics")
        pt = j.get("post_train_validation")
        drl.append({"where": sub, "path": fp, "exists": True,
                    "ok": j.get("ok"),
                    "train_metrics_n": len(tm) if isinstance(tm, dict) else None,
                    "train_metrics_keys": sorted(tm.keys()) if isinstance(tm, dict) else None,
                    "actual_timesteps": (tm or {}).get("actual_timesteps") if isinstance(tm, dict) else None,
                    "post_train_ok": (pt or {}).get("ok") if isinstance(pt, dict) else None,
                    "post_new": (pt or {}).get("new") if isinstance(pt, dict) else None,
                    "post_delta": (pt or {}).get("delta_new_minus_old") if isinstance(pt, dict) else None,
                    "val_new": (pt or {}).get("val_new") if isinstance(pt, dict) else None})
    out["drl_artifacts"] = drl

    # ---- 降级台账 ----
    lp = os.path.join(_REPO, "data", "drl_degrade_events.jsonl")
    ev = []
    if os.path.isfile(lp):
        with open(lp, encoding="utf-8-sig") as f:
            for ln in f:
                if ln.strip():
                    try:
                        ev.append(json.loads(ln))
                    except Exception:  # noqa: BLE001
                        pass
    out["degrade_events_n"] = len(ev)
    out["degrade_last"] = ({k: ev[-1].get(k) for k in ("at", "kind", "level", "level_name")}
                           if ev else None)
    out["degrade_L3_today"] = [e for e in ev if e.get("level") == 3 and str(e.get("at", "")).startswith(str(TODAY))]
    return out


def verdict(d: dict, phase: str) -> list:
    """按**该时点应当成立什么**给出判据。返回 [(项, 结果, 说明)]。"""
    v = []
    def add(item, ok, note=""):
        v.append((item, ok, note))

    svc = d["service"]
    add("服务存在且 RUNNING", svc.get("state") == "RUNNING",
        f"state={svc.get('state')} pid={svc.get('pid')}")
    add("is_trading_day(2026-09-21)=True", d["is_trading_day"] is True,
        f"{d['is_trading_day']}（修复 P0-CALCLOBBER 前的复现值是 False）")

    st = d.get("daemon_state") or {}
    if phase in ("premarket", "intraday", "close"):
        add("daemon_state.last_health_day 已推进到今天",
            st.get("last_health_day") == str(TODAY),
            f"last_health_day={st.get('last_health_day')}（08:30 窗口内应写为 {TODAY}）")
    if phase == "intraday":
        # 仅盘中要求引擎在跑。收盘阶段引擎会按计划自停(15:03), 那时 running_day 为 None
        # **本来就是正确行为** —— 初版在 close 也要求它, 于是收盘必然误报 FAIL。
        add("daemon_state.running_day == 今天（盘中引擎已启动）",
            st.get("running_day") == str(TODAY), f"running_day={st.get('running_day')}")
    if phase == "close":
        add("daemon_state.last_close_day 已推进到今天",
            st.get("last_close_day") == str(TODAY),
            f"last_close_day={st.get('last_close_day')}（19:10 后应写为 {TODAY}）")

    hp = d["premarket_artifact"]
    if phase in ("intraday", "close"):
        add("盘前健康检查产物存在且为今天",
            hp.get("exists") and str(hp.get("mtime", "")).startswith(str(TODAY)),
            f"{hp.get('exists')} mtime={hp.get('mtime')}")

    eng = d.get("engine") or {}
    add("厂商引擎可用", eng.get("ok") is True, eng.get("error") or f"day={eng.get('day')}")
    fr = eng.get("freshness") or {}
    if phase == "close":
        # 19:10 时厂商应已发布当日数据 ⇒ 引擎该有 09-21; 若仍停在 09-18, 说明当日数据未到
        add("引擎数据已含 2026-09-21（19:10 时预期）", str(eng.get("day")) >= "20260921",
            f"engine_day={eng.get('day')}；若为 09-18 说明厂商当日数据尚未发布")
    else:
        add("引擎追平最后已收盘交易日（09-18）", fr.get("ok") is True,
            f"engine_day={fr.get('engine_day')} expected={fr.get('expected_day')}")

    # ---- 计划链：这里是最容易被误判的地方 ----
    pc = d["plan_consumed_today"]
    # 注意判据方向: 今日**确实没有**可消费的计划, 但那是**预期**(守护 09-09..09-20 缺岗),
    # 不是故障 —— 故此处按"预期缺失"判定, 避免把已知情况报成红色而让人忽略真正的红。
    add("今日消费计划按预期缺失（守护缺岗所致，非故障）", pc.get("exists") is False,
        f"{pc.get('path')} exists={pc.get('exists')}")
    pg = d["plan_generated_today"]
    if phase == "close":
        add("今日已生成新计划（19:10 后）", pg.get("exists") is True,
            f"exists={pg.get('exists')} generated_at={pg.get('generated_at')}")
        if pg.get("exists"):
            # ★ 判据按**场景**判定, 不再拿"今天"硬比。三个字段的真实语义(2026-09-21 实测订正):
            #   · section_as_of  —— 实际用的**截面日**; 期望值是"最后一个**已收盘**交易日"
            #                        (= prev_trading_day)。等于今天只在厂商已发布当日数据时成立。
            #   · data_lag_days  —— **自然日**差(09-18→09-21 = 3), **不是**交易日差, 故不会是 0/1。
            #   · source         —— **截面来源**(实测 `h5i_view`); **不是**因子融合路径 ——
            #                        融合降级在**盘中引擎**另一条日志(`fusion_or_fml`)里, 两者别混。
            got = str(pg.get("section_as_of"))
            exp = str(d.get("prev_trading_day"))
            if got == str(TODAY):
                tag = "场景1: 厂商已发布当日数据 ⇒ 该日**可作干净评估样本**"
                ok = True
            elif got == exp:
                tag = ("场景2: **厂商延迟发布** ⇒ target_plan 正常产出但截面滞后; "
                       "该日**不计入干净评估样本**, 顺延至 section_as_of 追上当天为止")
                ok = True          # 场景2 不是本仓故障, 故判 PASS 但明确标注
            else:
                tag = f"**异常**: section_as_of({got}) 既不等于今天({TODAY}) 也不等于上一交易日({exp})"
                ok = False
            add("section_as_of 场景判定", ok,
                f"{tag}\n         section_as_of={got} 期望(上一交易日)={exp} "
                f"data_lag_days={pg.get('data_lag_days')}(自然日) source={pg.get('source')}(截面来源)")
    else:
        add("盘后选股前不产出今日计划（预期如此，非故障）", pg.get("exists") is False,
            f"exists={pg.get('exists')} —— 选股在 19:10 盘后; 09:25 是**信号冻结截止**"
            f"(P0-FREEZE-0925, 未实现), 与计划产出无关")

    add("无 L3 降级事件", not d["degrade_L3_today"],
        f"今日 L3 事件数={len(d['degrade_L3_today'])}；账本末条={d.get('degrade_last')}")

    # ---- DRL 护栏产物（仅收盘后要求）----
    if phase == "close":
        arts = d.get("drl_artifacts") or []
        exist = [a for a in arts if a.get("exists")]
        add("至少一处 train_meta.json 已生成（drl 或 drl_factor_value）", bool(exist),
            "; ".join(f"{a['where']}={'有' if a.get('exists') else '无'}" for a in arts))
        withtm = [a for a in exist if a.get("train_metrics_n")]
        add("train_metrics 已落盘（含 actual_timesteps）", bool(withtm),
            "; ".join(f"{a['where']}: {a.get('train_metrics_n')} 键 "
                      f"actual_timesteps={a.get('actual_timesteps')}"
                      for a in exist) or "两处 train_meta 均无 train_metrics")
        # 字段名不是字面的 `val_new`: 实测 2026-09-21 的 train_meta 里是
        # `new` / `old` / `delta_new_minus_old`(学习后验证的新旧对比)。
        # 初版只找 val_new, 于是护栏明明在(14 键)却误报 FAIL。
        withpt = [a for a in exist if a.get("post_train_ok") is not None or a.get("post_new") is not None]
        add("post_train_validation 已落盘（含 new / delta_new_minus_old）", bool(withpt),
            "; ".join(f"{a['where']}: ok={a.get('post_train_ok')} "
                      f"new={a.get('post_new')} delta={a.get('post_delta')}"
                      for a in exist) or "两处 train_meta 均无 post_train_validation")
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase",
                    choices=["auto", "night", "premarket", "intraday", "postclose", "close"],
                    default="auto")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    d = collect()
    phase = args.phase
    if phase == "auto":
        # 时点边界必须与 daemon 的实际调度对齐, 否则会**把还没到点的项目报成 FAIL**
        # (初版把 00:xx 也算作 premarket, 于是要求"今天已做盘前健康检查")。
        t = dt.datetime.now().time()
        phase = ("night" if t < dt.time(8, 30)
                 else "premarket" if t < dt.time(9, 30)
                 else "intraday" if t < dt.time(15, 3)
                 else "postclose" if t < dt.time(19, 10)
                 else "close")

    if args.json:
        print(json.dumps({"phase": phase, "observed": d}, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 82)
    print(f"daemon 首日观察清单   现在={d['now']}   判定时点={phase}")
    print("=" * 82)
    print("\n--- 实测值 ---")
    print(f"  is_trading_day(2026-09-21) = {d['is_trading_day']}")
    print(f"  前一交易日                  = {d['prev_trading_day']}")
    svc = d["service"]
    print(f"  服务 {SERVICE:<14} = {svc.get('state')} (pid={svc.get('pid')})")
    print(f"  daemon 进程                 = {d['daemon_procs']}  (None=非提权会话读不到命令行)")
    st = d.get("daemon_state") or {}
    print(f"  daemon_state                = last_health={st.get('last_health_day')} "
          f"running_day={st.get('running_day')} last_close={st.get('last_close_day')}")
    print(f"  盘前健康检查产物            = {d['premarket_artifact'].get('exists')} "
          f"{d['premarket_artifact'].get('mtime', '')}")
    print(f"  h5i daily_bars MAX(date)    = {d['h5i_max_date']}")
    print(f"  交易日历                    = {d.get('calendar')}")
    eng = d.get("engine") or {}
    print(f"  引擎                        = ok={eng.get('ok')} day={eng.get('day')} "
          f"freshness={(eng.get('freshness') or {}).get('ok')}")
    print(f"  今日消费计划                = {d['plan_consumed_today'].get('exists')} "
          f"{d['plan_consumed_today'].get('path')}")
    print(f"  今日已生成计划              = {d['plan_generated_today'].get('exists')} "
          f"section_as_of={d['plan_generated_today'].get('section_as_of')}")
    for a in (d.get("drl_artifacts") or []):
        print(f"  DRL {a['where']:<16} = exists={a.get('exists')} "
              f"train_metrics={a.get('train_metrics_n')} "
              f"post_val_ok={a.get('post_train_ok')} val_new={a.get('val_new')}")
    print(f"  账本末条                    = {d.get('degrade_last')}")

    print("\n--- 判据 ---")
    rows = verdict(d, phase)
    bad = 0
    for item, ok, note in rows:
        tag = "PASS" if ok else "FAIL"
        if not ok:
            bad += 1
        print(f"  [{tag}] {item}")
        if note:
            print(f"         {note}")

    print("\n--- 两条预期更正（用户清单 vs 实际调度）---")
    print("  1) 09:25 **不产出** target_plan —— 选股在盘后 19:10；09:25 是信号冻结截止"
          "(P0-FREEZE-0925, 未实现)。")
    print("  2) 今日消费的是**前一交易日**的计划(data/drl/20260918/)，实测不存在："
          "守护 09-09..09-20 缺岗。首份新计划在 19:10 生成，供 09-22 消费。")
    print("  3) section_as_of == 2026-09-21 **只可能在 19:10 那次**成立（厂商收盘后 1–4 小时"
          "才发布当日数据）；09:25 时它必然是 09-18，那是正确行为。")

    print(f"\n  不符合项: {bad}/{len(rows)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
