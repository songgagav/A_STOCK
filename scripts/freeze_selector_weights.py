#!/usr/bin/env python
"""Create a content-addressed snapshot of the current effective weights.

Run only after explicitly setting SELECTOR_WEIGHT_MODE=adaptive. The command
does not change that setting or any service/runtime configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from selector_weight_policy import create_frozen_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT),
        help="repository whose current effective selector weights are captured",
    )
    args = parser.parse_args()
    result = create_frozen_snapshot(args.repo_root)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
