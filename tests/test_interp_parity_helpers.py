# -*- coding: utf-8 -*-
"""解释器过渡对比工具的回归（2026-09-20）.

**轻量**（只需 stdlib + 被测脚本本身）⇒ CI 的 regression-core job 可跑。

被测: `scripts/preflight_interp_parity.py` 的差异分类器与归一化。
它们决定了"差异可解释"这句话到底靠不靠得住 —— 分类错了就会出现两种坏结果:
  · 把**真实语义差异**误判成 VOLATILE/TIMESTAMP -> 假通过（危险）
  · 把**天然可变字段**误判成 SEMANTIC -> 假失败（噪声淹没真信号）

初版就在这里踩过一次: 只按**键名**白名单判易变字段, 于是 `live_state.updated` /
`data_ts` 这类墙钟时间戳被误报成 SEMANTIC。现改为**按值的形态**（整串时间戳）判定,
本文件即锁定该行为。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

import preflight_interp_parity as P  # noqa: E402


class TestDiffClassifier:
    def test_identical_is_empty(self):
        assert P._diff({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []

    def test_volatile_key_by_name(self):
        out = P._diff({"total_seconds": 1.0}, {"total_seconds": 2.0})
        assert [c for c, *_ in out] == ["VOLATILE"]

    def test_timestamp_by_value_shape(self):
        """★ 初版踩坑处的回归: 键名不在白名单里, 但值是墙钟时间戳 -> TIMESTAMP。"""
        out = P._diff({"data_ts": "2026-09-20 19:03:22"},
                      {"data_ts": "2026-09-20 19:03:28"})
        assert [c for c, *_ in out] == ["TIMESTAMP"]

    def test_timestamp_with_T_separator(self):
        out = P._diff({"x": "2026-09-20T19:03:22"}, {"x": "2026-09-20T19:03:23"})
        assert [c for c, *_ in out] == ["TIMESTAMP"]

    def test_non_timestamp_string_is_semantic(self):
        """带日期但**不是整串时间戳**的字符串不得被放过。"""
        out = P._diff({"canon": "600000.SH"}, {"canon": "600001.SH"})
        assert [c for c, *_ in out] == ["SEMANTIC"]
        out2 = P._diff({"msg": "更新于 2026-09-20 19:03:22"},
                       {"msg": "更新于 2026-09-20 19:03:29"})
        assert [c for c, *_ in out2] == ["SEMANTIC"], "嵌在文本里的时间戳不等于整串时间戳"

    def test_float_within_tolerance(self):
        out = P._diff({"v": 1.0}, {"v": 1.0 + 1e-13})
        assert [c for c, *_ in out] == ["FLOAT_TOL"]

    def test_float_beyond_tolerance_is_semantic(self):
        out = P._diff({"v": 1.0}, {"v": 1.01})
        assert [c for c, *_ in out] == ["SEMANTIC"]

    def test_bool_is_not_treated_as_float(self):
        """True/False 是 int 子类 —— 不得被浮点容差吞掉。"""
        out = P._diff({"ok": True}, {"ok": False})
        assert [c for c, *_ in out] == ["SEMANTIC"]

    def test_missing_key_is_semantic(self):
        assert [c for c, *_ in P._diff({"a": 1}, {})] == ["SEMANTIC"]
        assert [c for c, *_ in P._diff({}, {"a": 1})] == ["SEMANTIC"]

    def test_list_length_mismatch_is_semantic(self):
        out = P._diff({"t": [1, 2]}, {"t": [1, 2, 3]})
        assert [c for c, *_ in out] == ["SEMANTIC"]

    def test_nested_path_is_reported(self):
        out = P._diff({"steps": [{"rung": "a"}]}, {"steps": [{"rung": "b"}]})
        assert len(out) == 1 and out[0][1] == "$.steps[0].rung"

    def test_zero_vs_zero_point_zero_still_equal(self):
        assert P._diff({"v": 0}, {"v": 0.0}) == []


class TestReasonKey:
    def test_coverage_vs_exception_are_different_classes(self):
        """★ 本批次最重要的区分: 输出相同但原因不同（数据问题 vs 环境问题）。"""
        a = ("[fusion_or_fml] **降级** used=fml_fallback as_of=2026-09-05 "
             "reason=fusion 覆盖不足: n_sc=7 req=10 cov=0.70 < 门槛 0.8")
        b = ("[fusion_or_fml] **降级** used=fml_fallback as_of=2026-09-05 "
             "reason=fusion 异常: ModuleNotFoundError: No module named 'h5i_db'")
        assert P._reason_key(a) != P._reason_key(b)
        assert P._reason_key(a) == "覆盖不足"
        assert P._reason_key(b) == "异常"

    def test_same_reason_class_matches(self):
        a = "[x] **降级** used=y reason=fusion 覆盖不足: n_sc=3"
        b = "[x] **降级** used=y reason=fusion 覆盖不足: n_sc=9"
        assert P._reason_key(a) == P._reason_key(b)

    def test_line_without_reason_falls_back(self):
        assert P._reason_key("[x] something else") == "[x] something else"


class TestLogNormalization:
    def test_timestamps_and_durations_are_masked(self):
        got = P._norm_log("[2026-09-20 19:03:22] done in 1.23s", "")
        assert got == ["[<TS>] done in <SEC>"]

    def test_sandbox_path_is_masked(self):
        """沙箱路径必须被掩掉。掩成 <TMP> 或 <SANDBOX> 都可以 —— 两者都达到了
        "消除路径差异"的目的（`_TMP` 正则先命中时就是 <TMP>）。"""
        got = P._norm_log("out=C:\\Temp\\_interp_parity_A_x\\y.json",
                          "C:\\Temp\\_interp_parity_A_x")
        assert ("<TMP>" in got[0]) or ("<SANDBOX>" in got[0]), got[0]
        assert "interp_parity" not in got[0]

    def test_arbitrary_sandbox_path_is_masked(self):
        """不带 astock_dryrun_/_interp_parity_ 前缀的沙箱路径, 靠显式替换兜住。"""
        got = P._norm_log("wrote C:\\some\\other\\place\\state.json",
                          "C:\\some\\other\\place")
        assert "<SANDBOX>" in got[0] and "other" not in got[0]

    def test_blank_lines_dropped(self):
        assert P._norm_log("a\n\n   \nb", "") == ["a", "b"]


class TestOrchestratorDayDirFix:
    """`run_llm_commentary` 的第二个参数必须是 day_dir（无横线）—— 否则心跳落错目录。"""

    @staticmethod
    def _src():
        return open(os.path.join(_REPO, "src", "agent_orchestrator.py"),
                    encoding="utf-8").read()

    def test_no_longer_passes_day_twice(self):
        """注意: 只能断言**调用行**, 不能断言裸子串 —— 修复说明的注释里也写了
        那句旧写法（`原为 run_llm_commentary(day, day)`）, 裸子串会命中注释。"""
        assert "commentary = run_llm_commentary(day, day)" not in self._src(), \
            "不得再把带横线的 day 当作 day_dir 传入"

    def test_passes_normalized_day_dir(self):
        src = self._src()
        assert 'run_llm_commentary(day, _day_dir)' in src
        assert 'day_dir = day.replace("-", "")' in src

    def test_llm_commentary_signature_unchanged(self):
        """前提校验: 该函数第二参数确实叫 day_dir 且用于落盘。"""
        s = open(os.path.join(_REPO, "src", "llm_commentary.py"),
                 encoding="utf-8").read()
        assert "def run_llm_commentary(day: str, day_dir: str)" in s
        assert 'os.path.join(DATA_DIR, "daily", day_dir)' in s


class TestDayDirGuardsGeneralized:
    """`data/daily/` 的日期口径: 只认 **8 位数字**目录, 不许硬编码单个名字。

    为什么: 实测生产里出现过**两类**非规范目录 —— `day/`（CLI 占位符 `--day`）
    与 `2026-09-03/`（调用点把带横线的 day 当 day_dir 传）。硬编码 `!= "day"`
    只挡得住第一类, 所以必须用口径而不是名单。
    """

    @staticmethod
    def _src(name):
        return open(os.path.join(_REPO, "src", name), encoding="utf-8").read()

    def _code_lines(self, name):
        """剥掉注释行, 只看代码 —— 注释里会引用旧写法作说明, 不该被当成残留。"""
        out = []
        for ln in self._src(name).splitlines():
            s = ln.strip()
            if s.startswith("#"):
                continue
            out.append(ln)
        return "\n".join(out)

    @pytest.mark.parametrize("fname", ["dashboard.py", "health_check.py"])
    def test_no_hardcoded_day_name_skip(self, fname):
        code = self._code_lines(fname)
        assert 'in ("day",)' not in code, f"{fname} 仍用硬编码名字跳过"
        assert '!= "day"' not in code, f"{fname} 仍用硬编码名字跳过"

    @pytest.mark.parametrize("fname", ["dashboard.py", "health_check.py"])
    def test_eight_digit_guard_present(self, fname):
        code = self._code_lines(fname)
        assert "isdigit() and len(" in code, f"{fname} 缺 8 位数字口径的判定"


class TestPowerShellScriptEncoding:
    """含非 ASCII 的 `.ps1` **必须**带 UTF-8 BOM。

    为什么值得一条测试: PowerShell 在**无 BOM** 时按 ANSI（中文机器上即 GBK）解析脚本,
    中文注释被解成乱码, 而乱码字节可能破坏引号配对 ⇒ **级联语法错误**, 且报错位置离真正
    原因很远（实测报在完全无关的行上）, 极难定位。

    本会话已因此踩过两次: `scripts/setup_py310_drl_venv.ps1` 与 `ops/start_daemon.ps1`。
    第二次尤其隐蔽 —— 是**用编辑工具改过之后 BOM 被抹掉**造成的（编辑工具按 UTF-8 读、
    按无 BOM 写）。故这条测试的价值在于: 每次编辑后 CI 都会替你复查一遍。
    """

    _BOM = b"\xef\xbb\xbf"

    @staticmethod
    def _ps1_files():
        out = []
        for sub in ("scripts", "ops"):
            d = os.path.join(_REPO, sub)
            if not os.path.isdir(d):
                continue
            for n in sorted(os.listdir(d)):
                if n.lower().endswith(".ps1"):
                    out.append(os.path.join(d, n))
        return out

    def test_at_least_one_ps1_found(self):
        """前提校验: 若一个都没扫到, 这条测试就是空转。"""
        assert self._ps1_files(), "未找到任何 .ps1 —— 测试路径可能写错了"

    def test_non_ascii_ps1_has_bom(self):
        bad = []
        for p in self._ps1_files():
            raw = open(p, "rb").read()
            has_non_ascii = any(b > 0x7F for b in raw)
            has_bom = raw.startswith(self._BOM)
            if has_non_ascii and not has_bom:
                bad.append(os.path.relpath(p, _REPO))
        assert not bad, (
            "以下 .ps1 含非 ASCII 但缺 UTF-8 BOM, PowerShell 会按 ANSI/GBK 解析并级联报错: "
            + ", ".join(bad))
