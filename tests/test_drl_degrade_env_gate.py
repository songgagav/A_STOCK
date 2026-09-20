# -*- coding: utf-8 -*-
"""DRL 运行环境门禁 + 显式 L3 的回归（用户决策 D，2026-09-20）.

**轻量**（不依赖 torch / gymnasium / h5i_db）⇒ 在 CI 的 regression-core job 也能跑。

背景（这是本文件存在的理由）
  `run_daily` 原先只写：
      try:
          from drl_train import run_drl_train
          ...
      except Exception as e:
          report["steps"]["drl_train"] = {"ok": False, "error": str(e)[:200]}
  而 `drl_train` 顶层 `import torch`。在缺 torch 的解释器里 ImportError 被那句 except
  **吞成 {"ok": False}** —— 没有账本、没有告警、没有 L3。这就是"静默失败"。

用户决策 D：DRL 训练暂不在生产运行，但必须
  ① 显式置 L3（跳过 L1/L2 回退，**不回退旧模型继续生成信号**）；
  ② 每次触发都落盘降级账本；
  ③ 每日复盘 / 告警可见。

本文件锁定的行为:
  · `probe_runtime()` 用 find_spec 探测, **不 import** torch/h5i_db（无副作用/耗时）
  · `probe_runtime()` 在任何解释器下都**不抛异常**
  · `force_halt()` 写 L3 事件 + 指针 level=3 + CRITICAL 告警, 且 **halt=True**
  · `force_halt()` **不得**走 resolve 的 L2 回退分支（关键: 环境坏了不能拿旧模型下单）
  · `run_daily` 的门禁必须出现在 `import drl_train` **之前**（源码级断言）
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import config  # noqa: E402
import dataguard  # noqa: E402
import drl_degrade as D  # noqa: E402  (轻量模块, 无 torch 依赖)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    dataguard.reset_warnings()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    for v in ("DRL_DEGRADE_LEDGER", "DRL_VALIDATION_LEDGER", "DRL_MODEL_POINTER"):
        monkeypatch.delenv(v, raising=False)
    yield tmp_path
    dataguard.reset_warnings()


def _events(tmp):
    p = os.path.join(str(tmp), D.EVENT_LEDGER_NAME)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _pointer(tmp):
    p = os.path.join(str(tmp), "drl", D.POINTER_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# =====================================================================
# probe_runtime
# =====================================================================

class TestProbeRuntime:
    def test_returns_required_keys(self):
        p = D.probe_runtime()
        assert set(("ok", "deps", "missing", "python", "note")) <= set(p)
        assert set(p["deps"]) == set(D.REQUIRED_RUNTIME)

    def test_missing_matches_deps(self):
        p = D.probe_runtime()
        assert set(p["missing"]) == {k for k, v in p["deps"].items() if not v}
        assert p["ok"] is (not p["missing"])

    def test_required_runtime_is_the_documented_set(self):
        assert D.REQUIRED_RUNTIME == ("h5i_db", "torch", "gymnasium",
                                      "stable_baselines3")

    def test_does_not_import_heavy_modules(self):
        """必须用 find_spec 探测 —— 若真去 import torch/h5i_db, 本测试会慢到离谱。"""
        import importlib.util as iu
        src = open(os.path.join(_SRC, "drl_degrade.py"), encoding="utf-8").read()
        i = src.index("def probe_runtime")
        seg = src[i:i + 1400]
        assert "find_spec" in seg
        assert "import torch" not in seg and "import h5i_db" not in seg
        assert iu.find_spec  # 存在性

    def test_never_raises_even_if_find_spec_blows_up(self, monkeypatch):
        """探测本身绝不能抛 —— 它是 run_daily 里的第一道防线。"""
        import importlib.util as iu
        monkeypatch.setattr(iu, "find_spec",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        p = D.probe_runtime()          # 不得抛
        assert p["ok"] is False
        assert set(p["missing"]) == set(D.REQUIRED_RUNTIME)


# =====================================================================
# force_halt
# =====================================================================

class TestForceHalt:
    def test_returns_halt_true_and_level3(self, tmp_path):
        dec = D.force_halt("2026-09-20", "缺 torch/h5i_db")
        assert dec["halt"] is True
        assert dec["level"] == D.LEVEL_HALT
        assert dec["forced"] is True
        assert dec["effective_weights"] is None
        assert dec["source_day"] is None

    def test_writes_ledger_event(self, tmp_path):
        D.force_halt("2026-09-20", "缺 torch")
        ev = _events(tmp_path)
        assert len(ev) == 1, "每次 L3 触发都必须落盘账本"
        r = ev[0]
        assert r["level"] == D.LEVEL_HALT
        assert r["blocked_plan"] is True
        assert r["severity"] == "CRITICAL"
        assert "缺 torch" in r["trigger"]

    def test_writes_pointer_level3(self, tmp_path):
        D.force_halt("2026-09-20", "缺 torch")
        assert _pointer(tmp_path)["level"] == D.LEVEL_HALT

    def test_raises_critical_alert(self):
        D.force_halt("2026-09-20", "缺 torch")
        assert dataguard.warned_count(D.LEVEL_WARN_KEY[3]) == 1

    def test_does_not_fall_back_to_old_model(self, tmp_path):
        """★ 关键: 即使存在可用旧版本, 显式 L3 **也不得**回退使用它。

        与 `resolve()` 的 L3 的区别就在这里: resolve 是"扫遍都不可用"的结论,
        force_halt 是"环境坏了, 宁停一天也不用陈旧模型"的决策。
        """
        vdir = os.path.join(str(tmp_path), "drl", "20260905")
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, D.LIVE_MARKER_NAME), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(vdir, "model.zip"), "wb") as f:
            f.write(b"PK\x03\x04x")
        with open(os.path.join(vdir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"ok": True, "final_weights": {"a": 1.0}}, f)
        assert D.version_usable("20260905") is True, "前提: 旧版本确实可用"

        dec = D.force_halt("2026-09-20", "环境不可用")
        assert dec["halt"] is True
        assert dec["effective_weights"] is None, \
            "显式 L3 不得回退旧模型（环境坏了不能拿陈旧模型下单）"
        assert dec["source_day"] is None

    def test_ledger_keeps_every_trigger(self, tmp_path):
        """② 的核心: **每次**触发都有记录, 不是只记第一次。"""
        for d in ("2026-09-20", "2026-09-21", "2026-09-22"):
            D.force_halt(d, "缺 torch")
        ev = _events(tmp_path)
        assert len(ev) == 3
        assert [e["day"] for e in ev] == ["20260920", "20260921", "20260922"]
        assert D.event_count() == 3

    def test_extra_is_recorded(self, tmp_path):
        D.force_halt("2026-09-20", "缺依赖", extra={"probe": {"missing": ["torch"]}})
        assert _events(tmp_path)[0]["probe"]["missing"] == ["torch"]

    def test_survives_unwritable_ledger(self, tmp_path, monkeypatch):
        blocker = tmp_path / "b"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("DRL_DEGRADE_LEDGER", str(blocker / "s" / "e.jsonl"))
        dec = D.force_halt("2026-09-20", "缺依赖")      # 不得抛
        assert dec["halt"] is True


# =====================================================================
# 复盘/面板/指标用的读取器
# =====================================================================

class TestVisibilityReaders:
    def test_current_level_defaults_to_ok(self):
        cur = D.current_level()
        assert cur["level"] == D.LEVEL_OK
        assert cur["blocked_plan"] is False

    def test_current_level_after_force_halt(self, tmp_path):
        D.force_halt("2026-09-20", "缺 torch")
        cur = D.current_level()
        assert cur["level"] == D.LEVEL_HALT
        assert cur["blocked_plan"] is True
        assert cur["severity"] == "CRITICAL"
        assert cur["pointer_source"] == "halt_forced"

    def test_last_event_none_when_no_ledger(self):
        assert D.last_event() is None
        assert D.event_count() == 0

    def test_last_event_returns_final_line(self, tmp_path):
        D.force_halt("2026-09-20", "第一次")
        D.force_halt("2026-09-21", "第二次")
        assert "第二次" in D.last_event()["trigger"]

    def test_readers_never_raise_on_corrupt_ledger(self, tmp_path):
        p = os.path.join(str(tmp_path), D.EVENT_LEDGER_NAME)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json\n")
        assert D.last_event() is None          # 不得抛
        assert D.event_count() == 1            # 行数仍可数


# =====================================================================
# run_daily 接线（源码级: 该文件依赖重, 不在 CI core 里 import）
# =====================================================================

class TestRunDailyGate:
    @staticmethod
    def _src():
        with open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8") as f:
            return f.read()

    def test_gate_precedes_drl_train_import(self):
        """★ 门禁必须在 `import drl_train` **之前** —— 否则缺 torch 时先崩在导入处。"""
        src = self._src()
        gate = src.index("drl_degrade.probe_runtime()")
        imp = src.index("from drl_train import run_drl_train")
        assert gate < imp, "环境门禁必须早于 drl_train 导入"

    def test_uses_force_halt_not_resolve(self):
        src = self._src()
        seg = src[src.index("drl_degrade.probe_runtime()"):]
        seg = seg[:seg.index("from drl_train import run_drl_train")]
        assert "force_halt" in seg
        assert "resolve(" not in seg, "门禁不得走 resolve（会落到 L2 回退）"

    def test_skips_training_when_forced(self):
        src = self._src()
        assert "if _drl_forced is None:" in src

    def test_writes_degrade_into_daily_report(self):
        src = self._src()
        assert 'report["steps"]["drl_degrade"]' in src
        assert 'report["drl_status"]' in src
