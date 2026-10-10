from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "scripts/research")

from alpha_shadow_batch import load_factor_config, select_common_days  # noqa: E402


def test_load_factor_config_requires_all_factor_directions(tmp_path: Path):
    path = tmp_path / "fusion.json"
    path.write_text(
        json.dumps(
            {
                "factor_weights": {"pb_inv": 1.0},
                "directions": {"pb_inv": 1},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="roe_yy_chg"):
        load_factor_config(path)


def test_select_common_days_uses_only_complete_pit_pairs(tmp_path: Path):
    xsec = tmp_path / "xsec"
    fusion = tmp_path / "fusion_x"
    xsec.mkdir()
    fusion.mkdir()
    (xsec / "2026-09-01.parquet").write_bytes(b"x")
    (xsec / "2026-09-02.parquet").write_bytes(b"x")
    (fusion / "2026-09-02.parquet").write_bytes(b"x")
    (fusion / "2026-09-03.parquet").write_bytes(b"x")

    assert select_common_days(xsec, fusion) == ["2026-09-02"]
