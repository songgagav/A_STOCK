#!/usr/bin/env python
"""Verify and create one immutable selector-weight-bound observation epoch.

This command writes only the content-addressed epoch record. It does not edit
service configuration, select an active epoch, deploy, or start observation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from observation_epoch import freeze_observation_epoch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT),
        help="repository receiving data/evolution/epochs/<epoch_id>.json",
    )
    parser.add_argument(
        "--epoch-file", required=True,
        help="JSON file containing the already-built schema-v2 observation epoch",
    )
    args = parser.parse_args()
    epoch = json.loads(Path(args.epoch_file).read_text(encoding="utf-8"))
    path = freeze_observation_epoch(args.repo_root, epoch)
    print(json.dumps({"status": "frozen", "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
