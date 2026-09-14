# -*- coding: utf-8 -*-
"""取数防御: 重试 + 空表检测 (item 8, 2026-09-14).

背景(见 docs/perf-plan.md 第 2 步的"前置依赖"):
本仓库历史上出现过多次**静默失败**, 结果看起来正常但其实是错的:
  - `_load_bars_forward` 在 DB 瞬时失败时静默返回空表 ⇒ 上层用 1~2 只标的算篮子均值;
  - `_full_calendar` 把失败得到的空列表写进进程缓存 ⇒ 之后所有窗口都报"未来数据不足";
  - `run_vnpy_backtest` 对 10 只篮子只加载到 2 只时仍继续回测并把结果标成 OK。
共同点: **异常被吞、空结果被当成正常值**。本模块提供两个原语, 要求调用方显式声明
"什么算失败", 从而把这类问题变成响亮可见的告警/失败。

设计原则:
  1. 重试只针对**瞬时故障**(异常 / 意外为空), 不改变成功路径的语义;
  2. 重试耗尽后**保持原有返回**并告警(可配置为抛异常), 不引入新的失败模式;
  3. 批次级覆盖度检查(`guard_basket`)是防止"部分成功被当成成功"的最后一道闸门。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Iterable

_WARNED: dict = {}


def warn_once(key: str, msg: str) -> None:
    """同一 key 只告警一次(避免逐标的刷屏), 但**每次**都会记入计数便于审计."""
    _WARNED[key] = _WARNED.get(key, 0) + 1
    if _WARNED[key] == 1:
        print(msg, file=sys.stderr, flush=True)


def warned_count(key: str) -> int:
    return _WARNED.get(key, 0)


def reset_warnings() -> None:
    """测试用: 清空告警记录."""
    _WARNED.clear()


def _empty(x) -> bool:
    if x is None:
        return True
    try:
        return len(x) == 0
    except TypeError:
        return False


def with_retry(fn: Callable, *, tries: int = 3, base_delay: float = 0.3,
               label: str = "read", empty_is_failure: bool = False,
               raise_on_final: bool = False, warn_key: str | None = None):
    """调用 fn 并在瞬时故障时指数退避重试.

    tries            : 总尝试次数(>=1); 1 = 不重试
    empty_is_failure : 结果为空是否视为失败(默认 False —— 只有异常才重试,
                       因为"某标的在区间内确实无数据"是合法情形)
    raise_on_final   : 重试耗尽后是否抛出最后一个异常(默认 False: 返回最后结果并告警)
    返回             : (结果, 是否成功)
    """
    tries = max(int(tries), 1)
    last = None
    last_err: Exception | None = None
    for i in range(tries):
        try:
            out = fn()
            if empty_is_failure and _empty(out):
                last = out
                last_err = None
                if i < tries - 1:
                    time.sleep(base_delay * (2 ** i))
                    continue
                warn_once(warn_key or label,
                          f"[dataguard] {label}: 空结果(已重试 {tries} 次)")
                if raise_on_final:
                    raise RuntimeError(f"{label}: 空结果")
                return out, False
            return out, True
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < tries - 1:
                time.sleep(base_delay * (2 ** i))
                continue
    # 全部失败(异常路径)
    warn_once(warn_key or label,
              f"[dataguard] {label}: 连续 {tries} 次失败({type(last_err).__name__}: "
              f"{last_err})")
    if raise_on_final and last_err is not None:
        raise last_err
    return last, False


def guard_basket(n_ok: int, n_total: int, label: str, *, min_ok: int = 8,
                 min_frac: float = 0.6) -> tuple[bool, str]:
    """篮子覆盖度闸门: 有效只数过少时**判定失败**, 而不是拿残篮子算收益.

    历史事故: 并发读库时逐标的静默返回空表, 10 只篮子只用 1~2 只算了均值,
    结果被当成正常窗口写进报告。

    门槛 = min(n_total, max(min_ok, ceil(min_frac * n_total))):
      10 只 -> 需 8 只(绝对门槛主导)   3 只 -> 需 3 只(被 n_total 封顶)
    返回 (是否通过, 说明)。不通过时打印告警(每次调用都打印, 因为是硬伤)。
    """
    if n_total <= 0:
        return False, f"{label}: 篮子为空"
    import math
    need = min(int(n_total), max(int(min_ok), int(math.ceil(min_frac * n_total))))
    need = max(need, 1)
    if n_ok < need:
        msg = (f"[dataguard] {label}: 日线覆盖不足 {n_ok}/{n_total} "
               f"(要求 >= {need}) —— 判定为数据故障, 不产出结果")
        print(msg, file=sys.stderr, flush=True)
        return False, msg
    return True, f"{label}: 覆盖 {n_ok}/{n_total}"


def guard_min_rows(df, n_min: int, label: str, *, raise_on_fail: bool = False) -> bool:
    """单表最小行数检查(如"某日 valuation 至少 4000 行")."""
    n = 0 if df is None else len(df)
    if n < n_min:
        msg = f"[dataguard] {label}: 行数 {n} < {n_min} —— 疑似取数故障"
        warn_once(f"minrows:{label}", msg)
        if raise_on_fail:
            raise RuntimeError(msg)
        return False
    return True


def retry_call(fn: Callable, *, tries: int = 3, base_delay: float = 0.3,
               label: str = "read") -> object:
    """`with_retry` 的便捷版: 只取结果(失败返回最后结果/None), 供只关心值的调用方使用."""
    out, _ok = with_retry(fn, tries=tries, base_delay=base_delay, label=label)
    return out


def env_tries(var: str = "DATAGUARD_TRIES", default: int = 3) -> int:
    try:
        v = int(os.environ.get(var, default) or default)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default
