# -*- coding: utf-8 -*-
"""Dead-Man's Switch (路线图 P0 清单第 6 项, 2026-09-22 落地)。

与既有三个机制的分工(刻意不重叠)
--------------------------------
本仓已有:

  · `heartbeat.py`  —— **活性探测**: 组件在跑就每隔 N 秒刷新一次心跳文件,
    外部读 `last_seen` 判断"活着还是卡死"。
  · `flow_watchdog.py` —— 盘中**数据停流**: 进程活着但主循环卡住 / 行情源冻住。
  · `health_state.py` —— 把上面两者的结论装配成 NORMAL/DEGRADED/HALTED 单一出口。

三者都是**"等对方回应/观测对方产物"**的机制。它们有一个共同盲区:

  **监测者自己失联时, 没有任何人会发现。**
  心跳线程死了 => 心跳文件停止刷新, 但"停止刷新"与"组件没在跑"在文件层面
  不可区分; 看门狗进程被杀死 => 它不再产报告, 而"没有报告"与"一切正常、
  没什么可报"在下游同样不可区分。

Dead-Man's Switch 是反过来的:**周期任务必须主动留下一个带时间戳的 tick,
超过 3× 标称周期没有 tick 就推定失败** —— 失联本身即是证据, 不需要任何人
去"观测"它。这正是 P0-1 对账不可验证、P1-PROCALIVE 把已崩溃进程判为存活
这两类故障的通用兜底。

阈值推导(本仓房规, 不另造)
--------------------------
`3 × 标称周期` 是本仓既有惯例: `heartbeat.STALE_AFTER = 3 × HEARTBEAT_INTERVAL`
(60/20), `flow_watchdog` 的模块 docstring 明确把它写成"房规"并沿用。本模块
**沿用同一规则**, 由每个注册项自己声明 `period_s`, 阈值自动 = 3×period。

留痕
----
tick 落到 `data/deadman_ticks.jsonl`(append-only, 走 #6 哈希链)。**为什么
要链**: 死手开关的价值全在"最后一次 tick 是什么时候" —— 若那一行可以被事后
改写或删除, 告警就可以被抹掉。哈希链让"截尾/改行/换序"可验证。

本模块是**纯函数 + 一个落盘函数**, 不启动线程、不读时钟以外的状态:
`verdict()` 接受 `now` 参数, 因此可被完全确定性地测试。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time as dt_time, timedelta

_TS_FMT = "%Y-%m-%d %H:%M:%S"

#: 阈值 = 倍数 × 标称周期(与 heartbeat / flow_watchdog 的房规一致)
TIMEOUT_MULT = 3.0

#: 缺省注册表: 组件名 -> 标称周期(秒) + 说明。
#: **周期写的是"这个任务应该多久留一次 tick"**, 不是"它跑多久"。
DEFAULT_REGISTRY: dict[str, dict] = {
    "run_daily": {
        "period_s": 86400.0,      # 每个交易日的日更闭环
        "desc": "日更主链路(选股/回测/复盘)",
    },
    "daemon": {
        # 守护主循环**每分钟**留一次 tick(见 daemon.py 的 DEADMAN_EVERY_S),
        # 故标称周期 = 60s -> 阈值 180s。也就是说: 守护一旦停摆超过 3 分钟
        # 就会被判失联 —— 这比"某个文件不刷新了"要硬得多, 因为"没有 tick"
        # 本身即是证据, 不需要任何人去观测它。
        "period_s": 60.0,
        "desc": "守护进程主循环(每分钟 tick)",
    },
    "realtime_engine": {
        "period_s": 60.0,         # 盘中 tick 之外的"我还活着"声明
        "desc": "盘中引擎(收盘后应静默, 见 tolerated_offhours)",
        "trading_hours_only": True,
    },
    "obs_stack": {
        "period_s": 300.0,
        "desc": "观测栈(Prometheus/Alertmanager/AlertHook/Metrics)",
    },
}

#: 判定档位。**大写**: 与 `verdict()['level']` 同一套写法, 避免"result level 是
#: OK 而 item status 是 ok"这种需要逐处大小写转换的接口(首次实现就是小写,
#: 结果 7 个用例在大小写上失败 —— 两处命名风格不统一必然产生这种摩擦)。
OK, OVERDUE, UNKNOWN, SILENT = "OK", "OVERDUE", "UNKNOWN", "SILENT_EXPECTED"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "deadman_ticks.jsonl")


def registry_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "deadman_registry.json")


def load_registry(path: str | None = None) -> dict:
    """读注册表; 文件不存在/损坏时退回 `DEFAULT_REGISTRY`(不抛)。

    为什么允许文件覆盖: 周期是运营事实(改调度就得改周期), 不该要求改代码。
    为什么损坏时**退回缺省而不是空**: 空注册表会让 `verdict` 报"一切正常"
    —— 那是把"注册表读坏了"伪装成"系统健康", 是本仓明令避免的失效模式。
    """
    fp = registry_path(path)
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
        if isinstance(j, dict) and j:
            return j
    except Exception:  # noqa: BLE001
        pass
    return {k: dict(v) for k, v in DEFAULT_REGISTRY.items()}


def tick(component: str, *, note: str = "", ledger: str | None = None,
         now=None, write_head: bool = True) -> dict:
    """留下一个 tick(周期任务在"我做完了"时调用)。**失败不抛**。

    返回落盘的记录; 落盘失败时返回 {'ok': False, 'error': ...} 而不是抛异常
    —— 本仓纪律: 留痕不得拖垮业务路径(与 `pretrade_compliance.audit` 同)。
    """
    now = now or datetime.now()
    rec = {"component": str(component), "at": now.strftime(_TS_FMT),
           "ts": now.isoformat(timespec="seconds"), "note": str(note or "")[:200]}
    fp = ledger_path(ledger)
    try:
        import audit_chain as _AC
        _AC.append(fp, rec, now=now, write_head=write_head)
        return {"ok": True, **rec}
    except Exception as e:  # noqa: BLE001
        try:
            d = os.path.dirname(fp)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return {"ok": True, "chained": False, **rec}
        except Exception as e2:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e} | "
                                          f"{type(e2).__name__}: {e2}", **rec}


def last_ticks(ledger: str | None = None) -> dict:
    """读账本 -> {component: 最后一条 tick 记录}。坏行跳过(不抛)。"""
    fp = ledger_path(ledger)
    out: dict[str, dict] = {}
    try:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(rec, dict):
                    continue        # 合法 JSON 但不是对象(如 [1,2,3]) -> 跳过, 不让它
                                    # 变成一次 AttributeError 把整个读循环打断
                c = rec.get("component")
                if c:
                    out[str(c)] = rec
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return out
    return out


def _parse_ts(rec: dict):
    for key in ("ts", "at"):
        v = rec.get(key)
        if not v:
            continue
        s = str(v)
        for fmt in ("%Y-%m-%dT%H:%M:%S", _TS_FMT, "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s.split("+")[0][:19], fmt.replace(".%f", ""))
            except ValueError:
                continue
    return None


def _in_trading_hours(now: datetime) -> bool:
    """是否在"应当有 tick"的时段: 交易日 09:00~15:30。

    用 `trading_calendar.is_trading_day`(唯一日历源); 取不到时**保守返回 True**
    —— 会多报一次, 不会漏报。缺日历导致的漏报是不可接受的静默失效。
    """
    if now.weekday() >= 5:
        return False
    t = now.time()
    if not (dt_time(9, 0) <= t <= dt_time(15, 30)):
        return False
    try:
        from trading_calendar import is_trading_day
        return bool(is_trading_day(now.date()))
    except Exception:  # noqa: BLE001
        return True


def verdict(now=None, *, registry: dict | None = None, ledger: str | None = None,
            only: list | None = None) -> dict:
    """**纯函数**: 对每个注册项判定是否超过 3× 周期没有 tick。

    返回 {'level': 'OK'|'OVERDUE'|'UNKNOWN', 'items': [...], 'overdue': [...],
          'silent': [...], 'unknown': [...], 'reasons': [...]}

    档位语义:
      · ok              —— 有 tick 且在阈值内
      · overdue         —— 有 tick 但超过 3× 周期(或从未有过 tick 且账本非空,
                            说明该组件曾经工作过而现在停了)
      · silent_expected —— 非交易时段/非交易日的盘中引擎: **不该有 tick**,
                            不算失败(但如实列出, 不隐藏判定依据)
      · unknown         —— 账本**完全为空**: 死手开关从未被喂过。这**不是"健康"**
                            —— 它是"这套监控还没生效"。故 level 取 UNKNOWN 而不是
                            OK, 使首次上线的静默期不会被读成"一切正常"。
    """
    now = now or datetime.now()
    reg = registry if registry is not None else load_registry()
    led = last_ticks(ledger)
    if only:
        reg = {k: v for k, v in reg.items() if k in set(only)}

    items, overdue, silent, unknown = [], [], [], []
    for name, spec in sorted(reg.items()):
        period = float((spec or {}).get("period_s", 0) or 0)
        limit_s = period * TIMEOUT_MULT
        rec = led.get(name)
        item = {"component": name, "period_s": period, "limit_s": limit_s,
                "desc": (spec or {}).get("desc", ""), "last_at": None,
                "age_s": None, "status": UNKNOWN}
        if rec is None:
            if (spec or {}).get("trading_hours_only") and not _in_trading_hours(now):
                item["status"] = SILENT
                item["detail"] = "非交易时段/非交易日, 本项不该有 tick"
                silent.append(item)
            else:
                # 两种"没有 tick"必须分开(首次实现把两者混进同一分支, 靠
                # `overdue if led else unknown` 选桶 —— 而 `led` 是 dict,
                # 只要账本**有任何**条目就为真, 于是"从未 tick 过的组件"也被
                # 塞进 overdue, 使 level 恒为 OVERDUE、UNKNOWN 永不出现):
                #   · 账本**完全为空**  -> 死手开关从未生效 => UNKNOWN(不是健康,
                #                          也不是"某组件失联")
                #   · 账本非空但本组件无 tick -> 别的组件在 tick, 说明开关已生效,
                #                          那么本组件不 tick 就是**真失联** => OVERDUE
                if led:
                    item["status"] = OVERDUE
                    item["detail"] = "账本里没有本组件的任何 tick(其余组件在 tick)"
                    overdue.append(item)
                else:
                    item["status"] = UNKNOWN
                    item["detail"] = "账本为空 => 死手开关从未生效(不等于健康)"
                    unknown.append(item)
            items.append(item)
            continue
        ts = _parse_ts(rec)
        if ts is None:
            item["status"] = UNKNOWN
            item["detail"] = "最后一条 tick 的时间戳无法解析"
            unknown.append(item)
            items.append(item)
            continue
        age = (now - ts).total_seconds()
        item["last_at"] = rec.get("at") or rec.get("ts")
        item["age_s"] = round(age, 1)
        if (spec or {}).get("trading_hours_only") and not _in_trading_hours(now):
            item["status"] = SILENT
            item["detail"] = (f"非交易时段(最后 tick {item['last_at']}), "
                              f"本项不该有 tick")
            silent.append(item)
        elif age > limit_s:
            item["status"] = OVERDUE
            item["detail"] = (f"已 {age:.0f}s 无 tick > 阈值 {limit_s:.0f}s "
                              f"({TIMEOUT_MULT:g}× {period:.0f}s)")
            overdue.append(item)
        else:
            item["status"] = OK
            item["detail"] = f"最后 tick {age:.0f}s 前"
        items.append(item)

    if overdue:
        level = OVERDUE
    elif unknown and not any(it.get("status") == OK for it in items):
        # 一条 ok 都没有(全未知/全静默) => 这套监控还没真正生效。
        # **注意判定用的是 `== OK` 而不是"没有 overdue"**: 全静默(比如周末)
        # 时把所有项读成 OK 也是一种误读 —— 静默只说明"此刻不该有 tick",
        # 不说明"tick 正常"。
        level = UNKNOWN
    else:
        level = OK
    reasons = [f"{it['component']}: {it['detail']}" for it in overdue]
    reasons += [f"{it['component']}: {it['detail']}" for it in unknown]
    return {"level": level, "items": items, "overdue": overdue, "silent": silent,
            "unknown": unknown, "reasons": reasons,
            "checked_at": now.strftime(_TS_FMT),
            "ledger_entries": len(led)}


def items_ok(items: list) -> bool:
    """是否至少有一项**真正 tick 正常**(未被判 UNKNOWN)。

    注意: 静默预期(周末/收盘后)**不算**"正常" —— 它只说明"此刻不该有 tick",
    不说明 tick 链路验证过。故本函数只看 OK。
    """
    return any(it.get("status") == OK for it in items or [])


def beat(component: str, note: str = "") -> bool:
    """**接线专用的轻量 tick**: 失败静默返回 False, 绝不抛。

    给"每轮都要调用"的热路径用(守护主循环 15s 一轮、引擎每 tick):
    那里不该为了留痕引入异常面。判定失败与否由 `verdict()` 从账本读出,
    调用方不需要关心返回值。
    """
    try:
        return bool(tick(component, note=note).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Dead-Man's Switch: 周期 tick 的超时判定")
    ap.add_argument("--tick", metavar="COMPONENT", help="为某组件留一个 tick")
    ap.add_argument("--note", default="", help="tick 备注(留痕用)")
    ap.add_argument("--flush-percent", type=int, default=None, metavar="P",
                    help="批量补 tick: 把缺 tick 的组件按当前时刻登记(仅用于首次上线)")
    ap.add_argument("--status", action="store_true", help="打印各组件状态")
    ap.add_argument("--only", default="", help="只检查指定组件, 逗号分隔")
    args = ap.parse_args(argv)

    if args.tick:
        r = tick(args.tick, note=args.note)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if args.flush_percent is not None:
        # 首次上线的"打底": 不补的话第一次 verdict 只能是 UNKNOWN(账本为空)。
        # **这不是"让告警闭嘴"**: 它只是把起点登记为"现在", 之后仍旧按 3× 周期判。
        reg = load_registry()
        n = 0
        for name in reg:
            r = tick(name, note=f"flush_percent 首次登记 p={args.flush_percent}")
            n += 1 if r.get("ok") else 0
        print(f"已为 {n} 个组件登记起始 tick")
        return 0

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    v = verdict(only=only)
    icon = {OK: "  ok   ", OVERDUE: " OVERDUE", UNKNOWN: " UNKNOWN",
            SILENT: " silent "}
    for it in v["items"]:
        print(f"[{icon.get(it['status'], it['status'])}] {it['component']:18s} "
              f"{it['detail']}")
    print(f"\nlevel = {v['level']}  (账本条目 {v['ledger_entries']})")
    if v["reasons"]:
        print("原因:")
        for r in v["reasons"]:
            print(f"  - {r}")
    # OVERDUE(有 tick 记录但超时)与 UNKNOWN(死手开关从未生效)**都要给非零退出码**:
    # 两者都不是"可以放心继续"的状态 —— 前者是真失联, 后者是这套监控还没上线。
    # 只把 OVERDUE 当失败, 会让首次部署的空账本静默通过。
    return 1 if v["level"] in (OVERDUE, UNKNOWN) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
