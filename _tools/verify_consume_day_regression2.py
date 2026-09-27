# -*- coding: utf-8 -*-
"""一次性验收 v2: 严格复刻**梯子全 5 档**, 逐日比对 `consume_day` 改动前后的取池.

## v1 错在哪(为什么必须重做)

v1 把"第 1 档"与"第 3 档跨日回退"合并成一次倒序扫描, 于是**两侧拿到的是同一份**,
自然报"0 差异" —— 那是个**空对照**: 它没有复刻梯子的真实顺序, 因此
"改了第 1 档会不会改变最终池"这个问题**根本没被回答**。

真实风险恰恰在顺序上: 旧第 1 档读 `drl/<d>`(失败) → **第 2 档读
`daily/<d>/selection.json`**; 而新第 1 档改读 `drl/<前一交易日>` 并**直接返回**,
于是**第 2 档被跳过**。若某个消费日第 2 档本来有 `selection.json`, 新旧就会取到
**不同的池** —— 这才是必须查清的回归面。

## 做法

严格按源码顺序复刻两侧的 5 档:

    ① drl plan   ② daily selection   ③ 跨日 drl   ④ 跨日 selection   ⑤ 现场选股

  · 旧侧 == `git show HEAD:src/realtime_engine.py` 的逻辑(第 1 档 = `drl/<d>`);
  · 新侧 == 当前工作区逻辑(第 1 档 = 前一交易日目录 / consume_day 匹配)。

两侧共用同一份 `_try_load_daily_plan` 与真实磁盘, 只让**第 1 档的目录选择**不同,
从而把差异**完全归因到本次改动**。第 5 档(现场选股)很重且与本次改动无关,
故两侧**都不执行**: 落到第 5 档时记为 `(onsite)`, 只要两侧同为 onsite 即视为一致。

**只读**。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import realtime_engine as re  # noqa: E402

DRL = os.path.join(_BASE, "data", "drl")
DAILY = os.path.join(_BASE, "data", "daily")


def _pool(top_n):
    return sorted(str(it.get("canon")) for it in (top_n or [])
                  if isinstance(it, dict) and it.get("canon"))


# ============================================================
# ⚠️ 关键前提: 本对照必须模拟**引擎实际决策时刻(消费日 08:30)**的磁盘状态。
#
# 直接读"现在的磁盘"是错的, 因为 `daily/<D>/selection.json` 是 **D 日盘后**产物 ——
# 实测 `daily/20260922/selection.json` 的 mtime 是 09-22 **22:28:40**, 而引擎在当日
# **08:30** 就要选池, 那时它还不存在。
#
# 不模拟的话, 旧侧会走"第 2 档 selection_same_day"(用当天盘后才产出的池),
# 新侧走第 1 档 plan ⇒ 看起来 8/18 天池不同 —— 但那**不是真实分歧**, 而是拿
# "收盘后的磁盘"去比"盘前的决策"。实盘台账(08:30 留痕)直接否证了这一点:
# 09-22/09-23/09-24 实盘记的都是 `drl_cross_day` ⇒ 第 2 档当时**没命中**。
#
# 故对"消费日 == 当天"的 `selection.json` 一律视为**不存在**。
# ============================================================
def _selection_available(d8: str, consume_day: str) -> bool:
    return str(d8) != str(consume_day).replace("-", "")


def _keep_a_share(items):
    try:
        return re._keep_a_share(items)
    except Exception:
        return items


def _step2_same_day_selection(day: str, d: str, allow: bool):
    """第 2 档: 当日 `selection.json`。`allow=False` 时视为不存在(08:30 视角)。"""
    if not allow:
        return None, None, None
    sp = os.path.join(DAILY, d, "selection.json")
    if not os.path.exists(sp):
        return None, None, None
    try:
        with open(sp, encoding="utf-8") as f:
            cs = json.load(f)
        if cs and cs.get("top_n"):
            cs["top_n"] = _keep_a_share(cs["top_n"])
            if cs["top_n"]:
                return d, "selection_same_day", _pool(cs["top_n"])
    except Exception:
        pass
    return None, None, None


def _old_select(day: str, d8: str = None):
    """复刻 HEAD 的 5 档顺序。"""
    d = day.replace("-", "")
    prev = re._prev_trade_day(day)

    # ① 旧: 当日目录
    top_n, _info = re._try_load_daily_plan(d, day, prev_trade_day=prev)
    if top_n:
        return d, "drl_same_day", _pool(top_n)

    # ② 当日 selection.json
    s_src, s_rung, s_pool = _step2_same_day_selection(
        day, d, _selection_available(d, day))
    if s_pool:
        return s_src, s_rung, s_pool

    # ③ 跨日 drl
    try:
        dirs = sorted((x for x in os.listdir(DRL)
                       if x.isdigit() and len(x) == 8), reverse=True)
        for cand in dirs:
            if cand == d:
                continue
            top_n, _info = re._try_load_daily_plan(cand, day, prev_trade_day=prev)
            if top_n:
                return cand, "drl_cross_day", _pool(top_n)
    except Exception:
        pass

    # ④ 跨日 selection
    try:
        dirs = sorted((x for x in os.listdir(DAILY)
                       if os.path.isdir(os.path.join(DAILY, x)) and x.isdigit()),
                      reverse=True)
        for cand in dirs:
            if not _selection_available(cand, day):
                continue
            sp = os.path.join(DAILY, cand, "selection.json")
            if not os.path.exists(sp):
                continue
            try:
                with open(sp, encoding="utf-8") as f:
                    cs = json.load(f)
                if cs.get("top_n"):
                    cs["top_n"] = _keep_a_share(cs["top_n"])
                    if cs["top_n"]:
                        return cand, "selection_cross_day", _pool(cs["top_n"])
            except Exception:
                continue
    except Exception:
        pass

    # ⑤ 现场选股(两侧都不执行)
    return None, "(onsite)", None


def _new_select(day: str, d8: str = None):
    """复刻当前工作区的 5 档顺序。

    第 1 档 = `_load_daily_plan_for_consume_day`(新契约: 仅显式 `consume_day`
    或当日目录按旧窗口), 其余 4 档与旧一致。
    """
    d = day.replace("-", "")
    prev = re._prev_trade_day(day)

    # ① 新
    src, top_n, _info = re._load_daily_plan_for_consume_day(d, day, prev)
    if top_n:
        return src, "drl_same_day", _pool(top_n)

    # ②③④⑤ 与旧完全一致
    return _old_select_from_step2(day, d, prev)


def _old_select_from_step2(day, d, prev):
    s_src, s_rung, s_pool = _step2_same_day_selection(
        day, d, _selection_available(d, day))
    if s_pool:
        return s_src, s_rung, s_pool
    try:
        dirs = sorted((x for x in os.listdir(DRL)
                       if x.isdigit() and len(x) == 8), reverse=True)
        for cand in dirs:
            if cand == d:
                continue
            top_n, _info = re._try_load_daily_plan(cand, day, prev_trade_day=prev)
            if top_n:
                return cand, "drl_cross_day", _pool(top_n)
    except Exception:
        pass
    try:
        dirs = sorted((x for x in os.listdir(DAILY)
                       if os.path.isdir(os.path.join(DAILY, x)) and x.isdigit()),
                      reverse=True)
        for cand in dirs:
            if not _selection_available(cand, day):
                continue
            sp = os.path.join(DAILY, cand, "selection.json")
            if not os.path.exists(sp):
                continue
            try:
                with open(sp, encoding="utf-8") as f:
                    cs = json.load(f)
                if cs.get("top_n"):
                    cs["top_n"] = _keep_a_share(cs["top_n"])
                    if cs["top_n"]:
                        return cand, "selection_cross_day", _pool(cs["top_n"])
            except Exception:
                continue
    except Exception:
        pass
    return None, "(onsite)", None


def main() -> int:
    try:
        import trading_calendar as tc
    except Exception:
        print("ERROR: trading_calendar 不可用")
        return 2

    # ============================================================
    # 说明: "消费日 08:30 视角"的模拟规则见模块级 `_selection_available`。
    # ============================================================

    def _next_td(d8):
        d0 = datetime.date(int(d8[:4]), int(d8[4:6]), int(d8[6:8]))
        for _ in range(30):
            d0 += datetime.timedelta(days=1)
            try:
                if tc.is_trading_day(d0):
                    return d0.isoformat()
            except Exception:
                return None
        return None

    days = sorted(x for x in os.listdir(DRL)
                  if x.isdigit() and len(x) == 8
                  and os.path.exists(os.path.join(DRL, x, "target_plan.json")))
    consume_days = sorted({c for c in (_next_td(x) for x in days) if c})

    print("=" * 104)
    print("旧 vs 新 全梯子对照 (模拟**消费日 08:30** 的磁盘状态, 只读)")
    print("=" * 104)
    print("%-12s | %-13s %-19s %-4s | %-13s %-19s %-4s | %s"
          % ("消费日", "旧:来源", "旧:档位", "只", "新:来源", "新:档位", "只", "池"))
    print("-" * 104)
    diffs = []
    tier1_new = 0
    for day in consume_days:
        _d8 = day.replace("-", "")
        osrc, orung, opool = _old_select(day, _d8)
        nsrc, nrung, npool = _new_select(day, _d8)
        if nrung == "drl_same_day":
            tier1_new += 1
        same = (opool == npool) and (opool is not None)
        if not same:
            diffs.append((day, osrc, orung, opool, nsrc, nrung, npool))
        print("%-12s | %-13s %-19s %-4s | %-13s %-19s %-4s | %s"
              % (day, str(osrc), orung, len(opool or []),
                 str(nsrc), nrung, len(npool or []),
                 "OK" if same else "**差异**"))

    print("-" * 104)
    print("消费日总数              : %d" % len(consume_days))
    print("池一致的消费日          : %d" % (len(consume_days) - len(diffs)))
    print("池有差异的消费日        : %d" % len(diffs))
    print("新逻辑第 1 档命中        : %d" % tier1_new)
    if diffs:
        print()
        print("!! 池差异明细 !!")
        for day, osrc, orung, opool, nsrc, nrung, npool in diffs:
            print("  %s: 旧=%s/%s 新=%s/%s 仅旧=%s 仅新=%s"
                  % (day, osrc, orung, nsrc, nrung,
                     sorted(set(opool or []) - set(npool or [])),
                     sorted(set(npool or []) - set(opool or []))))
    print("=" * 104)
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
