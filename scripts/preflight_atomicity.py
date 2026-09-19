# -*- coding: utf-8 -*-
"""上线前复验 · 写入原子性: kill 中断 append 后库是否仍是"完好旧态/完好新态"
(preflight_atomicity.py).

复验清单"一、数据层 #8": 写入原子性 —— rebuild 中途 kill, 检查表是否半成品。
**安全边界**: 全程只在一次性测试库 `data/_preflight_atomic.db` 上做, 绝不触碰
生产库 `data/h5i/market.db`。清单强调"kill 中断"与"备份恢复"是两件事, 本脚本只测前者。

API 备忘(本脚本踩过的坑):
  · `create_table(name, schema: pa.Schema, time_column=, sort_key=)` —— 第二参是
    **pyarrow Schema** 而非 DataFrame; 且声明 time_column 时 sort_key 必须**以时间列开头**。
  · `append(name, data)` 经 `_to_ipc` -> 只接受 `pa.Table` / `RecordBatch` / 批次序列,
    **不接受 DataFrame**, 需 `pa.Table.from_pandas(df, schema=..., preserve_index=False)`。
  · h5i 库是**目录**(catalog/tables/...), 清理必须整树删除, 只判 isfile 会漏删。

输出
  data/preflight_atomicity.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

DBP = os.path.join(_BASE, "data", "_preflight_atomic.db")
OUT = os.path.join(_BASE, "data", "preflight_atomicity.json")
PY = sys.executable

WRITER = r'''
import sys, os, time
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import h5i_db, pandas as pd, numpy as np, pyarrow as pa
db = h5i_db.Database(sys.argv[1])
n = int(sys.argv[2])
schema = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()),
                    ("close", pa.float64())])
print("writer start", flush=True)
# ts 必须**严格晚于**库内现有最大 ts（h5i_db.append 的硬约束），否则直接报错 ——
# 基线写到 2021-09 附近, 故这里从 2022-01-01 起。
for chunk in range(20):
    ts = pd.date_range("2022-01-01", periods=n // 20, freq="min") + pd.Timedelta(days=chunk)
    df = pd.DataFrame({"ts": ts, "symbol": ["T%06d" % chunk] * (n // 20),
                       "close": np.arange(n // 20, dtype=float)})
    db.append("atomic_test", pa.Table.from_pandas(df, schema=schema, preserve_index=False))
    print("chunk %d ok" % chunk, flush=True)
    time.sleep(0.15)          # 拉长窗口, 便于中途 kill
print("writer done", flush=True)
'''


def _count():
    """只读打开测试库, 返回 (行数, 错误文本)."""
    import h5i_db
    try:
        db = h5i_db.Database(DBP, read_only=True)
    except Exception as e:  # noqa: BLE001
        return None, f"open: {type(e).__name__}: {str(e)[:120]}"
    try:
        d = db.sql("SELECT COUNT(*) AS n FROM atomic_test").to_pandas()
        return int(d["n"].iloc[0]), None
    except Exception as e:  # noqa: BLE001
        return None, f"query: {type(e).__name__}: {str(e)[:120]}"
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    import h5i_db
    import pandas as pd
    import numpy as np
    import pyarrow as pa

    if os.path.isdir(DBP):
        shutil.rmtree(DBP, ignore_errors=True)
    elif os.path.exists(DBP):
        os.remove(DBP)
    print("测试库:", DBP)

    db = h5i_db.Database(DBP, create=True)
    schema = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()),
                        ("close", pa.float64())])
    db.create_table("atomic_test", schema, time_column="ts", sort_key=["ts"])
    base = pd.DataFrame({"ts": pd.date_range("2019-01-01", periods=1000, freq="D"),
                         "symbol": ["BASE"] * 1000,
                         "close": np.arange(1000, dtype=float)})
    db.append("atomic_test", pa.Table.from_pandas(base, schema=schema,
                                                  preserve_index=False))
    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass
    n0, err0 = _count()
    print(f"基线: 可读={err0 is None} 行数={n0} {err0 or ''}")

    N = 20000
    print(f"\n启动写入子进程 (目标 +{N} 行, 分 20 块, 每块间 0.15s)...")
    proc = subprocess.Popen([PY, "-u", "-c", WRITER, DBP, str(N)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", cwd=_BASE)
    time.sleep(2.0)
    print(f"  2 秒后 kill 子进程 (pid={proc.pid})")
    proc.kill()
    try:
        out = proc.communicate(timeout=30)[0] or ""
    except Exception:  # noqa: BLE001
        out = ""
    print(f"  子进程输出={out.strip()[:200]!r} returncode={proc.returncode}")

    time.sleep(1.0)
    n1, err1 = _count()
    full = (n0 or 0) + N
    print(f"\nkill 后: 可读={err1 is None} 行数={n1} {err1 or ''}")
    if err1 is not None:
        verdict = "不可读(corrupt)"
    elif n1 == n0:
        verdict = "回到基线(旧态完好)"
    elif n1 == full:
        verdict = "恰好写完(窗口太短, 需加长)"
    elif n1 is not None and n0 is not None and n0 < n1 < full:
        verdict = f"部分提交(+{n1 - n0} 行) —— 需判断是否属分块提交语义"
    else:
        verdict = "其它"
    print(f"判定: 基线={n0}, kill 后={n1}, 写满应为={full} => {verdict}")

    res = {"db": DBP, "base_rows": n0, "base_err": err0, "target_rows": N,
           "after_kill_rows": n1, "after_kill_err": err1, "full_rows": full,
           "writer_returncode": proc.returncode, "verdict": verdict,
           "note": "仅在一次性测试库上执行, 生产库未触碰"}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
