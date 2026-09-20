# -*- coding: utf-8 -*-
"""DRL 端到端**验收**（只桩掉两条数据路径）—— P0-DRLSRC 迁移的最终验收脚本.

为什么这样切
  两条**数据路径**的正确性已分别独立证明, 不需要在本脚本里重复:
    · `_load_factor_state` (h5i 复算 IC/rets)：`scripts/preflight_drl_h5i_parity.py`
      在真实 h5i 上 16 样本 8/8 PASS（差异唯一归因到 legacy 缺 2026-09-01）;
    · `_load_plan_frame` (h5i 取候选池截面)：同脚本第④节 15/15 PASS（5225 只截面、
      LEFT JOIN 5205/5205 不丢行）+ `tests/test_drl_target_plan_h5i.py` 15 用例。
  本脚本桩掉这两条路径后跑**完整 `run_drl_train`**, 于是它验证的是**下游全部真实逻辑**:
  FactorWeightEnv → CVaR-PPO 训练 → 模型落盘 → train_meta(含 DRL-2 指标) →
  DRL-4 降级链决策 → `_build_target_plan` 打分/TopN → target_plan.json。

  为什么要这么切: 生产环境的 `P0-DRLDEP`（无解释器同时具备 h5i_db 与 torch）让
  "两条数据路径 + 下游逻辑"无法在同一个解释器里一次跑完。拆开验证后,
  **解除 P0-DRLDEP 就只剩"装上依赖"这一步**, 本脚本即刻成为真正的端到端验收。

用法（需 torch/gymnasium/sb3, 无需 h5i_db）:
  python scripts/preflight_drl_e2e_stubbed.py --data-dir <沙箱> --prod-data <生产 data/>
退出码: 0 = 全部断言通过; 1 = 有失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

_RESULTS: "list[tuple[bool, str]]" = []


def _check(ok: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _md5(p: str) -> "str | None":
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _stub_plan_frame(n=40):
    """造一个与 legacy SQL 输出**列完全一致**的候选池截面。"""
    import numpy as np
    import pandas as pd
    rng = np.random.RandomState(20260920)
    mk = ["sh", "sz", "bj"]
    rows = []
    for i in range(n):
        m = mk[i % 3]
        bare = f"{600000 + i:06d}"
        rows.append({
            "canon": f"{bare}.{m.upper()}",
            "date": "2026-09-05",
            "close": float(5.0 + rng.rand() * 100),
            "change_pct": float(rng.randn()),
            "turnover": float(abs(rng.randn())),
            "amount": float(abs(rng.randn()) * 1e6),
            "f_signal": float(rng.randn()),
            "f_trend": float(rng.randn()),
            "f_govern": float(rng.randn()),
            "f_liquidity": float(rng.randn()),
            "f_vol": float(rng.randn()),
            "f_mom_rev": float(rng.randn()),
            "signal": "HOLD",
        })
    df = pd.DataFrame(rows)
    df.loc[df["f_trend"] > 0, "signal"] = "BUY"
    return df, "stub_plan_frame"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="沙箱输出目录（写入都落这里）")
    ap.add_argument("--prod-data", default=None, help="生产 data/（仅校验 md5 未变）")
    ap.add_argument("--day", default="2026-09-05")
    ap.add_argument("--timesteps", type=int, default=200)
    args = ap.parse_args()

    sandbox = os.path.abspath(args.data_dir)
    os.makedirs(sandbox, exist_ok=True)
    os.environ["QUANT_DATA_DIR"] = sandbox

    before = {}
    prod_plan = None
    if args.prod_data:
        for n in ("state.json", "live_state.json"):
            before[n] = _md5(os.path.join(args.prod_data, n))
        # 生产侧同名 plan 也要在**跑之前**取指纹: 它很可能本来就存在（m4 前的历史产物,
        # 如 20260905）, 若跑完再取就成了自己和自己比, 断言会变成空转。
        prod_plan = os.path.join(args.prod_data, "drl", args.day.replace("-", ""),
                                 "target_plan.json")
        before["<prod plan>"] = _md5(prod_plan)

    import importlib.util as _iu
    if _iu.find_spec("torch") is None or _iu.find_spec("gymnasium") is None:
        print("[BLOCKED] 本脚本需要 torch + gymnasium（数据路径已被桩掉, 不需要 h5i_db）。")
        return 1

    import numpy as np
    import config
    import drl_degrade
    import drl_train as T

    # 沙箱: drl_train 用的是 `from config import DATA_DIR` 的模块级绑定, 必须一并改
    T.DATA_DIR = sandbox
    config.DATA_DIR = sandbox

    day = args.day
    day_dir = day.replace("-", "")
    n_days = 45
    rng = np.random.RandomState(7)
    rets = rng.randn(n_days) * 0.012
    ic = rng.randn(n_days, len(T.SCORE_FACTORS)) * 0.05
    dates = [f"2026-07-{i + 1:02d}" for i in range(n_days)]

    print("=" * 78)
    print("DRL 端到端验收（只桩数据路径, 下游逻辑全部真实）")
    print("=" * 78)
    print(f"沙箱 DATA_DIR : {sandbox}")
    print(f"交易日        : {day}   timesteps={args.timesteps}")
    print(f"桩: _load_factor_state -> ic{ic.shape} rets{rets.shape}（生产同形）")
    print("桩: _load_plan_frame   -> 40 只候选池（列与 legacy SQL 一致）")
    print(f"真实: FactorWeightEnv / CVaR-PPO / 落盘 / DRL-2 指标 / DRL-4 降级链 / "
          f"打分与 TopN / target_plan.json")

    T._load_factor_state = lambda _d, _lb=60: (ic, rets, dates)
    T._load_plan_frame = _stub_plan_frame

    print("\n--- ① 跑完整 run_drl_train ---")
    r = T.run_drl_train(day, total_timesteps=args.timesteps, use_vnpy_reward=False)
    _check(r.get("ok") is True, "run_drl_train 返回 ok=True",
           f"ok={r.get('ok')} err={r.get('error')}")

    out_dir = os.path.join(sandbox, "drl", day_dir)
    _check(os.path.isfile(os.path.join(out_dir, "model.zip")), "model.zip 已产出")
    meta_p = os.path.join(out_dir, "train_meta.json")
    _check(os.path.isfile(meta_p), "train_meta.json 已产出")

    if os.path.isfile(meta_p):
        with open(meta_p, encoding="utf-8") as f:
            m = json.load(f)

        print("\n--- ② DRL-2 学习中断指标 ---")
        tm = m.get("train_metrics") or {}
        summ = tm.get("summary") or {}
        _check(int(tm.get("actual_timesteps") or 0) > 0, "actual_timesteps > 0",
               f"{tm.get('actual_timesteps')}")
        _check((tm.get("duration_s") or 0) > 0, "duration_s 已测量", f"{tm.get('duration_s')}s")
        _check(tm.get("threshold_applied") is False, "不施加收敛阈值（METHOD-1）")
        for k in ("entropy_loss", "policy_loss", "value_loss", "grad_norm"):
            _check(k in summ and summ[k]["n"] >= 1, f"指标 {k} 非空",
                   f"n={summ.get(k, {}).get('n', 0)}")
        _check(len(summ) >= 8, "指标数量充足（>=8）", f"{len(summ)}")

        print("\n--- ③ DRL-4 降级链 ---")
        dec = m.get("degrade") or {}
        _check(dec.get("level") == drl_degrade.LEVEL_OK, "分级为 L0 正常部署",
               f"level={dec.get('level')} {dec.get('level_name')}")
        _check(dec.get("halt") is False, "未阻断当日 plan")
        _check(os.path.isfile(drl_degrade.pointer_path()), "模型指针已写入",
               drl_degrade.pointer_path())

        print("\n--- ④ target_plan 生成（真实打分 + TopN）---")
        tp = m.get("target_plan") or {}
        _check(tp.get("ok") is True, "target_plan ok=True", f"error={tp.get('error')}")
        _check(int(tp.get("universe_size") or 0) == 40, "候选池规模正确",
               f"{tp.get('universe_size')}")
        _check(int(tp.get("top_n") or 0) == T.MAX_STOCKS, f"top_n == MAX_STOCKS({T.MAX_STOCKS})",
               f"{tp.get('top_n')}")
        plan_p = os.path.join(out_dir, "target_plan.json")
        _check(os.path.isfile(plan_p), "target_plan.json 文件已产出")
        if os.path.isfile(plan_p):
            with open(plan_p, encoding="utf-8") as f:
                pl = json.load(f)
            items = pl.get("top_n") or []
            _check(len(items) == T.MAX_STOCKS, "top_n 列表长度正确", f"{len(items)}")
            _check(len(pl.get("weights_used") or {}) == len(T.SCORE_FACTORS),
                   "weights_used 含全部 6 个因子", f"{sorted((pl.get('weights_used') or {}))}")
            _check(all(("." in str(it.get("canon"))) for it in items),
                   "canon 均带市场后缀")
            sc = [it.get("drl_score") for it in items]
            _check(all(a >= b for a, b in zip(sc, sc[1:])), "TopN 按 drl_score 降序")
            _check(abs(sum(it.get("target_weight", 0) for it in items) - 1.0) < 1e-6,
                   "target_weight 合计 ≈ 1", f"{sum(it.get('target_weight', 0) for it in items):.6f}")
            print(f"  top1: {items[0].get('canon')} score={items[0].get('drl_score'):.4f}")

    print("\n--- ⑤ 只写沙箱, 不碰生产 ---")
    # 正确的隔离检查 = **跑前/跑后指纹比对**, 而不是"文件是否存在":
    # 仓库 data/drl/<day>/target_plan.json 很可能**本来就存在**（m4 之前的历史产物,
    # 例如 20260905）, 用 os.path.exists 判据会把历史文件误报成"被本次写出"。
    if args.prod_data:
        for n, h in before.items():
            p = prod_plan if n == "<prod plan>" else os.path.join(args.prod_data, n)
            after = _md5(p)
            _check(after == h, f"生产 {n if n != '<prod plan>' else 'drl/<day>/target_plan.json'}"
                               f" 跑前跑后 md5 一致",
                   f"{'(不存在)' if after is None else after[:12]}")

    fails = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结论: {'全部通过' if not fails else '有失败项'}  "
          f"({len(_RESULTS) - len(fails)}/{len(_RESULTS)} PASS)")
    for f in fails:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
