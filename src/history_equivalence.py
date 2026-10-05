"""Pure comparison helpers for dormant dual-read migration checks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd


def compare_history_frames(
    left: pd.DataFrame | None,
    right: pd.DataFrame | None,
    *,
    columns: Sequence[str],
    sort_by: Sequence[str] = (),
) -> dict[str, Any]:
    """Compare two reader results without treating unavailable as empty.

    This is intentionally a pure, side-effect-free helper.  Production
    consumers can use it during a shadow period, while tests can exercise the
    equivalence contract without importing ArcticDB.
    """
    left_rows = None if left is None else len(left)
    right_rows = None if right is None else len(right)
    if left is None or right is None:
        return {
            "status": "unavailable",
            "left_rows": left_rows,
            "right_rows": right_rows,
        }
    wanted = list(columns)
    missing_left = [name for name in wanted if name not in left.columns and name != left.index.name]
    missing_right = [name for name in wanted if name not in right.columns and name != right.index.name]
    if missing_left or missing_right:
        return {
            "status": "mismatch",
            "left_rows": left_rows,
            "right_rows": right_rows,
            "reason": f"missing columns: left={missing_left}, right={missing_right}",
        }

    def materialize(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.reset_index() if frame.index.name and frame.index.name not in frame.columns else frame.copy()
        out = out.loc[:, wanted]
        if sort_by:
            out = out.sort_values(list(sort_by), kind="stable")
        return out.reset_index(drop=True)

    try:
        pd.testing.assert_frame_equal(
            materialize(left),
            materialize(right),
            check_dtype=False,
            check_like=False,
            check_exact=False,
            rtol=1e-9,
            atol=1e-12,
        )
    except AssertionError as exc:
        return {
            "status": "mismatch",
            "left_rows": left_rows,
            "right_rows": right_rows,
            "reason": str(exc).splitlines()[0],
        }
    return {"status": "equivalent", "left_rows": left_rows, "right_rows": right_rows}
