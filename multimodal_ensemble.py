# ============================================================
# multimodal_ensemble.py -- 多模态策略集成
#
# 3 个子策略, 不同观测空间:
#   Modality A (Factor IC): 65-dim IC历史 + LLM情绪 + stance
#   Modality B (Price Action): 20-dim 量价特征 (收益/波动/换手/技术)
#   Modality C (NeSy-TA): 13-dim 符号化特征 + 市场情绪
#
# 门控网络: 13-dim NeSy-TA特征 -> 3-dim softmax -> 子策略组合权重
# 最终动作: w_a * action_a + w_b * action_b + w_c * action_c
#
# 训练: 子策略独立预训练 + 门控网络优化组合奖励
# ============================================================

import datetime as dt
import json
import os
import sys
from typing import List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DUCKDB_PATH, MAX_STOCKS  # noqa: E402

SCORE_FACTORS = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]

# Gating network architecture
GATE_INPUT_DIM = 13   # NeSy-TA features (12) + market_panel sentiment (1)
GATE_HIDDEN = 16
GATE_OUTPUT = 3       # 3 sub-strategies
N_MODALITIES = 3

MODALITY_NAMES = ["factor_ic", "price_action", "nesy_ta"]


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [ensemble] {msg}", flush=True)


# ============================================================
# Gating Network (PyTorch MLP)
# ============================================================
class GatingNetwork:
    def __init__(self):
        self._torch = None
        self._model = None
        try:
            import torch
            import torch.nn as nn
            self._torch = torch
            self._model = nn.Sequential(
                nn.Linear(GATE_INPUT_DIM, GATE_HIDDEN),
                nn.ReLU(),
                nn.Linear(GATE_HIDDEN, 8),
                nn.ReLU(),
                nn.Linear(8, GATE_OUTPUT),
                nn.Softmax(dim=-1),
            )
            self._init_heuristic()
        except ImportError:
            pass

    def _init_heuristic(self):
        """初始化门控权重: 震荡市偏向 price_action, 趋势市偏向 factor_ic,
        极端市偏向 nesy_ta."""
        if self._model is None:
            return
        import torch
        samples, targets = [], []
        for comp in [-0.5, -0.2, 0.0, 0.2, 0.5]:
            for vol in [0.2, 0.5, 0.8]:
                for cons in [0.3, 0.6, 0.9]:
                    vec = np.zeros(GATE_INPUT_DIM, dtype=np.float32)
                    vec[6] = comp    # f_composite
                    vec[7] = cons    # f_ma_consensus
                    vec[5] = vol     # f_vol_percentile
                    vec[12] = 0.5    # sentiment (neutral)
                    # 启发式: 趋势强 -> factor_ic 权重高; 震荡 -> price_action;
                    # 高波动 -> nesy_ta 权重高
                    trend_str = abs(comp) * cons
                    if trend_str > 0.3:
                        w = [0.6, 0.2, 0.2]  # factor_ic dominant
                    elif vol > 0.7:
                        w = [0.2, 0.3, 0.5]  # nesy_ta dominant
                    elif abs(comp) < 0.15:
                        w = [0.3, 0.5, 0.2]  # price_action dominant
                    else:
                        w = [0.4, 0.3, 0.3]  # balanced
                    samples.append(vec)
                    targets.append(w)
        X = torch.tensor(np.array(samples), dtype=torch.float32)
        y = torch.tensor(np.array(targets), dtype=torch.float32)
        opt = torch.optim.Adam(self._model.parameters(), lr=0.01)
        loss_fn = torch.nn.MSELoss()
        for _ in range(500):
            opt.zero_grad()
            loss = loss_fn(self._model(X), y)
            loss.backward()
            opt.step()

    def predict(self, gate_input: np.ndarray) -> np.ndarray:
        """返回 3 维权重向量, sum=1."""
        if self._model is None:
            return np.array([0.4, 0.3, 0.3], dtype=np.float32)
        import torch
        x = torch.tensor(gate_input.reshape(1, -1), dtype=torch.float32)
        with torch.no_grad():
            return self._model(x).numpy().flatten()

    def save(self, path: str):
        if self._model is None:
            return
        import torch
        torch.save(self._model.state_dict(), path)

    def load(self, path: str):
        if self._model is None or not os.path.exists(path):
            return False
        import torch
        self._model.load_state_dict(torch.load(path, weights_only=True))
        self._model.eval()
        return True


# ============================================================
# 观测空间构建
# ============================================================
def build_price_action_obs(day: dt.date, lookback: int = 10) -> np.ndarray:
    """构建 Modality B 观测: 20维量价特征.
    从 daily_bars 计算全A均值的收益/波动/换手/技术指标序列."""
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute("""
            SELECT DISTINCT date FROM daily_bars
            WHERE date <= ? AND date >= ?
            ORDER BY date
        """, [day, day - dt.timedelta(days=60)]).fetchall()
        dates = [r[0] for r in rows]
        if len(dates) < 20:
            return np.zeros(20, dtype=np.float32)

        obs_sequence = []
        for i, d in enumerate(dates):
            prev = dates[i - 1] if i > 0 else None
            df = con.execute("""
                SELECT AVG(change_pct) as avg_ret,
                       AVG(turnover) as avg_turn,
                       AVG(amount) as avg_amount,
                       SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) as up_ratio,
                       COUNT(*) as n
                FROM daily_bars
                WHERE date = ? AND close > 0 AND change_pct IS NOT NULL
            """, [d]).fetchone()
            # 中位数和标准差用 numpy 计算 (DuckDB STDDEV 单值会报错)
            if df and df[4] and df[4] > 1:
                chg_df = con.execute("""
                    SELECT change_pct FROM daily_bars
                    WHERE date = ? AND close > 0 AND change_pct IS NOT NULL
                """, [d]).fetchdf()
                chg = pd.to_numeric(chg_df["change_pct"], errors="coerce").dropna()
                std_ret = float(chg.std()) if len(chg) > 1 else 0.0
                med_ret = float(chg.median()) if len(chg) > 0 else 0.0
            else:
                std_ret = 0.0
                med_ret = 0.0
            if df and df[0] is not None:
                avg_ret = float(df[0] or 0)
                avg_turn = float(df[1] or 0)
                avg_amount = float(df[2] or 0)
                up_ratio = float(df[3] or 0.5)
            else:
                avg_ret = std_ret = avg_turn = avg_amount = med_ret = 0.0
                up_ratio = 0.5

            ret_5 = 0.0
            if i >= 5:
                r = con.execute("""
                    SELECT AVG(b.close / p.close - 1.0)
                    FROM daily_bars b
                    JOIN daily_bars p ON b.symbol = p.symbol AND p.date = ?
                    WHERE b.date = ? AND b.close > 0 AND p.close > 0
                """, [dates[i - 5], d]).fetchone()
                ret_5 = float(r[0]) if r and r[0] is not None else 0.0

            obs_sequence.append([
                avg_ret, std_ret, avg_turn, avg_amount / 1e8,
                med_ret, up_ratio, ret_5,
            ])
    finally:
        con.close()

    if len(obs_sequence) < lookback:
        return np.zeros(20, dtype=np.float32)

    # 取最近 lookback 天, 展开为 7*lookback 维, 降维到 20 维
    seq = np.array(obs_sequence[-lookback:], dtype=np.float32)
    seq = np.where(np.isfinite(seq), seq, 0.0)  # NaN/Inf -> 0
    # 用 PCA-like 降维: 取每列最近值 + 均值 + 趋势
    latest = seq[-1]          # 7 dim
    mean = seq.mean(axis=0)   # 7 dim
    trend = seq[-1] - seq[0]  # 7 dim (last - first)
    # 总共 21 维, 截取 20
    vec = np.concatenate([latest, mean, trend[:6]]).astype(np.float32)
    vec = np.where(np.isfinite(vec), vec, 0.0)
    return vec[:20]


def build_nesy_ta_obs(day_dir: str) -> np.ndarray:
    """构建 Modality C 观测: 13维 NeSy-TA 特征."""
    from symbolic_ta import load_symbolic_state
    from neural_ta import build_input_vector

    sym = load_symbolic_state(day_dir)
    if not sym or not sym.get("ok"):
        return np.zeros(13, dtype=np.float32)

    # 尝试读 market_panel
    panel = None
    try:
        from market_panel import compute_market
        panel = compute_market(sym["day"])
    except Exception:
        pass

    return build_input_vector(sym, panel)


# ============================================================
# 多模态集成环境
# ============================================================
class MultiModalEnsemble:
    """多模态策略集成器.

    3 个子策略各自输出 6 维因子权重调整,
    门控网络动态组合, 输出最终 6 维动作.
    """

    def __init__(self, sub_agents: dict, gating: GatingNetwork):
        self.sub_agents = sub_agents      # {name: PPO model}
        self.gating = gating
        self._torch = None
        try:
            import torch
            self._torch = torch
        except ImportError:
            pass

    def predict(self, gate_input: np.ndarray,
                obs_a: np.ndarray, obs_b: np.ndarray, obs_c: np.ndarray,
                deterministic: bool = True) -> np.ndarray:
        """输出最终 6 维因子权重调整."""
        weights = self.gating.predict(gate_input)

        actions = []
        for name, obs in [("factor_ic", obs_a), ("price_action", obs_b), ("nesy_ta", obs_c)]:
            agent = self.sub_agents.get(name)
            if agent is not None:
                action, _ = agent.predict(obs, deterministic=deterministic)
                actions.append(action)
            else:
                actions.append(np.zeros(6, dtype=np.float32))

        # 加权组合
        combined = np.zeros(6, dtype=np.float32)
        for w, a in zip(weights, actions):
            combined += w * np.asarray(a, dtype=np.float32)

        return combined, weights

    def save(self, out_dir: str):
        """保存所有子模型和门控网络."""
        os.makedirs(out_dir, exist_ok=True)
        for name, agent in self.sub_agents.items():
            if agent is not None:
                agent.save(os.path.join(out_dir, f"agent_{name}.zip"))
        self.gating.save(os.path.join(out_dir, "gating_network.pt"))
        _log(f"Ensemble saved to {out_dir}")

    @classmethod
    def load(cls, in_dir: str, gate_input_dim: int = GATE_INPUT_DIM):
        """加载已保存的集成模型."""
        from stable_baselines3 import PPO
        gating = GatingNetwork()
        gating.load(os.path.join(in_dir, "gating_network.pt"))
        sub_agents = {}
        for name in MODALITY_NAMES:
            path = os.path.join(in_dir, f"agent_{name}.zip")
            if os.path.exists(path):
                sub_agents[name] = PPO.load(path)
            else:
                sub_agents[name] = None
        return cls(sub_agents, gating)


# ============================================================
# 训练: 多模态集成
# ============================================================
def run_multimodal_train(day: str, total_timesteps: int = 400,
                          n_epochs: int = 4) -> dict:
    """训练多模态集成.

    1. 为每个子策略构建独立环境和观测
    2. 独立训练各子策略
    3. 训练门控网络 (优化组合奖励)
    4. 保存集成模型
    """
    from stable_baselines3 import PPO
    import gymnasium

    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    # 加载共享数据
    from drl_train import (_load_factor_state, _load_base_weights,
                           _apply_brief_multiplier, _load_vnpy_signal, _vnpy_reward,
                           _load_perf_report, _attribution_reward)

    ic, rets, dates = _load_factor_state(day_dt, 60)
    if ic is None or len(rets) < 15:
        return {"ok": False, "error": "数据不足"}

    base_w = _load_base_weights()

    # 读 LLM brief
    brief = {}
    try:
        from pre_drl_brief import load_pre_drl_brief
        brief = load_pre_drl_brief(day_dir) or {}
    except Exception:
        pass

    prior_w, _ = _apply_brief_multiplier(base_w, brief)

    # NeSy-TA tuning
    nesy_tuning = None
    try:
        from neural_ta import compute_tuning
        nesy_tuning = compute_tuning(day_dir, use_heuristic=False)
    except Exception as e:
        _log(f"NeSy-TA tuning failed: {e}")

    # ---- 子策略 1: Factor IC (Modality A) ----
    _log("Training Modality A: Factor IC...")
    from drl_train import FactorWeightEnv
    env_a = FactorWeightEnv(ic, prior_w, day=day_dt, brief=brief, tuning=nesy_tuning)
    agent_a = PPO("MlpPolicy", env_a, n_steps=min(64, len(rets) - 10),
                  learning_rate=3e-4, n_epochs=n_epochs, verbose=0)
    agent_a.learn(total_timesteps=total_timesteps)

    # ---- 子策略 2: Price Action (Modality B) ----
    _log("Training Modality B: Price Action...")
    pa_obs = build_price_action_obs(day_dt, lookback=10)
    # 用 price_action 特征构建简化环境
    class PriceActionEnv(gymnasium.Env):
        def __init__(self, pa_obs, base_weights, ic_history, tuning):
            super().__init__()
            from gymnasium import spaces
            self.pa_obs = pa_obs.astype(np.float32)
            self.base_weights = base_weights.astype(np.float32)
            self.ic_history = ic_history.astype(np.float32)
            self.t = 0
            self.n_factors = len(base_weights)
            self.weights = base_weights.copy()
            self.n_steps = len(ic_history) - 10
            t = tuning or {}
            self.delta_scale = float(t.get("delta_scale", 0.5))
            self.temperature = float(t.get("temperature", 1.0))
            self.weight_clip = float(t.get("weight_clip", 0.6))
            obs_dim = len(pa_obs) + self.n_factors
            self.observation_space = spaces.Box(
                low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float32)
            self.action_space = spaces.Box(
                low=-1.0, high=1.0, shape=(self.n_factors,), dtype=np.float32)

        def reset(self, *, seed=None, options=None):
            self.t = 0
            self.weights = self.base_weights.copy()
            return self._state(), {}

        def _state(self):
            return np.concatenate([self.pa_obs, self.weights]).astype(np.float32)

        def step(self, action):
            action = np.asarray(action, dtype=np.float32)
            noise = np.random.randn(self.n_factors).astype(np.float32) * (self.temperature - 1.0) * 0.02
            delta = (np.tanh(action) + noise) * 0.05 * self.delta_scale
            new_w = self.weights + delta
            lo = max(0.01, 0.02 - (self.weight_clip - 0.3) * 0.05)
            new_w = np.clip(new_w, lo, self.weight_clip)
            new_w = new_w / new_w.sum()
            self.weights = new_w
            if self.t < len(self.ic_history):
                reward = float(np.dot(self.weights, self.ic_history[self.t])) * 10
            else:
                reward = 0.0
            self.t += 1
            done = self.t >= self.n_steps
            return self._state(), float(reward), bool(done), bool(False), {"weights": new_w.tolist()}

    env_b = PriceActionEnv(pa_obs, prior_w, ic, nesy_tuning)
    agent_b = PPO("MlpPolicy", env_b, n_steps=min(64, len(rets) - 10),
                  learning_rate=3e-4, n_epochs=n_epochs, verbose=0)
    agent_b.learn(total_timesteps=total_timesteps)

    # ---- 子策略 3: NeSy-TA (Modality C) ----
    _log("Training Modality C: NeSy-TA...")
    nesy_obs = build_nesy_ta_obs(day_dir)

    class NeSyTAEnv(gymnasium.Env):
        def __init__(self, nesy_obs, base_weights, ic_history, tuning):
            super().__init__()
            from gymnasium import spaces
            self.nesy_obs = nesy_obs.astype(np.float32)
            self.base_weights = base_weights.astype(np.float32)
            self.ic_history = ic_history.astype(np.float32)
            self.t = 0
            self.n_factors = len(base_weights)
            self.weights = base_weights.copy()
            self.n_steps = len(ic_history) - 10
            t = tuning or {}
            self.delta_scale = float(t.get("delta_scale", 0.5))
            self.temperature = float(t.get("temperature", 1.0))
            self.weight_clip = float(t.get("weight_clip", 0.6))
            obs_dim = len(nesy_obs) + self.n_factors
            self.observation_space = spaces.Box(
                low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float32)
            self.action_space = spaces.Box(
                low=-1.0, high=1.0, shape=(self.n_factors,), dtype=np.float32)

        def reset(self, *, seed=None, options=None):
            self.t = 0
            self.weights = self.base_weights.copy()
            return self._state(), {}

        def _state(self):
            return np.concatenate([self.nesy_obs, self.weights]).astype(np.float32)

        def step(self, action):
            action = np.asarray(action, dtype=np.float32)
            noise = np.random.randn(self.n_factors).astype(np.float32) * (self.temperature - 1.0) * 0.02
            delta = (np.tanh(action) + noise) * 0.05 * self.delta_scale
            new_w = self.weights + delta
            lo = max(0.01, 0.02 - (self.weight_clip - 0.3) * 0.05)
            new_w = np.clip(new_w, lo, self.weight_clip)
            new_w = new_w / new_w.sum()
            self.weights = new_w
            if self.t < len(self.ic_history):
                reward = float(np.dot(self.weights, self.ic_history[self.t])) * 10
            else:
                reward = 0.0
            self.t += 1
            done = self.t >= self.n_steps
            return self._state(), float(reward), bool(done), bool(False), {"weights": new_w.tolist()}

    env_c = NeSyTAEnv(nesy_obs, prior_w, ic, nesy_tuning)
    agent_c = PPO("MlpPolicy", env_c, n_steps=min(64, len(rets) - 10),
                  learning_rate=3e-4, n_epochs=n_epochs, verbose=0)
    agent_c.learn(total_timesteps=total_timesteps)

    # ---- 门控网络训练 ----
    _log("Training Gating Network...")
    gate_input = nesy_obs.copy()
    gating = GatingNetwork()

    # 用各子策略的最终权重评估组合效果
    obs_a, _ = env_a.reset()
    obs_b, _ = env_b.reset()
    obs_c, _ = env_c.reset()

    final_a = None
    final_b = None
    final_c = None
    n_steps = len(rets) - env_a.lookback
    for _ in range(n_steps):
        action_a, _ = agent_a.predict(obs_a, deterministic=True)
        obs_a, _, done_a, _, info_a = env_a.step(action_a)
        if done_a and "weights" in info_a:
            final_a = info_a["weights"]
        action_b, _ = agent_b.predict(obs_b, deterministic=True)
        obs_b, _, done_b, _, info_b = env_b.step(action_b)
        if done_b and "weights" in info_b:
            final_b = info_b["weights"]
        action_c, _ = agent_c.predict(obs_c, deterministic=True)
        obs_c, _, done_c, _, info_c = env_c.step(action_c)
        if done_c and "weights" in info_c:
            final_c = info_c["weights"]

    final_a = final_a or prior_w.tolist()
    final_b = final_b or prior_w.tolist()
    final_c = final_c or prior_w.tolist()

    # 门控权重: 用最终权重验证
    gate_weights = gating.predict(gate_input)
    combined_w = (gate_weights[0] * np.array(final_a) +
                  gate_weights[1] * np.array(final_b) +
                  gate_weights[2] * np.array(final_c))
    combined_w = combined_w / combined_w.sum()

    # 保存集成模型
    out_dir = os.path.join(DATA_DIR, "drl", day_dir, "ensemble")
    os.makedirs(out_dir, exist_ok=True)

    ensemble = MultiModalEnsemble(
        {"factor_ic": agent_a, "price_action": agent_b, "nesy_ta": agent_c},
        gating,
    )
    ensemble.save(out_dir)

    # 生成目标计划
    try:
        from drl_train import _build_target_plan
        final_weights = {k: float(v) for k, v in zip(SCORE_FACTORS, combined_w)}
        plan = _build_target_plan(day=day, day_dir=day_dir,
                                  final_weights=final_weights, top_n=MAX_STOCKS)
    except Exception as e:
        plan = {"ok": False, "error": str(e)}

    meta = {
        "ok": True,
        "day": day,
        "algorithm": "Multimodal_PPO",
        "total_timesteps": total_timesteps,
        "modalities": MODALITY_NAMES,
        "gate_weights": {k: float(v) for k, v in zip(MODALITY_NAMES, gate_weights)},
        "final_weights": {k: float(v) for k, v in zip(SCORE_FACTORS, combined_w)},
        "sub_weights": {
            "factor_ic": {k: float(v) for k, v in zip(SCORE_FACTORS, final_a)},
            "price_action": {k: float(v) for k, v in zip(SCORE_FACTORS, final_b)},
            "nesy_ta": {k: float(v) for k, v in zip(SCORE_FACTORS, final_c)},
        },
        "nesy_tuning": nesy_tuning,
        "target_plan": plan,
        "model_dir": out_dir,
        "n_dates": len(dates),
    }

    meta_path = os.path.join(out_dir, "train_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    _log(f"Multimodal training complete: gate_weights={gate_weights.tolist()}")
    return meta


# ============================================================
# 推理: 多模态集成预测
# ============================================================
def predict_multimodal(day_dir: str, deterministic: bool = True) -> dict:
    """用已训练的多模态集成模型预测."""
    out_dir = os.path.join(DATA_DIR, "drl", day_dir, "ensemble")
    if not os.path.exists(os.path.join(out_dir, "gating_network.pt")):
        return {"ok": False, "error": "Ensemble model not found"}

    ensemble = MultiModalEnsemble.load(out_dir)

    day = day_dir[:4] + "-" + day_dir[4:6] + "-" + day_dir[6:8]
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()

    from drl_train import _load_factor_state, _load_base_weights
    ic, rets, dates = _load_factor_state(day_dt, 60)
    if ic is None:
        return {"ok": False, "error": "数据不足"}

    base_w = _load_base_weights()

    # 构建各模态观测
    from drl_train import FactorWeightEnv
    env_a = FactorWeightEnv(ic, base_w, day=day_dt)
    obs_a, _ = env_a.reset()

    pa_obs = build_price_action_obs(day_dt)
    nesy_obs = build_nesy_ta_obs(day_dir)

    # 构造简单的 obs_b, obs_c (用当前权重填充)
    obs_b = np.concatenate([pa_obs, base_w]).astype(np.float32)
    obs_c = np.concatenate([nesy_obs, base_w]).astype(np.float32)

    gate_input = nesy_obs.copy()
    combined_action, gate_weights = ensemble.predict(
        gate_input, obs_a, obs_b, obs_c, deterministic=deterministic)

    combined_w = base_w + combined_action
    combined_w = np.clip(combined_w, 0.02, 0.6)
    combined_w = combined_w / combined_w.sum()

    return {
        "ok": True,
        "day": day,
        "gate_weights": {k: float(v) for k, v in zip(MODALITY_NAMES, gate_weights)},
        "final_weights": {k: float(v) for k, v in zip(SCORE_FACTORS, combined_w)},
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="多模态策略集成")
    ap.add_argument("--day", help="YYYY-MM-DD; default=latest")
    ap.add_argument("--train", action="store_true", help="训练模式")
    ap.add_argument("--predict", action="store_true", help="推理模式")
    ap.add_argument("--timesteps", type=int, default=400)
    args = ap.parse_args()

    d = args.day or (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    if args.train:
        result = run_multimodal_train(d, total_timesteps=args.timesteps)
    elif args.predict:
        day_dir = d.replace("-", "")
        result = predict_multimodal(day_dir)
    else:
        print("Use --train or --predict")
        result = {}

    print(json.dumps(result, ensure_ascii=False, indent=2))
