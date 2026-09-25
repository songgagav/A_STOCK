# -*- coding: utf-8 -*-
"""回填**自动触发**判据 (2026-09-25, 用户清单第 1、2 项)。

## 判据(用户指定): **A 且 B**

| 判据 | 含义 | 来源 |
|---|---|---|
| **A** 引擎缺口 | `engine_day < expected_day` | `engine_bars_sync.freshness()` 的 `engine_day` / `expected_day` |
| **B** 存储缺口 | `h5i_watermark < expected_day` | `h5i_sync.max_bar_date()` |

**触发条件 = A 且 B。** 这个合取是刻意的, 两个单条件各自都会误触发:

- **只有 A**(引擎缺口但 h5i 已补过): 引擎没数据而存储有 ⇒ **已经补过了, 再补是重复动作**。
  这正是 2026-09-25 补完 09-23/09-24 之后的状态: A 为真(引擎仍 09-22)、B 为假
  (h5i 已 09-24)。**验收要求就是"此时不触发"** —— 若判据写成"只要 A 就补",
  会每轮盘后都去拉一次全市场 5000+ 只(约 25 分钟), 纯属浪费且反复覆盖同一批数据。
- **只有 B**(h5i 落后但引擎已追平): 存储落后通常是**摄入链路**的问题
  (`engine_bars_sync` 没跑成功), 拿去 Baostock 补是**绕过主源掩盖故障** ——
  该修的是摄入, 不是找个替代源把水位推上去。

## `expected_day` 的语义(踩过坑, 必须记)

它是**"最后一个已收盘的交易日"**, 由 `trading_calendar.latest_calendar_day(今天-1)`
给出 —— **不是**官方日历的年尾(拿年尾比会把每个正常交易日都判成落后)。
判据 A/B 都以它为准。

## 日历强度是前置条件

`freshness()` 同时给出 `calendar_strength`。当它是 `official` 之外的档位
(`data_derived` / `weekday_only`)时, "最后一个已收盘交易日"会退化成
"最后一个有数据的日子" ⇒ **`engine_day` 与 `expected_day` 同时停在那天, 落后恒为 0**
⇒ A 恒假、永不触发, 而且**报告上一切正常**。

这是本仓 DISC-2 ⑥ 的形状(系统正确地不触发, 却没人知道它判定不了),
故此处**显式**把弱日历判为"不可判定"并给出说明, 而不是安静地返回"不需要补"。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

#: 默认**关闭**。回填会写生产行情库, 属不可逆动作, 必须显式开启。
ENABLED_ENV = "BACKFILL_ENABLED"
#: 最多往回补多少个自然日(防止一次补很久以前的空洞)。默认 10。
LOOKBACK_ENV = "BACKFILL_LOOKBACK_DAYS"
DEFAULT_LOOKBACK_DAYS = 10

#: 开关的**配置文件**(2026-09-25 新增)。见 `_load_switch_file` 的说明 ——
#: 为什么光有环境变量不够。
SWITCH_FP = os.path.join(os.path.dirname(_HERE), "data", "backfill_switch.json")


def _load_switch_file(path: str | None = None) -> dict:
    """读运行时开关文件 `data/backfill_switch.json`。

    ## 为什么**必须有**这条路径(实测踩到, 不是预防性设计)

    `run_daily` 是由**守护进程**(Windows 服务 `AStockDaemon`)以
    `subprocess.Popen(cmd, cwd=_BASE, ...)` 拉起的, **没有传 `env=`** ——
    即它继承的是**守护进程的环境**。

    后果: 在交互式 shell 里 `$env:BACKFILL_ENABLED=1` 再手工跑 `run_daily`
    **有效**; 但**守护自动拉起的那次完全看不到** —— 而我们要的恰恰是自动触发。
    没有这个文件, 唯一的开启方式就变成"去改服务的环境变量并重启服务",
    那既难验证也容易被忘掉(改完以为开了, 实际没生效)。

    ## 取值优先级(**与 `factor_gate` 同一约定**)

       环境变量  >  配置文件  >  代码默认值

    环境变量优先: 它让"手工跑一次带开关"成为可能(不必改文件);
    配置文件兜底: 它让**守护拉起的那次**也能拿到开关。

    **文件不存在/读不动/格式错** => 返回 `{}`(即"没配") ⇒ 落到默认关闭。
    **绝不因为读不到就当成开启** —— 那会让一个手滑的坏文件变成"自动写生产库"。

    ## ⚠️ 必须用 `utf-8-sig` 读(实测踩到)

    第一次实现用了普通 `utf-8`, 而用 PowerShell 写这个文件
    (`Out-File -Encoding UTF8` / `Set-Content -Encoding UTF8`)**会带 UTF-8 BOM**
    (`EF BB BF`)。于是 `json.load` 抛 `Unexpected UTF-8 BOM`,
    被下面的 `except` 吞掉 ⇒ **文件明明写着 `enabled: true`, 却读成"没配"**,
    开关静默保持关闭。

    这与 `metrics_server` 里 `fusion_health.json` 踩过的是**同一个坑**
    (那里已注明"Windows 工具如 PowerShell Set-Content -Encoding UTF8 会写 BOM")。
    故此处同样用 `utf-8-sig` —— 它兼容带 BOM 与不带 BOM 两种。
    """
    p = path or SWITCH_FP
    if not p or not os.path.exists(p):
        return {}
    try:
        import json
        # utf-8-sig: 兼容 PowerShell 写出的 BOM(见 docstring 说明)
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:  # noqa: BLE001
        return {}


def is_enabled(env=None, *, switch_fp=None) -> bool:
    """是否允许自动回填。**默认关闭**; 优先级: 环境变量 > 开关文件 > 关。

    只认显式的正数标志 —— 与"没配就是关"一致: 一个写错的值不该把回填打开。
    """
    e = os.environ if env is None else env
    raw = e.get(ENABLED_ENV)
    if raw is None:
        raw = _load_switch_file(switch_fp).get("enabled")
    if raw is None:
        return False
    # bool 直接判; 其余按字符串走白名单(避免 "0" 被当成真)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def lookback_days(env=None, *, switch_fp=None) -> int:
    e = os.environ if env is None else env
    raw = e.get(LOOKBACK_ENV)
    if raw is None:
        raw = _load_switch_file(switch_fp).get("lookback_days")
    if raw is None:
        return DEFAULT_LOOKBACK_DAYS
    try:
        return max(0, int(str(raw).strip()))
    except Exception:  # noqa: BLE001
        return DEFAULT_LOOKBACK_DAYS


def decide(*, engine_day, h5i_watermark, expected_day,
           calendar_strength="official", enabled=True,
           today=None, lookback=None) -> dict:
    """**纯函数**: 要不要回填, 以及补哪些日子。不碰文件系统/网络, 便于测试。

    参数都用 `YYYY-MM-DD`(或 `YYYYMMDD`)字符串; `None` 表示取不到。
    返回 `{action, triggered, reasons, missing_days, ...}`,
    其中 `action ∈ {disabled, refuse_weak_calendar, no_gap, trigger, cannot_decide}`。

    **判据顺序有意义**:
      0. 未启用            -> `disabled`(**最优先**: 关闭时不该做任何推断)
      1. 日历强度非 official -> `refuse_weak_calendar`(见模块 docstring)
      2. 关键值取不到      -> `cannot_decide`
      3. A 与 B 至少一个为假 -> `no_gap`
      4. A 且 B            -> `trigger`(并算出要补的日子)
    """
    out = {"action": None, "triggered": False, "reasons": [], "missing_days": [],
           "engine_day": _d8(engine_day), "h5i_watermark": _d8(h5i_watermark),
           "expected_day": _d8(expected_day),
           "calendar_strength": calendar_strength,
           "enabled": bool(enabled), "criteria": {}}

    if not enabled:
        out["action"] = "disabled"
        out["reasons"].append(
            f"{ENABLED_ENV} 未开启(默认关闭) —— 回填会写生产行情库, 需显式开启")
        return out

    if str(calendar_strength or "") != "official":
        out["action"] = "refuse_weak_calendar"
        out["reasons"].append(
            f"交易日历强度为 {calendar_strength!r} 而非 'official' —— "
            f"『最后一个已收盘交易日』会退化成『最后一个有数据的日子』, 落后恒为 0, "
            f"此时判据不可用(**不可判定**, 不等于不需要补)")
        return out

    if not (out["engine_day"] and out["h5i_watermark"] and out["expected_day"]):
        out["action"] = "cannot_decide"
        out["reasons"].append(
            f"关键值取不到(engine_day={out['engine_day']} / "
            f"h5i_watermark={out['h5i_watermark']} / expected_day={out['expected_day']}) —— "
            f"缺信息时**不补**(与『缺字段不据此拒单』同一立场: 不据缺失做动作)")
        return out

    a = out["engine_day"] < out["expected_day"]
    b = out["h5i_watermark"] < out["expected_day"]
    out["criteria"] = {"A_engine_gap": a, "B_h5i_gap": b}
    if not (a and b):
        out["action"] = "no_gap"
        if a and not b:
            out["reasons"].append(
                f"A 真但 B 假: 引擎停在 {out['engine_day']} 而 h5i 已到 "
                f"{out['h5i_watermark']} —— **已经补过了**, 再补是重复动作")
        elif b and not a:
            out["reasons"].append(
                f"B 真但 A 假: h5i 停在 {out['h5i_watermark']} 而引擎已到 "
                f"{out['engine_day']} —— 这是**摄入链路**的问题, "
                f"拿替代源补会掩盖主源故障")
        else:
            out["reasons"].append(
                f"两者都已追平 expected_day={out['expected_day']}")
        return out

    # A 且 B -> 触发
    days = missing_days(out["h5i_watermark"], out["expected_day"], today=today,
                        lookback=lookback)
    out["triggered"] = True
    if days:
        out["action"] = "trigger"
        out["missing_days"] = days
        out["reasons"].append(
            f"A 且 B 同时成立(引擎 {out['engine_day']} / h5i {out['h5i_watermark']} "
            f"均落后于 {out['expected_day']}) ⇒ 需回填 {len(days)} 个交易日: {days}")
    else:
        # A 且 B 成立但算不出具体日子(如 lookback=0) -> 仍报触发, 但日子为空
        out["action"] = "trigger"
        out["reasons"].append(
            "A 且 B 同时成立, 但按 lookback 算不出待补交易日(检查 "
            f"{LOOKBACK_ENV})")
    return out


def missing_days(h5i_watermark, expected_day, *, today=None, lookback=None) -> list:
    """水位与 expected_day 之间**需要补的交易日**(不含水位当天, 含 expected_day)。

    用官方日历取交易日 —— **不猜**。日历不可用时返回 `[]`(宁可不动手)。
    `lookback` 限制回溯的自然日跨度, 防一次补很久以前的大洞。
    """
    w = _d8(h5i_watermark)
    e = _d8(expected_day)
    if not (w and e) or w >= e:
        return []
    try:
        import trading_calendar as TC
        cal = sorted(str(x)[:10].replace("-", "") for x in (TC._calendar_days() or set()))
    except Exception:  # noqa: BLE001
        return []
    if not cal:
        return []
    days = [d for d in cal if w < d <= e]
    if lookback is not None:
        lb = int(lookback)
        if lb <= 0:
            return []
        base = _to_date(today) or _to_date(e)
        if base is not None:
            floor = (base - dt.timedelta(days=lb)).strftime("%Y%m%d")
            days = [d for d in days if d >= floor]
    return days


def evaluate(*, enabled=None, lookback=None, probe_engine=None, watermark=None,
             today=None) -> dict:
    """生产入口: 取真实值后调用 `decide`。**只判定, 不回填。**

    `probe_engine` / `watermark` 可注入(便于测试);
    真实实现分别走 `engine_bars_sync` 与 `h5i_sync` ——
    故本函数需要**同时**有 `stock_sdk` 与 `h5i_db` 的解释器(本仓 `.venv310`)。
    """
    enabled = is_enabled() if enabled is None else bool(enabled)
    lookback = lookback_days() if lookback is None else int(lookback)

    engine_day = expected = strength = None
    errs = []
    try:
        import engine_bars_sync as E
        p = (probe_engine or (lambda: E.engine_available()))()
        if (p or {}).get("ok"):
            f = E.freshness(p.get("day"))
            engine_day = f.get("engine_day")
            expected = f.get("expected_day")
            strength = f.get("calendar_strength") or "official"
        else:
            errs.append(f"引擎探针失败: {(p or {}).get('error')}")
            f = E.freshness(None)
            expected = f.get("expected_day")
            strength = f.get("calendar_strength") or "official"
    except Exception as e:  # noqa: BLE001
        errs.append(f"引擎侧异常: {type(e).__name__}: {e}")

    wm = watermark
    if wm is None:
        try:
            import h5i_sync
            wm = h5i_sync.max_bar_date()
        except Exception as e:  # noqa: BLE001
            errs.append(f"h5i 水位异常: {type(e).__name__}: {e}")
    wm = wm.isoformat() if isinstance(wm, dt.date) else wm

    res = decide(engine_day=engine_day, h5i_watermark=wm, expected_day=expected,
                 calendar_strength=strength or "official",
                 enabled=enabled, today=today, lookback=lookback)
    res["lookback_days"] = lookback
    if errs:
        res["errors"] = errs
    return res


def _d8(v):
    """任意日期表示 -> `YYYYMMDD`; 取不到返回 None(**不猜**)。"""
    if v is None:
        return None
    s = str(v)[:10].replace("-", "").replace("/", "")
    return s if len(s) == 8 and s.isdigit() else None


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    s = _d8(v)
    if not s:
        return None
    try:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="回填触发判据(只判定, 不回填)")
    ap.add_argument("--evaluate", action="store_true", help="用真实值判定一次")
    a = ap.parse_args(argv)
    if not a.evaluate:
        ap.print_help()
        return 0
    res = evaluate()
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
