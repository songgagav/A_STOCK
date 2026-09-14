# -*- coding: utf-8 -*-
"""取数防御原语 (dataguard): 重试 + 空表检测 + 篮子覆盖闸门.

这些测试锁定的是"静默失败必须变成可见失败"这一约束本身 ——
历史事故都是"异常被吞/空结果被当正常值"造成的 (见 docs/perf-plan.md 第 2 步)。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


@pytest.fixture(autouse=True)
def _clean():
    from dataguard import reset_warnings
    reset_warnings()
    yield
    reset_warnings()


def test_retry_succeeds_after_transient_failures():
    from dataguard import with_retry
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("db busy")
        return [1, 2, 3]

    out, ok = with_retry(flaky, tries=5, base_delay=0.0, label="t")
    assert ok is True and out == [1, 2, 3] and calls["n"] == 3


def test_retry_exhausted_returns_false_and_warns():
    from dataguard import warned_count, with_retry
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise OSError("db down")

    out, ok = with_retry(always_fail, tries=3, base_delay=0.0, label="t2",
                         warn_key="t2")
    assert ok is False and out is None and calls["n"] == 3
    assert warned_count("t2") == 1                    # 告警且只告警一次


def test_empty_is_failure_retries_then_returns_false():
    from dataguard import warned_count, with_retry
    calls = {"n": 0}

    def empty():
        calls["n"] += 1
        return pd.DataFrame()

    out, ok = with_retry(empty, tries=3, base_delay=0.0, label="t3",
                         empty_is_failure=True, warn_key="t3")
    assert ok is False and calls["n"] == 3
    assert warned_count("t3") == 1


def test_empty_is_ok_by_default():
    """默认不把空结果当失败(某标的在区间内确实无数据是合法情形)."""
    from dataguard import with_retry
    calls = {"n": 0}

    def empty():
        calls["n"] += 1
        return []

    out, ok = with_retry(empty, tries=3, base_delay=0.0, label="t4")
    assert ok is True and out == [] and calls["n"] == 1


def test_empty_raise_on_final():
    from dataguard import with_retry
    with pytest.raises(RuntimeError):
        with_retry(lambda: None, tries=2, base_delay=0.0, label="t5",
                   empty_is_failure=True, raise_on_final=True, warn_key="t5r")


def test_raise_on_final_propagates_last_exception():
    from dataguard import with_retry

    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        with_retry(boom, tries=2, base_delay=0.0, label="t6", raise_on_final=True)


def test_guard_basket_thresholds():
    from dataguard import guard_basket
    ok, _ = guard_basket(10, 10, "win")
    assert ok
    ok, _ = guard_basket(8, 10, "win")          # 10 只篮子需 8 只
    assert ok
    ok, msg = guard_basket(7, 10, "win")
    assert not ok and "覆盖不足" in msg         # 7/10 属数据故障
    ok, msg = guard_basket(1, 10, "win")        # 历史事故: 1~2 只也算均值
    assert not ok
    ok, msg = guard_basket(0, 0, "win")
    assert not ok and "篮子为空" in msg


def test_guard_basket_small_basket_not_killed():
    """小篮子(top3)门槛被 n_total 封顶, 不会因 min_ok=8 被误杀, 但缺一只即拦截."""
    from dataguard import guard_basket
    ok, _ = guard_basket(3, 3, "win")
    assert ok
    ok, _ = guard_basket(2, 3, "win")
    assert not ok


def test_guard_min_rows():
    from dataguard import guard_min_rows, warned_count
    assert guard_min_rows(pd.DataFrame({"a": range(5000)}), 4000, "valuation")
    assert not guard_min_rows(pd.DataFrame({"a": range(10)}), 4000, "valuation")
    assert warned_count("minrows:valuation") == 1
    with pytest.raises(RuntimeError):
        guard_min_rows(None, 10, "x", raise_on_fail=True)


def test_warn_once_dedups_but_counts():
    from dataguard import warn_once, warned_count
    for _ in range(3):
        warn_once("k", "should print only once")
    assert warned_count("k") == 3


def test_env_tries_default_and_override(monkeypatch):
    from dataguard import env_tries
    monkeypatch.delenv("DATAGUARD_TRIES", raising=False)
    assert env_tries() == 3
    monkeypatch.setenv("DATAGUARD_TRIES", "5")
    assert env_tries() == 5
    monkeypatch.setenv("DATAGUARD_TRIES", "0")
    assert env_tries() == 3                            # 非法值回退默认


def test_valuation_asof_uses_retry_and_warns(monkeypatch):
    """`db._valuation_asof_h5i` 的读取失败必须重试并告警, 不再静默返回空表.

    用假的 h5i store 注入: 前两次抛异常, 第三次成功 -> 应拿到结果。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import db as dbmod
    monkeypatch.setattr(dbmod, "_h5i_enabled", lambda: True)
    monkeypatch.setenv("DATAGUARD_TRIES", "3")
    monkeypatch.setenv("DATAGUARD_TRIES", "3")
    state = {"n": 0}

    class _FakeSQL:
        def sql(self, q):
            state["n"] += 1
            if state["n"] < 3:
                raise OSError("locked")
            return _FakeRes()

    class _FakeRes:
        def to_pandas(self):
            return pd.DataFrame({
                "symbol": ["600519"], "pe_ttm": [20.0], "pb": [3.0],
                "ps_ttm": [1.0], "float_shares": [1e9], "is_st": [False],
                "market_cap": [2e12], "free_cap": [1.5e12]})

    monkeypatch.setattr(dbmod, "_h5i_store", lambda: type("S", (), {"_db": _FakeSQL()})())
    out = dbmod._valuation_asof_h5i("2024-07-03")
    assert state["n"] == 3                     # 退避重试生效
    # free_cap 单位是"元", 转"亿"= /1e8 -> 1.5e12 元 = 15000 亿
    assert not out.empty and float(out["float_mv"].iloc[0]) == pytest.approx(15000.0)


def test_valuation_asof_warns_when_all_retries_fail(monkeypatch, capsys):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import db as dbmod
    monkeypatch.setattr(dbmod, "_h5i_enabled", lambda: True)
    monkeypatch.setenv("DATAGUARD_TRIES", "2")
    state = {"n": 0}

    class _FakeSQL:
        def sql(self, q):
            state["n"] += 1
            raise OSError("locked")

    monkeypatch.setattr(dbmod, "_h5i_store", lambda: type("S", (), {"_db": _FakeSQL()})())
    out = dbmod._valuation_asof_h5i("2024-07-03")
    assert state["n"] == 2                     # 用满重试
    assert out.empty
    err = capsys.readouterr().err
    assert "PIT 估值读取失败" in err           # 响亮告警, 不再静默
