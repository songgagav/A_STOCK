# -*- coding: utf-8 -*-
"""一次性判定: 门禁在**厂商正常**时能否判定, 以及快照路径为何报"未生效".

## 要回答的三问(用户清单)

1. 「数据源被判定」需要什么条件?
2. 厂商正常时, 这个条件是否满足?
3. 不满足 => 独立问题(门禁有缺陷)?

## 做法

不猜、不重跑探针, 而是把**真实落盘的 steps** 喂回门禁的两个入口:

  · 入口 A(快照路径, `health_state.gather` 的等价调用):
        `evaluate(sync_step=..., db_update_step=...)`   —— **不传 engine_probe**
  · 入口 B(开盘/收盘路径, `run_daily` 的等价调用):
        `evaluate(engine_probe=<真探针>, sync_step=..., db_update_step=...)`

用生产真实探针(`engine_bars_sync.engine_available()`)作为 B 的 engine_probe。
两侧都**只判定**, 不写账本(`evaluate` 本身是纯函数)。

**只读**。
"""

from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import datasource_gate as DG  # noqa: E402


def _steps(day8: str) -> dict:
    p = os.path.join(_BASE, "data", "daily", day8, "daily_summary.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            return (json.load(f).get("steps") or {})
    except Exception:
        return {}


def _show(tag: str, res: dict) -> None:
    print("  %-22s level=%-9s allow=%-5s items=%d halt=%s"
          % (tag, res.get("level"), res.get("allow"),
             len(res.get("items") or []), res.get("halt_sources") or []))
    for it in (res.get("items") or []):
        print("      · %-16s ok=%-5s kind=%-24s critical=%s"
              % (it.get("source"), it.get("ok"), it.get("kind"),
                 it.get("source") in set(DG.CRITICAL_SOURCES)))
    for r in (res.get("reasons") or [])[:3]:
        print("      > %s" % str(r)[:150])


def main() -> int:
    print("=" * 88)
    print("门禁判据参数")
    print("=" * 88)
    print("  FAILS_TO_HALT                 = %s" % DG.FAILS_TO_HALT)
    print("  PUBLISHER_GRACE_TRADING_DAYS  = %s" % DG.PUBLISHER_GRACE_TRADING_DAYS)
    print("  ENGINE_LAG_HALT_TRADING_DAYS  = %s" % DG.ENGINE_LAG_HALT_TRADING_DAYS)
    print("  CRITICAL_SOURCES(可致 HALT)   = %s" % (DG.CRITICAL_SOURCES,))
    print("  SRC_ENGINE                    = %s" % DG.SRC_ENGINE)

    # 真实探针(供入口 B) —— **必须用 `DG.probe_engine()`**, 不能用
    # `E.engine_available()`: 后者返回的 dict **不含 `freshness` 键**, 于是
    # `classify_engine` 会走"缺 freshness"的错误分支, 把真实结论
    # (`engine_lag_over_grace`) 盖掉。本脚本第一版就栽在这里, 报出了一个
    # **假结论**("门禁读不到新鲜度"), 差一点写进登记册。
    # 正确入口: 门禁自己跑 `engine_bars_sync.py --probe` 子进程拿带 freshness 的 dict。
    probe = None
    try:
        pr = DG.probe_engine()
        if pr.get("ok"):
            probe = pr.get("probe")
            print("\n  DG.probe_engine(): day=%s  freshness.lag=%s  kind=%s"
                  % ((probe or {}).get("day"),
                     ((probe or {}).get("freshness") or {}).get("lag_trading_days"),
                     DG.classify_engine(probe).get("kind")))
        else:
            print("\n  DG.probe_engine() 失败: %s" % pr.get("error"))
    except Exception as e:                       # noqa: BLE001
        print("\n  探针异常: %s: %s" % (type(e).__name__, e))

    for day8 in ("20260925", "20260926", "20260927"):
        print()
        print("=" * 88)
        print("data/daily/%s/daily_summary.json" % day8)
        print("=" * 88)
        st = _steps(day8)
        if not st:
            print("  (文件不存在)")
        sync = st.get("engine_bars_sync")
        dbu = st.get("db_update")
        print("  steps.engine_bars_sync : %s"
              % ("有" if sync else "**缺**"))
        print("  steps.db_update        : %s"
              % ("有" if dbu else "**缺**"))
        if isinstance(st.get("datasource_gate"), dict):
            g = st["datasource_gate"]
            print("  落盘的 datasource_gate : level=%s allow=%s"
                  % (g.get("level"), g.get("allow")))
        else:
            print("  落盘的 datasource_gate : **缺**")

        print()
        print("  入口 A = 快照路径(health_state.gather 的做法: 不传 engine_probe)")
        _show("A: sync+db_update", DG.evaluate(sync_step=sync, db_update_step=dbu))

        print()
        print("  入口 B = 开盘路径(run_daily 的做法: 传真实 engine_probe)")
        _show("B: probe+sync+db_update",
              DG.evaluate(engine_probe=probe, sync_step=sync, db_update_step=dbu))

        print()
        print("  入口 C = 只传探针(即「引擎探针是否足以判定」)")
        _show("C: probe only", DG.evaluate(engine_probe=probe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
