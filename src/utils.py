# -*- coding: utf-8 -*-
"""共享的小型运行时工具。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any


def atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """原子写入 JSON，并重试 Windows 读锁造成的替换冲突。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.05 * (2 ** attempt))
                else:
                    raise
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
