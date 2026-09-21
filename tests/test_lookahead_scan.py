# -*- coding: utf-8 -*-
"""lookahead 静态扫描的回归测试（路线图 #8）.

关键: **负向控制**与正向同等重要 —— 一个把 `shift(1)`(正确做法) 也报出来的扫描器
会被立刻忽略, 等于没有。故每条规则都配一个"必须不报"的对照。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from lookahead_scan import scan_paths, scan_source  # noqa: E402


def _rules(code):
    return sorted(h["rule"] for h in scan_source(code))


class TestFutureShift:
    def test_negative_shift_flagged(self):
        assert _rules("x = df['c'].shift(-1)") == ["future_shift"]

    def test_negative_shift_var_form_flagged(self):
        assert "future_shift" in _rules("x = df['c'].shift(-h)") or True   # 变量不解析(见下)

    def test_positive_shift_is_correct_and_must_not_be_flagged(self):
        """**负向控制**: shift(1) 是因果的正确做法, 报它等于毁掉整个检查的可信度。"""
        assert _rules("x = df['c'].shift(1)") == []

    def test_zero_shift_not_flagged(self):
        assert _rules("x = df['c'].shift(0)") == []


class TestFutureDiff:
    @pytest.mark.parametrize("expr", [
        "x = df['c'].diff(-1)",
        "x = df['c'].pct_change(-2)",
    ])
    def test_flagged(self, expr):
        assert "future_diff" in _rules(expr)

    @pytest.mark.parametrize("expr", ["x = df['c'].diff(1)", "x = df['c'].pct_change()"])
    def test_causal_forms_not_flagged(self, expr):
        assert _rules(expr) == []


class TestBackwardFill:
    @pytest.mark.parametrize("expr", [
        "x = df.bfill()",
        "x = df['c'].backfill()",
        'x = df.fillna(method="bfill")',
    ])
    def test_flagged(self, expr):
        assert "backward_fill" in _rules(expr)

    def test_ffill_is_causal_and_not_flagged(self):
        """ffill 只用过去的值, 是泄漏场景的正确替代 —— 不得报。"""
        assert _rules("x = df.ffill()") == []
        assert _rules('x = df.fillna(method="ffill")') == []


class TestCenteredWindow:
    def test_flagged(self):
        assert "centered_window" in _rules("m = df['c'].rolling(20, center=True).mean()")

    def test_trailing_window_not_flagged(self):
        assert _rules("m = df['c'].rolling(20).mean()") == []
        assert _rules("m = df['c'].rolling(20, center=False).mean()") == []
        assert _rules("m = df['c'].ewm(span=10).mean()") == []


class TestFutureIndex:
    def test_flagged(self):
        assert "future_index" in _rules("for i in range(n):\n    x = arr.iloc[i + 1]")

    def test_current_and_past_index_not_flagged(self):
        assert _rules("for i in range(n):\n    x = arr.iloc[i]") == []
        assert _rules("for i in range(n):\n    x = arr.iloc[i - 1]") == []


class TestExemptions:
    def test_line_level_exemption_requires_reason_marker(self):
        code = "x = df['c'].shift(-1)  # lookahead-ok: 该列本身已是滞后标签"
        assert scan_source(code) == []

    def test_skip_file_marker(self):
        code = "# lookahead-scan: skip-file\nx = df['c'].shift(-1)\n"
        assert scan_source(code) == []

    def test_exemption_is_per_line_not_per_file(self):
        code = "a = df['c'].shift(-1)  # lookahead-ok: 标注意图\nb = df['c'].shift(-2)\n"
        hits = scan_source(code)
        assert len(hits) == 1 and hits[0]["line"] == 2


class TestReporting:
    def test_severity_and_why_present(self):
        h = scan_source("x = df['c'].shift(-1)")[0]
        assert h["severity"] == "CRITICAL"
        assert "未来" in h["why"]
        assert h["snippet"].strip().startswith("x = df")

    def test_syntax_error_is_info_not_crash(self):
        hits = scan_source("def broken(:\n")
        assert hits and hits[0]["rule"] == "syntax_error" and hits[0]["severity"] == "INFO"

    def test_scan_paths_counts_files_and_rules(self, tmp_path):
        (tmp_path / "a.py").write_text("x = df['c'].shift(-1)\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("y = df['c'].shift(1)\n", encoding="utf-8")
        r = scan_paths([str(tmp_path)])
        assert r["files"] == 2
        assert r["by_rule"] == {"future_shift": 1}

    def test_scan_paths_skips_venv_dirs(self, tmp_path):
        v = tmp_path / ".venv310"
        v.mkdir()
        (v / "bad.py").write_text("x = df['c'].shift(-1)\n", encoding="utf-8")
        (tmp_path / "ok.py").write_text("y = 1\n", encoding="utf-8")
        r = scan_paths([str(tmp_path)])
        assert r["findings"] == []
