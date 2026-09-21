# -*- coding: utf-8 -*-
"""已知坏行排除登记的回归测试（P1-ZEROFILL：标记为排除项，不改写）."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from quality_exclusions import (  # noqa: E402
    exclusions_path, explain, excluded_keys, filter_frame, is_excluded,
    load_exclusions, save_rows,
)

NOW = datetime(2026, 9, 22, 10, 0, 0)


def _mk(tmp_path, rows=None):
    fp = str(tmp_path / "excl.json")
    rows = rows if rows is not None else [
        {"symbol": "600825", "date": "20260907"},
        {"symbol": "600825", "date": "20260908"},
        {"symbol": "688291", "date": "2026-09-08"},
    ]
    save_rows(rows, reason="零填充占位行", actor="human", path=fp, now=NOW)
    return fp


class TestRegistry:
    def test_save_and_query(self, tmp_path):
        fp = _mk(tmp_path)
        assert is_excluded("600825", "20260907", path=fp) is True
        assert is_excluded("600825", "2026-09-08", path=fp) is True   # 带横线也认
        assert is_excluded("688291", "20260908", path=fp) is True
        assert is_excluded("600825", "20260909", path=fp) is False

    def test_date_format_normalized(self, tmp_path):
        fp = _mk(tmp_path)
        assert excluded_keys(path=fp) == {("600825", "20260907"), ("600825", "20260908"),
                                          ("688291", "20260908")}

    def test_idempotent(self, tmp_path):
        fp = _mk(tmp_path)
        r = save_rows([{"symbol": "600825", "date": "20260907"}], reason="重复", path=fp, now=NOW)
        assert r["added"] == 0 and r["total"] == 3

    def test_reason_and_actor_recorded(self, tmp_path):
        """排除一个数据行是有后果的动作 —— 必须能回答"谁在何时因何排除"。"""
        fp = _mk(tmp_path)
        e = explain("600825", "20260908", path=fp)
        assert e["decided_by"] == "human" and e["decided_at"] == "2026-09-22 10:00:00"
        assert "零填充" in e["reason"]

    def test_explain_returns_none_for_unknown(self, tmp_path):
        assert explain("000001", "20260908", path=_mk(tmp_path)) is None


class TestFailSafe:
    """读不到登记 => `ok=False`，**绝不静默当成"没有排除项"**。"""

    def test_missing_file_is_not_ok(self, tmp_path):
        d = load_exclusions(str(tmp_path / "absent.json"))
        assert d["ok"] is False and d["rows"] == [] and "不存在" in d["error"]

    def test_corrupt_file_is_not_ok(self, tmp_path):
        fp = tmp_path / "bad.json"
        fp.write_text("{半截", encoding="utf-8")
        assert load_exclusions(str(fp))["ok"] is False

    def test_rows_missing_fields_are_ignored_not_guessed(self, tmp_path):
        fp = tmp_path / "x.json"
        fp.write_text(json.dumps({"rows": [{"symbol": "600825"}, {"date": "20260907"},
                                           "not-a-dict"]}), encoding="utf-8")
        assert excluded_keys(path=str(fp)) == set()

    def test_default_path_under_data(self):
        assert exclusions_path().replace("\\", "/").endswith("data/quality_exclusions.json")


class TestFilterFrame:
    def _df(self):
        pd = pytest.importorskip("pandas")
        return pd.DataFrame({
            "symbol": ["600825", "600825", "600929"],
            "date": ["2026-09-07", "2026-09-08", "2026-09-07"],
            "close": [10.0, 10.1, 20.0]})

    def test_drops_only_registered_rows(self, tmp_path):
        out, dropped = filter_frame(self._df(), path=_mk(tmp_path))
        assert dropped == 2
        assert list(out["symbol"]) == ["600929"]      # 未被登记的第三方不受影响

    def test_other_days_of_same_symbol_survive(self, tmp_path):
        """**关键**: 登记的是"某天某只", 不做泛化 —— 同标的其它交易日不得被误伤。"""
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"symbol": ["600825", "600825"],
                           "date": ["2026-09-07", "2026-09-09"], "close": [10.0, 11.0]})
        out, dropped = filter_frame(df, path=_mk(tmp_path))
        assert dropped == 1 and list(out["date"]) == ["2026-09-09"]

    def test_no_exclusions_is_a_noop(self, tmp_path):
        fp = str(tmp_path / "none.json")
        out, dropped = filter_frame(self._df(), path=fp)
        assert dropped == 0 and len(out) == 3

    def test_missing_columns_is_a_noop_not_a_crash(self, tmp_path):
        pd = pytest.importorskip("pandas")
        out, dropped = filter_frame(pd.DataFrame({"a": [1]}), path=_mk(tmp_path))
        assert dropped == 0 and len(out) == 1


class TestProductionRegistry:
    def test_the_seven_rows_are_registered(self):
        """生产登记必须正好覆盖那 7 行（3 + 4），不多不少。"""
        d = load_exclusions()
        if not d.get("ok"):
            pytest.skip("生产排除登记不存在")
        keys = excluded_keys()
        expect = {("600825", "20260907"), ("600929", "20260907"), ("688432", "20260907"),
                  ("600825", "20260908"), ("600929", "20260908"), ("688291", "20260908"),
                  ("688432", "20260908")}
        assert keys == expect, f"登记集与实测坏行不一致: 多={keys-expect} 少={expect-keys}"

    def test_registry_is_readable_and_dated(self):
        d = load_exclusions()
        if not d.get("ok"):
            pytest.skip("生产排除登记不存在")
        assert d.get("updated")
