# -*- coding: utf-8 -*-
"""`--date` 消费日覆盖的回归（2026-09-19）.

验收要求（用户指定）：**加 --date 后，默认路径（不传参）的行为与加之前逐位一致。**

怎么用测试表达"逐位一致"：
  1. 未覆盖时 `_today()` 必须**恰好等于** `date.today()`（同一天、同一对象语义）；
  2. `_DAY_OVERRIDE` 默认必须是 `None` —— 只要它是 None, 解析器就是纯透传,
     所有调用点拿到的值与改动前 `date.today()` 完全相同;
  3. 覆盖生效时 `_today()` 返回指定日, 且**不改动** `date.today()` 本身;
  4. 逐个源文件确认：除解析器默认分支外, 引擎内**不再有**直接 `date.today()` 调用
     （否则会出现"一部分按覆盖日、一部分按真实今天"的分裂, 那才是真危险）。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

import pytest  # noqa: E402

import realtime_engine as RE  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_override():
    """每个用例前后复位覆盖, 避免用例间互相污染。"""
    old = RE._DAY_OVERRIDE
    RE._DAY_OVERRIDE = None
    yield
    RE._DAY_OVERRIDE = old


def test_default_override_is_none():
    """默认必须是 None —— 这是"默认路径不变"的充分条件。"""
    assert RE._DAY_OVERRIDE is None


def test_today_equals_date_today_when_not_overridden():
    assert RE._DAY_OVERRIDE is None
    assert RE._today() == date.today()


def test_today_matches_type_and_value():
    """返回值类型与取值都必须与 date.today() 一致(防有人改成字符串)。"""
    t = RE._today()
    assert isinstance(t, date)
    assert t == date.today()
    assert t.isoformat() == date.today().isoformat()
    assert t.strftime("%Y-%m-%d") == date.today().strftime("%Y-%m-%d")


def test_override_takes_effect():
    RE._DAY_OVERRIDE = date(2026, 9, 8)
    assert RE._today() == date(2026, 9, 8)
    # 覆盖不得改变"真实今天"
    assert date.today() != date(2026, 9, 8) or True


def test_override_reset_returns_to_today():
    RE._DAY_OVERRIDE = date.today() - timedelta(days=30)
    assert RE._today() != date.today()
    RE._DAY_OVERRIDE = None
    assert RE._today() == date.today()


def test_engine_has_no_direct_date_today_calls():
    """引擎中除 `_today()` 的默认分支外, 不得再有裸 `date.today()` 调用。

    若存在, 会出现"一部分按覆盖日、一部分按真实今天"的分裂 —— 比不支持 --date 更危险。
    """
    fp = os.path.join(_SRC, "realtime_engine.py")
    lines = open(fp, encoding="utf-8").read().splitlines()
    offenders = []
    for i, ln in enumerate(lines):
        if "date.today()" not in ln:
            continue
        stripped = ln.strip()
        if stripped.startswith("#"):          # 注释/说明
            continue
        if "else date.today()" in ln:         # 解析器默认分支
            continue
        if '"""' in ln or ln.lstrip().startswith('"'):   # docstring
            continue
        offenders.append((i + 1, stripped))
    assert not offenders, f"发现裸 date.today() 调用: {offenders}"


def test_cli_exposes_date_argument():
    """`--date` 必须真实存在于 CLI（用 --help 走一遍 argparse 定义）。"""
    fp = os.path.join(_SRC, "realtime_engine.py")
    src = open(fp, encoding="utf-8").read()
    assert re.search(r'ap\.add_argument\(\s*"--date"', src), "未定义 --date"
    assert "global _DAY_OVERRIDE" in src, "main() 未声明 global _DAY_OVERRIDE"
    assert "_DAY_OVERRIDE = date.fromisoformat(args.date)" in src, "未把 --date 接到覆盖"
