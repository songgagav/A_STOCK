# -*- coding: utf-8 -*-
"""gplearn 0.4.2 × sklearn>=1.7 兼容补丁。
sklearn 1.7 移除了 BaseEstimator._validate_data / _check_n_features，
gplearn 0.4.2(2021) 内部仍依赖旧私有 API。本模块按旧语义注入最小
兼容实现(仅覆盖 gplearn 实际使用路径)，不动全局 sklearn。
用法: 在 import gplearn 之前 `import gp_sklearn_patch`。
"""
import numpy as np

_ORIG = None


def _validate_data(self, X="no_validation", y="no_validation",
                   reset=True, validate_separately=False, **check_params):
    from sklearn.utils.validation import check_X_y, check_array

    no_X = isinstance(X, str) and X == "no_validation"
    no_y = isinstance(y, str) and y == "no_validation"
    if no_X and no_y:
        raise ValueError("Need at least one of X or y")
    check_params.pop("reset", None)
    check_params.pop("validate_separately", None)
    if "dtype" not in check_params:
        check_params.setdefault("dtype", "numeric")
    if not no_X and no_y:
        X = check_array(X, **check_params)
        self.n_features_in_ = X.shape[1]
        return X
    if no_X and not no_y:
        y = check_array(y, ensure_2d=False, dtype=check_params.get("dtype", "numeric"),
                        force_all_finite=check_params.get("force_all_finite", True))
        return y
    X, y = check_X_y(X, y, **check_params)
    self.n_features_in_ = X.shape[1]
    return X, y


def apply():
    global _ORIG
    from sklearn.base import BaseEstimator
    if hasattr(BaseEstimator, "_validate_data"):
        return False  # 原生存在，无需打补丁
    _ORIG = _validate_data
    BaseEstimator._validate_data = _validate_data
    return True


if __name__ == "__main__":
    print("patch applied:", apply())
