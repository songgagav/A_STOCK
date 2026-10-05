import pandas as pd

from history_equivalence import compare_history_frames


def test_compare_history_frames_accepts_equivalent_index_and_column_views():
    left = pd.DataFrame(
        {"equity": [100.0, 101.0]},
        index=pd.Index(["20261001", "20261002"], name="day"),
    )
    right = pd.DataFrame(
        {"day": ["20261002", "20261001"], "equity": [101.0, 100.0]}
    )

    result = compare_history_frames(
        left, right, columns=("day", "equity"), sort_by=("day",)
    )

    assert result == {
        "status": "equivalent",
        "left_rows": 2,
        "right_rows": 2,
    }


def test_compare_history_frames_reports_mismatch_without_throwing():
    left = pd.DataFrame({"day": ["20261001"], "equity": [100.0]})
    right = pd.DataFrame({"day": ["20261001"], "equity": [99.0]})

    result = compare_history_frames(left, right, columns=("day", "equity"))

    assert result["status"] == "mismatch"
    assert result["left_rows"] == 1
    assert result["right_rows"] == 1
    assert "equity" in result["reason"]


def test_compare_history_frames_keeps_unavailable_distinct_from_empty():
    result = compare_history_frames(
        None,
        pd.DataFrame(columns=["day", "equity"]),
        columns=("day", "equity"),
    )

    assert result["status"] == "unavailable"
    assert result["left_rows"] is None
    assert result["right_rows"] == 0
