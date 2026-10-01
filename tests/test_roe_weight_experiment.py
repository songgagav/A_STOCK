# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from scripts import roe_weight_experiment as R  # noqa: E402


def _rows():
    values = {
        "current_1.0618": [1.0, -1.0],
        "rank3_0.7300": [2.0, -0.5],
        "half_0.5309": [0.5, -2.0],
        "ablation_0": [0.0, 0.0],
    }
    rows = []
    for i in range(2):
        variants = {name: {"equal": vals[i], "weighted": vals[i],
                           "overlap_with_current": 10 if name == "current_1.0618" else 8}
                    for name, vals in values.items()}
        rows.append({"variants": variants})
    return rows


def test_summary_is_paired_against_current():
    s = R.summarize(_rows())
    x = s["rank3_0.7300"]["weighted"]
    assert x["paired_delta_vs_current_mean_pp"] == 0.75
    assert x["windows_better_than_current"] == 2
    assert s["rank3_0.7300"]["mean_top10_overlap_with_current"] == 8


def test_grid_keeps_direction_out_of_experiment():
    assert R.VARIANTS["current_1.0618"] == 1.0618
    assert R.VARIANTS["ablation_0"] == 0.0
    src = open(R.__file__, encoding="utf-8").read()
    assert '"direction_roe_yy_chg": 1' in src
    assert '"threshold_applied": False' in src
    assert '"decision": None' in src
