"""CI collection contracts for tests that exercise optional live-data sources."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_price_feed_provenance_does_not_import_optional_akshare_at_collection_time():
    source = (ROOT / "tests" / "test_price_feed_provenance.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    eager_akshare_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import)
        and any(alias.name == "akshare" for alias in node.names)
    ] + [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "akshare"
    ]

    assert not eager_akshare_imports
