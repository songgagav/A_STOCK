"""汇总既有 Alpha 研究产物，不运行回测、不改生产数据。

示例:
    python scripts/research/alpha_evidence_report.py
    python scripts/research/alpha_evidence_report.py --output reports/alpha.json --strict

``--strict`` 只把 ``evidence_ready`` 视为成功；缺数据、结构错误或弱证据
都会返回非零，但不会把它们改写成通过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from alpha_evidence import evaluate_alpha_evidence  # noqa: E402


DEFAULT_INPUTS = {
    "ic_term_structure": ROOT / "data" / "ic_term_structure.json",
    "forward_windows": ROOT / "data" / "vnpy_backtest_nonoverlap_fwd_results.json",
    "attribution": ROOT / "data" / "attribution_b1.json",
}


def _read_json(path: Path) -> tuple[Any | None, dict[str, Any]]:
    """读取一个研究产物，并保留缺失/读取错误的审计信息。"""

    if not path.exists():
        return None, {"path": str(path), "exists": False, "sha256": None}
    try:
        raw = path.read_bytes()
        return json.loads(raw.decode("utf-8")), {
            "path": str(path),
            "exists": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"_load_error": f"{type(exc).__name__}: {exc}"}, {
            "path": str(path),
            "exists": True,
            "sha256": None,
            "load_error": f"{type(exc).__name__}: {exc}",
        }


def build_report(paths: dict[str, Path] | None = None) -> dict[str, Any]:
    paths = paths or DEFAULT_INPUTS
    ic, ic_meta = _read_json(paths["ic_term_structure"])
    windows, windows_meta = _read_json(paths["forward_windows"])
    attribution, attribution_meta = _read_json(paths["attribution"])
    report = evaluate_alpha_evidence(ic, windows, attribution)
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["inputs"] = {
        "ic_term_structure": ic_meta,
        "forward_windows": windows_meta,
        "attribution": attribution_meta,
    }
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ic", type=Path, default=DEFAULT_INPUTS["ic_term_structure"])
    parser.add_argument("--windows", type=Path, default=DEFAULT_INPUTS["forward_windows"])
    parser.add_argument("--attribution", type=Path, default=DEFAULT_INPUTS["attribution"])
    parser.add_argument("--output", type=Path, help="可选：写入报告 JSON")
    parser.add_argument("--strict", action="store_true", help="非 evidence_ready 返回退出码 2")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(
        {
            "ic_term_structure": args.ic,
            "forward_windows": args.windows,
            "attribution": args.attribution,
        }
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.strict and report["status"] != "evidence_ready":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

