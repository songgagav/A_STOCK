# -*- coding: utf-8 -*-
"""把「上游发布延迟」记入降级账本 `data/drl_degrade_events.jsonl`（按日幂等）。

为什么不能等到"补数据"才记
--------------------------
2026-09-21 实测: 收盘选股 19:10 触发、19:47 完成 exit=0, `target_plan` **正常产出**;
但 `section_as_of=2026-09-18` 而当天是 09-21 —— 因为**厂商到 20:10 仍未发布 09-21 数据**。
即: **产物存在 ≠ 产物可用于评估**。若不记账, 事后看 `target_plan` 只会看到"那天有产物",
无从知道它是用**三天前的截面**算出来的。

这与断供期的处置一脉相承(见 `record_data_outage.py`): 不产出 / 或产出但标注滞后,
都必须在账本里留下**可判读**的一条。

判据来自 `target_plan.json` 自身的字段:
  · `section_as_of`   —— 实际用的截面日
  · `data_lag_days`   —— **自然日**(非交易日)差值
  · `source`          —— 截面来源(实测 `h5i_view`; 注意它**不是**因子融合路径)
用法
  python scripts/record_upstream_lag.py                 # 预演(默认)
  python scripts/record_upstream_lag.py --apply         # 落盘(幂等)
退出码: 0=成功(含"无需记录/已存在"); 1=待记录但未加 --apply; 2=环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="YYYYMMDD, 缺省=今天")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import config
    import drl_degrade as D
    try:
        import trading_calendar as TC
    except Exception:  # noqa: BLE001
        TC = None

    day = a.day or dt.date.today().strftime("%Y%m%d")
    tp = os.path.join(config.DATA_DIR, "drl", day, "target_plan.json")
    print("=" * 74)
    print(f"记录「上游发布延迟」  day={day}")
    print("=" * 74)
    if not os.path.isfile(tp):
        print(f"  [跳过] 无 target_plan: {tp}（当天未产出, 应由 record_data_outage 处理）")
        return 0
    with open(tp, encoding="utf-8-sig") as f:
        j = json.load(f)
    sa = j.get("section_as_of")
    lag = j.get("data_lag_days")
    src = j.get("source")
    print(f"  section_as_of = {sa}")
    print(f"  data_lag_days = {lag}  (自然日)")
    print(f"  source        = {src}")

    # 期望截面 = 最后一个**已收盘**交易日（与 engine_bars_sync.freshness / premarket 检查同源）
    expect = None
    if TC is not None:
        try:
            expect = TC.latest_calendar_day(dt.date.fromisoformat(
                f"{day[:4]}-{day[4:6]}-{day[6:]}") - dt.timedelta(days=1))
            expect = expect.strftime("%Y%m%d") if expect else None
        except Exception:  # noqa: BLE001
            expect = None
    print(f"  期望截面      = {expect}（最后一个已收盘交易日）")

    if not lag:
        print("\n  [跳过] data_lag_days=0 —— 截面已追平, 无需记账")
        return 0

    reason = (f"上游发布延迟: {day} 的收盘选股已正常产出(target_plan 存在), 但所用截面为 "
              f"{sa}(滞后 {lag} 自然日); 厂商 free-stockdb 引擎当日数据尚未发布 "
              f"(实测 20:10 仍 engine_day={sa}) ⇒ **产物存在 ≠ 可用于评估**")
    action = ("target_plan 照常产出并标注 data_lag_days; **该日不计入干净评估样本**, "
              "顺延至 section_as_of 追上当天为止（非固定顺延一天）")
    extra = {"kind": "data_lag_upstream", "model_degrade": False,
             "section_as_of": sa, "expected_section": expect, "data_lag_days": lag,
             "plan_source": src, "upstream": "free-stockdb (stockdb.exe @127.0.0.1:7899)",
             "ref": "登记册 P1-DATA-STALE / P2-ATLASPROXY"}

    lp = D.event_ledger_path()
    if os.path.isfile(lp):
        try:
            with open(lp, encoding="utf-8-sig") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("kind") == "data_lag_upstream" and r.get("day") == day:
                        print(f"\n  [跳过] 账本里已有 {day} 的 data_lag_upstream"
                              f"（at={r.get('at')}）—— 幂等")
                        return 0
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 读账本失败, 按未记录处理: {type(e).__name__}: {e}")

    print(f"\n  原因: {reason}")
    print(f"  处置: {action}")
    if not a.apply:
        print("\n(--预演: 未落盘; 加 --apply 执行)")
        return 1
    rec = D.record_event(D.LEVEL_RETAIN, reason, action, day, extra=extra)
    print(f"\n[已记录] at={rec.get('at')} level={rec.get('level')} "
          f"({rec.get('level_name')}) kind=data_lag_upstream")
    print(f"  账本现有事件数: {D.event_count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
