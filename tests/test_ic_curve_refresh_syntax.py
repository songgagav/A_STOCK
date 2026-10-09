"""IC 曲线刷新入口的语法完整性守卫。"""

import ast
from pathlib import Path


def test_ic_curve_refresh_source_is_parseable():
    source_path = Path(__file__).parents[1] / "src" / "ic_curve_refresh.py"
    ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
