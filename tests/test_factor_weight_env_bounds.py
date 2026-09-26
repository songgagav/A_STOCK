# -*- coding: utf-8 -*-
"""`FactorWeightEnv` 回合终止边界 + `_load_factor_state` 失败路径的回归（2026-09-20）.

★ 本文件依赖 torch / gymnasium / stable_baselines3 ⇒ 只在 CI 的 **regression-drl** job 跑,
  并在 **regression-core** job 里被 --ignore（见 .github/workflows/ci.yml）。

修掉的两个真实缺陷
────────────────────────────────────────────────────────────────
① **回合终止步必然 IndexError**（`FactorWeightEnv._state`）
   `step()` 是**先 `self.t += 1` 再调用 `_state()`**, 而
   `done = self.t >= len(self.ic_history)` 恰好在 `self.t == len(...)` 时为真
   ⇒ "回合正常结束的那一步"必然越界。后果: 只要 rollout 跨过 IC 序列末端, 训练就崩在
   **本该返回 done=True** 的地方。
   实测复现（`scripts/preflight_drl_metrics_realrun.py`）:
     · n_days=41, n_steps=31, total_timesteps=800 -> IndexError: index 41 out of bounds (size 41)
     · n_days=90, n_steps=32, total_timesteps=200 -> IndexError: index 90 out of bounds (size 90)
   而生产口径 `n_obs_steps=33` + `lookback=10` ⇒ `L=43`, `n_steps = min(64, L-10) = 33`
   = 回合长度 ⇒ **第一次 rollout 就会撞上**。

② **`_load_factor_state` 失败是"逃出函数"的静默路径**（`run_drl_train`）
   该调用原先在**外层 try 之外**, 异常直接冒泡给 `run_daily` 的 except ⇒
   当天"无 plan + 无告警 + 无留痕" —— 正是 DRL-4 要消除的静默路径,
   而且**恰是当前生产实际命中的那条**（因子状态源 `data/legacy_stockdb.duckdb` 已不存在,
   `duckdb.connect(..., read_only=True)` 实测抛 IOException）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

from doc_section import code_block_bounds  # noqa: E402  按**结构**定界, 取代 src[i:i+N] 的魔数窗口

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import config  # noqa: E402
import dataguard  # noqa: E402
import drl_degrade  # noqa: E402
import drl_train as T  # noqa: E402  (依赖 torch -> 只在 regression-drl job)

LOOKBACK = 10
N_FACTORS = 6


def _make_env(n_days, seed=0):
    rng = np.random.RandomState(seed)
    rets = rng.randn(n_days) * 0.012
    ic = rng.randn(n_days, N_FACTORS) * 0.05
    base = np.ones(N_FACTORS) / N_FACTORS
    regime = T._compute_regime_features(rets)
    env = T.FactorWeightEnv(ic, base, day=None, brief={}, tuning=None,
                            regime_features=regime)
    return env, ic, regime


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    dataguard.reset_warnings()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    for v in ("DRL_DEGRADE_LEDGER", "DRL_MODEL_POINTER", "DRL_VALIDATION_LEDGER"):
        monkeypatch.delenv(v, raising=False)
    yield tmp_path
    dataguard.reset_warnings()


# =====================================================================
# ① 回合终止边界
# =====================================================================

class TestEpisodeTermination:
    @pytest.mark.parametrize("n_days,n_steps", [(41, 31), (90, 32), (43, 33), (15, 5)])
    def test_episode_terminates_without_index_error(self, n_days, n_steps):
        """★ 核心回归: 走到回合末端必须返回 done=True, 而不是 IndexError。"""
        env, _, _ = _make_env(n_days)
        obs, _ = env.reset()
        assert np.all(np.isfinite(obs))
        done = False
        steps = 0
        while not done:
            obs, r, done, trunc, info = env.step(np.zeros(N_FACTORS, dtype=np.float32))
            steps += 1
            assert np.all(np.isfinite(obs)), f"第 {steps} 步状态含 NaN/Inf"
            assert steps <= n_days + 5, "回合未在预期步数内结束"
        assert done is True
        assert trunc is False
        assert steps == n_days - LOOKBACK, \
            f"回合长度应为 L-lookback={n_days - LOOKBACK}, 实际 {steps}"

    def test_terminal_step_does_not_raise_and_env_can_reset(self):
        env, _, _ = _make_env(41)
        env.reset()
        for _ in range(41 - LOOKBACK):
            _, _, done, _, _ = env.step(np.zeros(N_FACTORS, dtype=np.float32))
        assert done is True
        obs, _ = env.reset()          # SB3 会紧接 done 之后 reset
        assert np.all(np.isfinite(obs))
        assert env.t == LOOKBACK

    def test_terminal_state_uses_last_regime_row(self):
        """clamp 语义: 终止步取**最后一行** regime 特征（该步只用于返回值）。"""
        env, ic, regime = _make_env(41)
        obs, _ = env.reset()
        while True:
            obs, _, done, _, _ = env.step(np.zeros(N_FACTORS, dtype=np.float32))
            if done:
                break
        assert np.allclose(obs[-3:], regime[-1], atol=1e-6)

    def test_non_terminal_state_semantics_unchanged(self):
        """非终止步必须与修复前**逐位一致**（修复只影响越界那一刻）。"""
        env, ic, regime = _make_env(41)
        obs, _ = env.reset()
        for _ in range(5):
            obs, _, done, _, _ = env.step(np.zeros(N_FACTORS, dtype=np.float32))
            assert done is False
        t = env.t
        expect = np.concatenate([
            ic[t - LOOKBACK:t].flatten().astype(np.float32),
            env.sentiment_vec, env.stance_scalar,
            regime[t].astype(np.float32),
        ])
        assert np.allclose(obs, expect, atol=1e-6)

    def test_sb3_learn_crosses_episode_boundary(self):
        """★ 最强回归: 真实 `model.learn()` 跨过回合末端不得崩溃。

        这是生产崩溃的**精确形态**: `n_steps = min(64, L-10)` = 回合长度,
        故第一次 rollout 结束时正好撞上终止步。
        """
        from drl_train import CVaR_PPO
        n_days = 41
        n_steps = n_days - LOOKBACK          # = 31, 与生产 min(64, L-10) 同形
        env, _, _ = _make_env(n_days)
        model = CVaR_PPO("MlpPolicy", env, n_steps=n_steps, learning_rate=3e-4,
                         n_epochs=1, verbose=0, cvar_alpha=0.05, cvar_coef=0.1)
        model.learn(total_timesteps=n_steps * 3)   # 跨回合 >= 2 次
        assert model.num_timesteps >= n_steps * 3
        assert model.metric_history.n_records >= 2, "跨回合后仍应持续采集指标"


# =====================================================================
# ② 数据源失败不得逃出函数
# =====================================================================

class TestDataSourceFailureIsNotSilent:
    @staticmethod
    def _src():
        with open(os.path.join(_SRC, "drl_train.py"), encoding="utf-8") as f:
            return f.read()

    def test_source_call_is_inside_try(self):
        src = self._src()
        i = src.index("ic, rets, dates = _load_factor_state(day_dt, 60)")
        # 往前找最近的 try: / except
        head = src[:i]
        assert head.rindex("try:") > head.rindex("except "), \
            "`_load_factor_state` 必须在 try 内（原先在外层 try 之外 -> 异常逃出函数）"

    def test_source_failure_returns_degrade(self):
        src = self._src()
        i = src.index("因子状态数据源不可用")
        seg = src[i:code_block_bounds(src, i)]
        assert '_degrade_on_failure' in seg and '"degrade"' in seg

    def test_behavioural_source_failure_does_not_raise(self, monkeypatch, tmp_path):
        """行为验证: 源抛异常时 `run_drl_train` 必须**正常返回**并带上降级决策。"""
        def _boom(*a, **k):
            raise OSError("legacy duckdb 不存在")
        monkeypatch.setattr(T, "_load_factor_state", _boom)
        # drl_train 用的是 `from config import DATA_DIR` 的**模块级绑定**,
        # 故只 monkeypatch config.DATA_DIR 不够, 必须同时改 drl_train.DATA_DIR
        # （Heartbeat 会写 <DATA_DIR>/drl/<day>/）—— 指到 tmp_path 避免污染 /tmp。
        monkeypatch.setattr(T, "DATA_DIR", str(tmp_path))

        r = T.run_drl_train("2026-09-07", total_timesteps=1)
        assert r["ok"] is False
        assert "degrade" in r, "失败必须携带降级决策, 否则又是静默路径"
        assert "数据源不可用" in r["error"]
        # 无任何有效版本 -> L3 暂停交易（并留痕 + CRITICAL 告警）
        assert r["degrade"]["level"] == drl_degrade.LEVEL_HALT
        assert r["degrade"]["halt"] is True
        assert dataguard.warned_count(drl_degrade.LEVEL_WARN_KEY[3]) == 1

    def test_behavioural_source_failure_retains_previous_model(self, monkeypatch, tmp_path):
        """有可用旧模型时, 数据源失败应降为 L1 保留旧模型（而不是 L3）。"""
        vdir = os.path.join(str(tmp_path), "drl", "20260904")
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, drl_degrade.LIVE_MARKER_NAME), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(vdir, "model.zip"), "wb") as f:
            f.write(b"PK\x03\x04x")
        import json
        with open(os.path.join(vdir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"ok": True, "final_weights": {k: 1 / 6 for k in T.SCORE_FACTORS}}, f)
        drl_degrade.save_pointer("20260904", "test", level=drl_degrade.LEVEL_OK)

        def _boom(*a, **k):
            raise OSError("legacy duckdb 不存在")
        monkeypatch.setattr(T, "_load_factor_state", _boom)

        r = T.run_drl_train("2026-09-07", total_timesteps=1)
        assert r["degrade"]["level"] == drl_degrade.LEVEL_RETAIN
        assert r["degrade"]["source_day"] == "20260904"
