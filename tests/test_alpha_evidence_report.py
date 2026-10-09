from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from alpha_evidence_report import build_report  # noqa: E402


def test_build_report_marks_recent_unfinished_window_pending(tmp_path: Path):
    ic = {
        "summary": {
            str(h): {"mean": 0.04, "pos": 8, "n": 12}
            for h in (1, 5, 10, 20, 60, 120)
        }
    }
    windows = [
        {
            "day": "2025-06-30",
            "ok": True,
            "window_mode": "forward",
            "fallback": False,
            "stats": {
                "start_date": "2025-06-30",
                "end_date": "2025-12-22",
                "total_return": 1.0,
                "sharpe_ratio": 1.0,
                "max_ddpercent": -1.0,
            },
        }
        for _ in range(10)
    ]
    windows.append(
        {
            "day": "2026-09-04",
            "ok": False,
            "window_mode": "forward",
            "fallback": False,
            "stats": {},
        }
    )
    attribution = [{"win_rate": 1.0} for _ in range(5)]
    paths = {}
    for name, payload in {
        "ic_term_structure": ic,
        "forward_windows": windows,
        "attribution": attribution,
    }.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path

    report = build_report(
        paths,
        as_of_date=date(2026, 10, 6),
        forward_horizon_days=120,
    )

    assert report["checks"]["forward_windows"]["pending_maturity_count"] == 1
    assert report["checks"]["forward_windows"]["excluded_count"] == 0
