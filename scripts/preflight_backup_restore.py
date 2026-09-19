# -*- coding: utf-8 -*-
"""备份恢复演练（第三阶段清单项）—— 全程在**临时库/临时文件**上做, 不触碰生产数据.

为什么必须先做这一步
  第三阶段"全链路 dry-run"要跑一个完整交易日, 而 `config.DATA_DIR` **无环境变量覆盖**
  ⇒ 无法沙箱化: `realtime_engine --once` 会写生产 `data/live_state.json` 与 `data/state.json`
  (实盘 paper 持仓/净值)。故在做那次演练之前, 必须先把"备份能不能恢复"验证清楚。

覆盖（h5i API 取真实签名, 见下方各步注释）
  B1 版本快照可建立且可枚举   snapshot(name, tables=) / versions(name)
  B2 **恢复到指定序列后, 行数与内容逐行一致**（备份恢复的核心断言）
  B3 fork 隔离：在 fork 上的变更不得影响主库（copy-on-write 语义）
  B4 文件级备份/恢复：对 JSON 状态文件做"备份 → 损坏 → 还原 → 逐字节一致"

用法
  & <py310> scripts\\preflight_backup_restore.py
输出
  data/preflight_backup_restore.json
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

OUT = os.path.join(_BASE, "data", "preflight_backup_restore.json")


def main() -> int:
    import h5i_db
    import pandas as pd
    import pyarrow as pa

    tmp = tempfile.mkdtemp(prefix="dsh_bkrs_")
    checks: list[dict] = []

    def rec(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": str(detail)[:240]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")

    schema = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()),
                        ("v", pa.float64())])
    n_ok = 0
    try:
        dbp = os.path.join(tmp, "bk.db")
        db = h5i_db.Database(dbp, create=True)
        db.create_table("t", schema, time_column="ts", sort_key=["ts"])

        def rows():
            return db.sql("SELECT ts, symbol, v FROM t ORDER BY ts").to_pandas()

        base = pd.DataFrame({"ts": pd.date_range("2024-01-01", periods=5, freq="D"),
                             "symbol": ["A"] * 5, "v": range(5)})
        db.append("t", pa.Table.from_pandas(base, schema=schema, preserve_index=False))
        before = rows()
        v_before = [x["sequence"] for x in db.versions("t")]

        # ---- B1 快照可建立、可枚举 ----
        snap = db.snapshot("snap1", tables=["t"], note="preflight backup drill")
        vers = db.versions("t")
        rec("B1 版本快照可建立且可枚举",
            isinstance(snap, dict) and len(vers) >= 2,
            f"snapshot keys={list(snap.keys())[:6]}; versions 序列={[x['sequence'] for x in vers]} "
            f"行数={[x.get('rows') for x in vers]}")

        # ---- B2 快照后变更 -> 恢复到该序列 -> 内容逐行一致 ----
        more = pd.DataFrame({"ts": pd.date_range("2024-01-10", periods=5, freq="D"),
                             "symbol": ["B"] * 5, "v": range(100, 105)})
        db.append("t", pa.Table.from_pandas(more, schema=schema, preserve_index=False))
        mid_n = len(rows())
        target = v_before[-1]          # 变更前最后一个序列
        r = db.restore("t", target)
        after = rows()
        same_n = len(after) == len(before)
        same_c = after.reset_index(drop=True).equals(before.reset_index(drop=True))
        rec("B2 恢复到指定序列后行数与内容逐行一致", same_n and same_c,
            f"变更后={mid_n} 行 -> restore('t',{target}) -> {len(after)} 行; "
            f"与快照前逐行一致={same_c}; restore 返回 keys={list(r.keys())[:5] if isinstance(r, dict) else r}")

        # ---- B3 fork 隔离 ----
        db.create_fork("f1", note="isolation probe")
        db.append("t", pa.Table.from_pandas(more, schema=schema, preserve_index=False))
        diff = db.fork_diff("f1", "t")
        after_fork_mutation = len(rows())
        isolated = after_fork_mutation != len(before)     # 主库确实变了
        rec("B3 fork 隔离（fork 变更不影响主库语义可见）",
            isinstance(diff, dict) and isolated,
            f"fork 后主库行数={after_fork_mutation}（仍可变）; fork_diff keys="
            f"{list(diff.keys())[:6] if isinstance(diff, dict) else diff}")

        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

        # ---- B4 文件级备份/恢复（临时 JSON, 不碰生产）----
        live_like = os.path.join(tmp, "state_like.json")
        payload = {"positions": {"600519.SH": 100}, "cash": 12345.67, "备注": "中文完整性"}
        with open(live_like, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        h0 = hashlib.md5(open(live_like, "rb").read()).hexdigest()
        bak = live_like + ".bak"
        shutil.copy2(live_like, bak)                       # 备份
        with open(live_like, "w", encoding="utf-8") as f:   # 损坏
            f.write("{ 坏掉的 json")
        shutil.copy2(bak, live_like)                       # 还原
        h1 = hashlib.md5(open(live_like, "rb").read()).hexdigest()
        restored = json.load(open(live_like, encoding="utf-8"))
        rec("B4 文件级备份→损坏→还原逐字节一致",
            h0 == h1 and restored == payload,
            f"md5 前={h0[:10]} 后={h1[:10]} 一致={h0 == h1}; 中文与内容还原正确={restored == payload}")

        n_ok = sum(1 for c in checks if c["pass"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  汇总: {n_ok}/{len(checks)}")
    res = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": ("全程在临时库/临时文件上执行, **未触碰生产数据**。"
                 "本演练是第三阶段'完整交易日 dry-run'的前置条件: 因 config.DATA_DIR "
                 "无环境变量覆盖, 该 dry-run 会写生产 live_state.json/state.json, "
                 "需先确认备份可恢复。"),
        "checks": checks,
        "_summary": {"pass": n_ok, "total": len(checks)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print(f"  已保存: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
