# -*- coding: utf-8 -*-
"""CAFPO: 条件自编码因子投资 (Conditional AutoEncoder Factor Investing).

参考: 2025 年 CAFPO 框架把股票收益压缩为"公司特征条件下"的潜在因子,
样本外 24.6% 复合收益, 夏普 0.94.

落地方式 (长期方向): 用自编码器从海量原始因子中提取低维潜在因子,
作为"潜在风险因子"供上层 PPO/风控消费. 本模块提供纯 NumPy 实现:

  - ConditionalAutoEncoder: 输入 = 原始因子 x + 公司特征 c,
    Encoder 压缩出潜在因子 z, Decoder 在条件 c 下重建 x.
  - 训练: Adam 全批量梯度下降, 重建 MSE + L2 正则, 内置训练/验证切分
    (样本外评估).
  - 输出: 潜在因子矩阵 + 重建解释度 + 潜在因子对未来收益的 IC.

依赖: 仅 numpy, 不引入 torch/tensorflow, 便于离线验证与轻量部署.
"""

from __future__ import annotations

import numpy as np


def _seed_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _standardize_fit(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按列标准化并记录均值/标准差."""
    X = np.asarray(X, dtype=np.float64)
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-8
    return (X - mu) / sd, mu, sd


class ConditionalAutoEncoder:
    """条件自编码器: x ≈ Decoder( Encoder([x;c]), c ).

    结构:
      encoder: z = tanh( [x;c] @ We^T + be ),  z ∈ R^{latent}
      decoder: x_hat = [z;c] @ Wd^T + bd

    Parameters
    ----------
    n_features : int      原始因子维度
    n_conditions : int    公司特征维度
    latent_dim : int      潜在因子维度 (默认 4)
    reg : float           L2 正则系数
    seed : int            随机种子
    """

    def __init__(
        self,
        n_features: int,
        n_conditions: int,
        latent_dim: int = 4,
        reg: float = 1e-4,
        seed: int = 0,
    ):
        self.n_features = int(n_features)
        self.n_conditions = int(n_conditions)
        self.latent_dim = int(latent_dim)
        self.reg = float(reg)
        self.seed = int(seed)

        rng = _seed_rng(seed)
        # 编码器: (latent, n_features+n_conditions) + bias
        self.We = rng.normal(0.0, 0.1, (self.latent_dim, self.n_features + self.n_conditions))
        self.be = np.zeros(self.latent_dim)
        # 解码器: (n_features, latent+n_conditions) + bias
        self.Wd = rng.normal(0.0, 0.1, (self.n_features, self.latent_dim + self.n_conditions))
        self.bd = np.zeros(self.n_features)

        # 训练统计
        self._x_mu = np.zeros(self.n_features)
        self._x_sd = np.ones(self.n_features)
        self._c_mu = np.zeros(self.n_conditions)
        self._c_sd = np.ones(self.n_conditions)
        self._fitted = False
        self.train_losses: list[float] = []
        self.val_losses: list[float] = []
        self._n_params = (
            self.We.size + self.be.size + self.Wd.size + self.bd.size
        )

    # ---------- 前向 ----------
    def _forward(self, X: np.ndarray, C: np.ndarray):
        """X:(T,n)  C:(T,m) -> z:(T,l), X_hat:(T,n)."""
        U = np.concatenate([X, C], axis=1)         # (T, n+m)
        a = U @ self.We.T + self.be                # (T, l)
        z = np.tanh(a)
        V = np.concatenate([z, C], axis=1)         # (T, l+m)
        X_hat = V @ self.Wd.T + self.bd            # (T, n)
        return z, X_hat, U, V

    # ---------- 训练 ----------
    def fit(
        self,
        features: np.ndarray,
        conditions: np.ndarray,
        steps: int = 400,
        lr: float = 0.01,
        holdout_frac: float = 0.2,
        seed: int | None = None,
        verbose: bool = False,
    ) -> "ConditionalAutoEncoder":
        """Adam 全批量训练.

        Args:
            features: (T, n_features) 原始因子矩阵 (含 NaN 行会被剔除).
            conditions: (T, n_conditions) 公司特征矩阵.
            steps: 迭代步数.
            lr: 学习率.
            holdout_frac: 样本外验证比例 (默认 20%, 尾部按时间切分).
            seed: 数据洗牌种子 (None = 训练集中不打乱, 保时间顺序).
        """
        X = np.asarray(features, dtype=np.float64)
        C = np.asarray(conditions, dtype=np.float64)
        if X.ndim != 2 or C.ndim != 2 or len(X) != len(C):
            raise ValueError("features/conditions 须为同长度的二维矩阵")
        if X.shape[1] != self.n_features or C.shape[1] != self.n_conditions:
            raise ValueError("维度不匹配: 输入须 (T,n_features)/(T,n_conditions)")

        # 剔除含 NaN 的行
        good = np.isfinite(X).all(axis=1) & np.isfinite(C).all(axis=1)
        X, C = X[good], C[good]
        if len(X) < max(20, self.n_features * 3 + 5):
            raise ValueError(f"有效样本不足 ({len(X)})")

        # 时间序切分: 尾部做样本外
        n_val = max(1, int(round(len(X) * holdout_frac)))
        X_tr, X_va = X[:-n_val], X[-n_val:]
        C_tr, C_va = C[:-n_val], C[-n_val:]

        # 标准化 (只在训练集上拟合 scaler)
        Xs_tr, self._x_mu, self._x_sd = _standardize_fit(X_tr)
        Cs_tr, self._c_mu, self._c_sd = _standardize_fit(C_tr)
        Xs_va = (X_va - self._x_mu) / self._x_sd
        Cs_va = (C_va - self._c_mu) / self._c_sd

        # Adam 状态
        ms = {k: np.zeros_like(v) for k, v in self._params().items()}
        vs = {k: np.zeros_like(v) for k, v in self._params().items()}
        beta1, beta2, eps = 0.9, 0.999, 1e-8

        self.train_losses, self.val_losses = [], []
        for step in range(1, steps + 1):
            loss = self._loss_and_grad(Xs_tr, Cs_tr, ms, vs, lr, beta1, beta2, eps)
            self.train_losses.append(float(loss))
            if step % max(1, steps // 10) == 0 or step == 1:
                # 验证损失在标准化空间内计算 (训练中无需经过 predict 的 fitted 门禁)
                _, xh_va, _, _ = self._forward(Xs_va, Cs_va)
                vloss = float(np.mean((xh_va - Xs_va) ** 2))
                self.val_losses.append(vloss)
                if verbose:
                    print(f"[CAFPO] step {step}/{steps} train={loss:.6f} val={vloss:.6f}")

        self._fitted = True
        return self

    def _params(self) -> dict[str, np.ndarray]:
        return {"We": self.We, "be": self.be, "Wd": self.Wd, "bd": self.bd}

    def _loss_and_grad(
        self, X: np.ndarray, C: np.ndarray,
        ms: dict, vs: dict, lr: float,
        beta1: float, beta2: float, eps: float,
    ) -> float:
        T = len(X)
        z, X_hat, U, V = self._forward(X, C)
        delta = (X_hat - X) / T                       # (T,n)

        loss = float(0.5 * np.sum((X_hat - X) ** 2) / T)
        reg = self.reg
        loss += 0.5 * reg * (float(np.sum(self.We ** 2)) + float(np.sum(self.Wd ** 2)))

        # 解码器梯度
        gWdbig = delta.T @ V + reg * self.Wd          # (n, l+m)
        gbd = delta.sum(axis=0)
        # 经过 z 的反传
        dz = delta @ self.Wd[:, :self.latent_dim]     # (T,l)
        gz = dz * (1.0 - z ** 2)                      # tanh'
        gU = gz.T @ U + reg * self.We                 # (l, n+m)
        gbe = gz.sum(axis=0)

        grads = {"We": gU, "be": gbe, "Wd": gWdbig, "bd": gbd}
        for k, g in grads.items():
            ms[k] = beta1 * ms[k] + (1 - beta1) * g
            vs[k] = beta2 * vs[k] + (1 - beta2) * (g ** 2)
            m_hat = ms[k] / (1 - beta1 ** (len(self.train_losses) + 1))
            v_hat = vs[k] / (1 - beta2 ** (len(self.train_losses) + 1))
            self._params()[k] -= lr * m_hat / (np.sqrt(v_hat) + eps)
        return loss

    # ---------- 推理 ----------
    def _standardize(self, X: np.ndarray, C: np.ndarray):
        if not self._fitted:
            raise RuntimeError("模型尚未 fit, 请先调用 fit()")
        Xs = (np.asarray(X, dtype=np.float64) - self._x_mu) / self._x_sd
        Cs = (np.asarray(C, dtype=np.float64) - self._c_mu) / self._c_sd
        return Xs, Cs

    def encode(self, features: np.ndarray, conditions: np.ndarray) -> np.ndarray:
        """输出潜在因子 z (T, latent_dim)."""
        Xs, Cs = self._standardize(features, conditions)
        z, _, _, _ = self._forward(Xs, Cs)
        return z

    def predict(self, features: np.ndarray, conditions: np.ndarray) -> np.ndarray:
        """重建原始因子 (返回原始尺度)."""
        Xs, Cs = self._standardize(features, conditions)
        _, xh_std, _, _ = self._forward(Xs, Cs)
        return xh_std * self._x_sd + self._x_mu

    def reconstruct(self, features: np.ndarray, conditions: np.ndarray) -> np.ndarray:
        """别名: 重建原始因子 (与 predict 一致)."""
        return self.predict(features, conditions)

    def reconstruction_r2(self, features: np.ndarray, conditions: np.ndarray) -> float:
        """重建解释度 R² = 1 - SS_res / SS_tot (1=完全重建)."""
        X = np.asarray(features, dtype=np.float64)
        Xh = self.predict(features, conditions)
        ss_res = float(np.sum((X - Xh) ** 2))
        ss_tot = float(np.sum((X - X.mean(axis=0)) ** 2))
        return 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0


# ===================================================================
# 便捷函数: 端到端提取潜在风险因子
# ===================================================================
def extract_latent_factors(
    features: np.ndarray,
    conditions: np.ndarray,
    latent_dim: int = 4,
    steps: int = 400,
    lr: float = 0.01,
    holdout_frac: float = 0.2,
    seed: int = 0,
) -> dict:
    """从海量因子提取潜在因子 (CAFPO 落地入口).

    Returns:
        dict: {
            "ok": True, "model": ConditionalAutoEncoder,
            "latent_factors": ndarray (T, latent_dim),
            "recon_r2": float,          # 训练集重建解释度
            "val_recon_r2": float,      # 样本外重建解释度
            "n_params": int,
        }
    """
    features = np.asarray(features, dtype=np.float64)
    conditions = np.asarray(conditions, dtype=np.float64)
    try:
        model = ConditionalAutoEncoder(
            features.shape[1], conditions.shape[1],
            latent_dim=latent_dim, seed=seed,
        )
        # 切分用于计算样本外 R²
        n_val = max(1, int(round(len(features) * holdout_frac)))
        model.fit(features, conditions, steps=steps, lr=lr,
                  holdout_frac=holdout_frac, seed=seed)
        latent = model.encode(features, conditions)
        return {
            "ok": True,
            "model": model,
            "latent_factors": latent,
            "recon_r2": round(float(model.reconstruction_r2(features, conditions)), 4),
            "val_recon_r2": round(float(model.reconstruction_r2(
                features[-n_val:], conditions[-n_val:])), 4),
            "n_params": model._n_params,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def latent_rank_ic(latent: np.ndarray, forward_returns: np.ndarray) -> dict:
    """潜在因子对未来收益的截面 RankIC.

    Args:
        latent: (T, latent_dim) 潜在因子.
        forward_returns: (T,) 未来收益.

    Returns:
        dict: {"mean_ic": float, "per_factor_ic": list[float]}.
    """
    L = np.asarray(latent, dtype=np.float64)
    y = np.asarray(forward_returns, dtype=np.float64)
    ics = []
    for j in range(L.shape[1]):
        col = L[:, j]
        m = np.isfinite(col) & np.isfinite(y)
        if m.sum() < 5 or col[m].std() < 1e-12 or y[m].std() < 1e-12:
            ics.append(0.0)
            continue
        c = np.corrcoef(
            np.argsort(np.argsort(col[m])),
            np.argsort(np.argsort(y[m])),
        )[0, 1]
        ics.append(float(c) if np.isfinite(c) else 0.0)
    return {
        "mean_ic": round(float(np.mean(ics)), 4) if ics else 0.0,
        "per_factor_ic": [round(v, 4) for v in ics],
    }
