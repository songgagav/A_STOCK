# -*- coding: utf-8 -*-
"""pytest 根 conftest: 把 src/ 加入 sys.path, 使 tests 可用扁平模块名 import
(config / factor_gate / ... 均位于 src/)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
