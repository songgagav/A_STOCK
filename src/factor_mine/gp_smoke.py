# -*- coding: utf-8 -*-
"""gp_mine 最小冒烟: 验证 gplearn fit 在 sklearn 1.7 补丁下可运行."""
import os
import sys
import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gp_sklearn_patch as _patch
_patch.apply()

import gp_mine as M  # noqa: E402

sub = M.load(frac=0.03, seed=3)
X = sub[M.FEATS].values.astype(float)
lo, hi = np.nanpercentile(sub[M.YCOL], [1, 99])
y = np.clip(sub[M.YCOL].values.astype(float), lo, hi)
months = sub["ym"].values
print("rows:", len(sub), "cols:", X.shape[1], "months:", np.unique(months).size)

from gplearn.genetic import SymbolicRegressor  # noqa: E402
from gplearn.fitness import make_fitness  # noqa: E402

months = sub["ym"].values

def metric(y_t, y_p, w):
    return M._rankic_impl(y_t, y_p, months)

fit = make_fitness(function=metric, greater_is_better=True, wrap=False)
est = SymbolicRegressor(
    population_size=60, generations=2, tournament_size=5,
    function_set=["add", "sub", "mul", "div", "neg", "inv", "sqrt", "log", "abs"],
    parsimony_coefficient=0.01, p_crossover=0.7, p_subtree_mutation=0.1,
    p_hoist_mutation=0.05, p_point_mutation=0.1,
    metric=fit, random_state=11, verbose=1, n_jobs=1)
est.fit(X, y)
print("smoke OK; n_features_in_:", getattr(est, "n_features_in_", None))
pool = list(est._programs[-1])
pool.sort(key=lambda p: p.raw_fitness_, reverse=True)
print("best:", round(float(pool[0].raw_fitness_), 5), str(pool[0])[:140])
