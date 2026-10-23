"""The close job must archive paper holdings even when selection is halted."""

import ast
import inspect
import json
import os
import sys
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import paper_book  # noqa: E402
import run_daily  # noqa: E402


def _halt_and_archive_branches():
    """Execute the real gate and archive branches without the external pipeline."""
    source = textwrap.dedent(inspect.getsource(run_daily.run_daily))
    tree = ast.parse(source)
    halt_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and any(
            isinstance(term, ast.UnaryOp)
            and isinstance(term.operand, ast.Name)
            and term.operand.id == "ds_allow"
            for term in node.test.values
        )
    ]
    branches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparison = node.test
        if not (
            isinstance(comparison.left, ast.Name)
            and comparison.left.id == "mode"
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
            and comparison.comparators[0].value == "full"
        ):
            continue
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "save_daily"
            for call in ast.walk(node)
        ):
            branches.append(node)
    assert len(halt_branches) == 1, "expected exactly one selection HALT branch"
    assert len(branches) == 1, "expected exactly one paper archival branch"
    module = ast.fix_missing_locations(
        ast.Module(body=halt_branches + branches, type_ignores=[])
    )
    return compile(module, run_daily.__file__, "exec")


def test_halt_without_selection_still_archives_paper_state(tmp_path, monkeypatch):
    """A data-source HALT leaves sel=None but must not suppress the paper archive."""
    monkeypatch.setattr(paper_book, "DAILY_DIR", str(tmp_path))
    report = {"steps": {}}
    state = {
        "day": "2026-10-23",
        "cash": 12345.0,
        "positions": {
            "000001.SZ": {
                "qty": 100,
                "avg_cost": 10.0,
                "buy_date": "2026-10-20",
                "last_price": 11.0,
            }
        },
    }
    scope = {
        "mode": "full",
        "ds_allow": False,
        "sel": None,
        "PaperBook": run_daily.PaperBook,
        "load_state": lambda: state,
        "day_dir": "20261023",
        "report": report,
    }

    exec(_halt_and_archive_branches(), scope)

    day_dir = tmp_path / "20261023"
    saved = json.loads((day_dir / "paper_book.json").read_text(encoding="utf-8"))
    trades = json.loads((day_dir / "trades.json").read_text(encoding="utf-8"))
    assert saved["cash"] == 12345.0
    assert saved["positions"]["000001.SZ"]["qty"] == 100
    assert saved["positions"]["000001.SZ"]["last_price"] == 11.0
    assert saved["open_positions"] == 1
    assert trades == []
    assert report["steps"]["select"]["skipped"] == "datasource_gate_halt"
    assert report["summary"] == {
        "equity": 13445.0,
        "cash": 12345.0,
        "open_positions": 1,
        "trades": 0,
    }
