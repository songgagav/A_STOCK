# ============================================================
# neural_ta.py -- 神经符号化趋势分析 (NeSy-TA) 的神经层
#
# 定位: 轻量 MLP, 以 Symbolic-TA 粗粒度特征为输入,
#       输出 PPO 策略网络的调优参数 (delta_scale, temperature, weight_clip).
#
# 输入:  12维 symbolic_ta 特征 + 1维 market_panel 情绪分 = 13维
# 输出:  3维调优参数 (delta_scale, temperature, weight_clip)
#
# 架构:  [13] -> Dense(32) -> ReLU -> Dense(16) -> ReLU -> Dense(3) -> Sigmoid
#        约 5000 参数, 初始化编码启发式规则.
#
# 调优参数含义:
#   delta_scale  : 动作幅度缩放 [0.2, 1.5] (低=保守, 高=激进)
#   temperature  : 动作噪声温度 [0.5, 2.5] (低=确定, 高=探索)
#   weight_clip  : 权重裁剪上限 [0.3, 0.7] (低=集中, 高=分散)
# ============================================================

import os
import sys
import json

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR

# ============================================================
# 特征维度定义
# ============================================================
# symbolic_ta 12维特征
SYM_FEATURE_KEYS = [
    'f_ma_score', 'f_ma_bull_ratio', 'f_ma_bear_ratio',
    'f_sr_position', 'f_vp_score', 'f_vol_percentile',
    'f_composite', 'f_ma_consensus', 'f_bull_strength',
    'f_bear_strength', 'f_trend_persistence', 'f_regime_stability',
]
# market_panel 附加特征 (1维: sentiment_score)
PANEL_KEYS = ['sentiment_score']

INPUT_DIM = len(SYM_FEATURE_KEYS) + len(PANEL_KEYS)  # 13
OUTPUT_DIM = 3  # delta_scale, temperature, weight_clip

# 调优参数范围
DELTA_SCALE_RANGE = (0.2, 1.5)    # 保守 ~ 激进
TEMPERATURE_RANGE = (0.5, 2.5)    # 确定 ~ 探索
WEIGHT_CLIP_RANGE = (0.3, 0.7)    # 集中 ~ 分散

# ============================================================
# 启发式初始化 (让模型在训练前就有合理行为)
# ============================================================
# 规则: composite_score > 0.25 -> 激进; < -0.25 -> 保守; 中间 -> 震荡
# 这些规则编码了"趋势市放大动作, 震荡市或下跌市收缩动作"的直觉


def _heuristic_tuning(features: dict) -> np.ndarray:
    """纯规则驱动的调优参数, 作为 Neural-TA 的初始化基线.
    返回 [delta_scale, temperature, weight_clip].
    """
    comp = features.get('f_composite', 0.0)
    vol_pct = features.get('f_vol_percentile', 0.5)
    consensus = features.get('f_ma_consensus', 0.5)
    ma_score = features.get('f_ma_score', 0.0)

    # delta_scale: 趋势越强越激进
    if comp > 0.3:
        delta = 0.8 + abs(comp) * 0.7  # 0.8 ~ 1.5
    elif comp < -0.3:
        delta = 0.2 + abs(comp) * 0.3  # 0.2 ~ 0.5
    else:
        delta = 0.35 + abs(comp) * 0.5  # 0.35 ~ 0.6

    # temperature: 高波动 + 低共识 -> 高温度 (多探索)
    if vol_pct > 0.7 and consensus < 0.5:
        temp = 1.5 + vol_pct * 0.5  # 1.5 ~ 2.0
    elif consensus > 0.8:
        temp = 0.5 + (1.0 - consensus) * 1.0  # 0.5 ~ 0.7
    else:
        temp = 0.8 + (1.0 - consensus) * 1.0  # 0.8 ~ 1.3

    # weight_clip: 趋势强 + 共识高 -> 集中 (clip低), 震荡 -> 分散 (clip高)
    trend_strength = abs(ma_score) * consensus
    wc = 0.55 - trend_strength * 0.25  # 0.3 ~ 0.55
    wc = max(0.3, min(0.7, wc))

    return np.array([delta, temp, wc], dtype=np.float32)


# ============================================================
# 特征向量构建
# ============================================================
def build_input_vector(sym_result: dict, panel_result: dict | None = None) -> np.ndarray:
    """从 symbolic_ta 和 market_panel 结果构建 13 维输入向量."""
    feats = sym_result.get('features', {})
    vec = np.zeros(INPUT_DIM, dtype=np.float32)
    for i, key in enumerate(SYM_FEATURE_KEYS):
        vec[i] = float(feats.get(key, 0.0))
    if panel_result and panel_result.get('ok'):
        vec[len(SYM_FEATURE_KEYS)] = float(panel_result.get('sentiment_score', 50.0)) / 100.0
    else:
        vec[len(SYM_FEATURE_KEYS)] = 0.5
    return vec


def scale_output(raw: np.ndarray) -> np.ndarray:
    """将 sigmoid 输出 [0,1] 映射到各参数的实际范围.
    raw: shape (3,) 或 (batch, 3)
    """
    out = np.zeros_like(raw)
    out[..., 0] = DELTA_SCALE_RANGE[0] + raw[..., 0] * (DELTA_SCALE_RANGE[1] - DELTA_SCALE_RANGE[0])
    out[..., 1] = TEMPERATURE_RANGE[0] + raw[..., 1] * (TEMPERATURE_RANGE[1] - TEMPERATURE_RANGE[0])
    out[..., 2] = WEIGHT_CLIP_RANGE[0] + raw[..., 2] * (WEIGHT_CLIP_RANGE[1] - WEIGHT_CLIP_RANGE[0])
    return out


# ============================================================
# Neural-TA Model (PyTorch)
# ============================================================
class NeuralTAModel:
    """轻量 MLP, 输入 Symbolic-TA + Market Panel 特征, 输出调优参数.

    可作为独立模块工作在两种模式:
    - heuristic 模式: 纯规则驱动 (不需要 torch)
    - model 模式: 神经网络推理 (需要 torch)
    """

    def __init__(self):
        self._torch = None
        self._model = None
        try:
            import torch
            self._torch = torch
            self._model = self._build_network()
        except ImportError:
            pass

    def _build_network(self):
        import torch.nn as nn
        return nn.Sequential(
            nn.Linear(INPUT_DIM, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, OUTPUT_DIM),
            nn.Sigmoid(),
        )

    def _init_heuristic_weights(self):
        """用启发式规则初始化网络权重, 使初始输出接近 _heuristic_tuning."""
        if self._model is None:
            return
        import torch
        # 生成若干代表性样本, 拟合线性近似
        samples = []
        targets = []
        for comp in [-0.5, -0.2, 0.0, 0.2, 0.5]:
            for vol in [0.2, 0.5, 0.8]:
                for cons in [0.3, 0.6, 0.9]:
                    feats = {k: 0.0 for k in SYM_FEATURE_KEYS}
                    feats['f_composite'] = comp
                    feats['f_vol_percentile'] = vol
                    feats['f_ma_consensus'] = cons
                    feats['f_ma_score'] = comp
                    feats['f_bull_strength'] = max(0, comp) * cons
                    feats['f_bear_strength'] = max(0, -comp) * cons
                    feats['f_trend_persistence'] = comp * (1.0 - vol)
                    feats['f_regime_stability'] = cons
                    feats['f_ma_bull_ratio'] = max(0, comp) * cons
                    feats['f_ma_bear_ratio'] = max(0, -comp) * cons
                    feats['f_sr_position'] = 0.5 + comp * 0.3
                    feats['f_vp_score'] = comp * 0.3
                    vec = build_input_vector({'features': feats})
                    # 缩放到 [0,1] 作为 target
                    tgt = _heuristic_tuning(feats)
                    tgt_scaled = np.array([
                        (tgt[0] - DELTA_SCALE_RANGE[0]) / (DELTA_SCALE_RANGE[1] - DELTA_SCALE_RANGE[0]),
                        (tgt[1] - TEMPERATURE_RANGE[0]) / (TEMPERATURE_RANGE[1] - TEMPERATURE_RANGE[0]),
                        (tgt[2] - WEIGHT_CLIP_RANGE[0]) / (WEIGHT_CLIP_RANGE[1] - WEIGHT_CLIP_RANGE[0]),
                    ], dtype=np.float32)
                    samples.append(vec)
                    targets.append(tgt_scaled)

        X = torch.tensor(np.array(samples), dtype=torch.float32)
        y = torch.tensor(np.array(targets), dtype=torch.float32)

        optimizer = torch.optim.Adam(self._model.parameters(), lr=0.01)
        loss_fn = torch.nn.MSELoss()
        for _ in range(500):
            optimizer.zero_grad()
            pred = self._model(X)
            loss = loss_fn(pred, y)
            loss.backward()
            optimizer.step()

    def predict(self, input_vec: np.ndarray, use_heuristic: bool = False) -> dict:
        """返回调优参数 dict.
        use_heuristic=True 时跳过神经网络, 直接用规则.
        """
        if use_heuristic or self._model is None:
            feats = {}
            for i, key in enumerate(SYM_FEATURE_KEYS):
                feats[key] = float(input_vec[i])
            raw = _heuristic_tuning(feats)
            return {
                'delta_scale': round(float(raw[0]), 4),
                'temperature': round(float(raw[1]), 4),
                'weight_clip': round(float(raw[2]), 4),
                'mode': 'heuristic',
            }

        import torch
        x = torch.tensor(input_vec.reshape(1, -1), dtype=torch.float32)
        with torch.no_grad():
            raw = self._model(x).numpy().flatten()
        scaled = scale_output(raw)
        return {
            'delta_scale': round(float(scaled[0]), 4),
            'temperature': round(float(scaled[1]), 4),
            'weight_clip': round(float(scaled[2]), 4),
            'mode': 'model',
            'raw_output': raw.tolist(),
        }

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
# 一站式接口: 从 symbolic_ta + market_panel -> tuning parameters
# ============================================================
_NEURAL_TA_INSTANCE = None


def get_neural_ta() -> NeuralTAModel:
    global _NEURAL_TA_INSTANCE
    if _NEURAL_TA_INSTANCE is None:
        _NEURAL_TA_INSTANCE = NeuralTAModel()
        _NEURAL_TA_INSTANCE._init_heuristic_weights()
        # 尝试加载已训练的模型
        model_path = os.path.join(DATA_DIR, 'neural_ta_model.pt')
        if os.path.exists(model_path):
            _NEURAL_TA_INSTANCE.load(model_path)
    return _NEURAL_TA_INSTANCE


def compute_tuning(day_dir: str, use_heuristic: bool = False) -> dict:
    """一站式: 读 symbolic_ta.json + market_panel -> 输出调优参数."""
    from symbolic_ta import load_symbolic_state
    from market_panel import compute_market

    sym = load_symbolic_state(day_dir)
    if not sym or not sym.get('ok'):
        return {'delta_scale': 0.5, 'temperature': 1.0, 'weight_clip': 0.5,
                'mode': 'fallback', 'error': 'symbolic_ta unavailable'}

    day = sym['day']
    try:
        panel = compute_market(day)
    except Exception:
        panel = None

    vec = build_input_vector(sym, panel)
    nta = get_neural_ta()
    result = nta.predict(vec, use_heuristic=use_heuristic)
    result['day'] = day
    result['symbolic_state'] = sym.get('market_state')
    result['symbolic_intensity'] = sym.get('intensity')
    result['sentiment_score'] = panel.get('sentiment_score') if panel and panel.get('ok') else None
    return result


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Neural-TA Tuning Parameter Predictor')
    ap.add_argument('day', nargs='?', help='YYYYMMDD; default=latest')
    ap.add_argument('--heuristic', action='store_true', help='Use heuristic mode')
    ap.add_argument('--train', action='store_true', help='Train heuristic init')
    args = ap.parse_args()

    if args.train:
        nta = get_neural_ta()
        path = os.path.join(DATA_DIR, 'neural_ta_model.pt')
        nta.save(path)
        print(f'Model saved to {path}')
    else:
        if args.day:
            day_dir = args.day
        else:
            import datetime as dt
            day_dir = (dt.date.today() - dt.timedelta(days=1)).strftime('%Y%m%d')
        result = compute_tuning(day_dir, use_heuristic=args.heuristic)
        print(json.dumps(result, ensure_ascii=False, indent=2))
