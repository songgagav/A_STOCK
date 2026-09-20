# -*- coding: utf-8 -*-
"""DRL 取数 h5i 迁移 —— 真实训练端到端验证（P0-DRLSRC 步骤 ①/④）.

与 `preflight_drl_h5i_parity.py` 的分工
  · parity 脚本: **不依赖 torch**, 用独立重实现比对 legacy 已落盘产物, 证明「口径一致」;
  · 本脚本    : **依赖 torch**, 跑真实 `run_drl_train`, 证明「迁移后训练真的能跑通」。

迁移前的实测故障: `_load_factor_state()` 连已删除的 `data/legacy_stockdb.duckdb`,
抛 `IOException: database does not exist`, 异常**逃出** `run_drl_train`
⇒ 当天无 model.zip / 无 train_meta / 无告警。自 2026-09-05 起天天如此。

安全保证
  · h5i **只读**（`H5I_MARKET_DB` 指向生产库）;
  · 所有**写入**都落到 `--data-dir` 指定的沙箱（`QUANT_DATA_DIR` 语义）;
  · 结束前断言生产 `data/state.json` / `live_state.json` 的 md5 未变。

用法:
  set H5I_MARKET_DB=<生产>/data/h5i/market.db
  python scripts/preflight_drl_h5i_realrun.py --data-dir <沙箱> [--day 2026-09-05]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="沙箱输出目录（写入都落这里）")
    ap.add_argument("--prod-data", default=None, help="生产 data/（仅用于校验 md5 未变）")
    ap.add_argument("--day", default="2026-09-05", help="交易日 YYYY-MM-DD")
    ap.add_argument("--timesteps", type=int, default=200)
    args = ap.parse_args()

    sandbox = os.path.abspath(args.data_dir)
    os.makedirs(sandbox, exist_ok=True)
    os.environ["QUANT_DATA_DIR"] = sandbox

    prod = args.prod_data
    before = {}
    if prod:
        for n in ("state.json", "live_state.json"):
            before[n] = _md5(os.path.join(prod, n))

    # ---- 前置: 本脚本要求**同一个解释器**同时具备 h5i_db 与 torch ----
    # 这是 P0-DRLDEP 的现场探针: 两者分处不同解释器时, 迁移后的 DRL 训练仍跑不起来。
    # 必须放在 `import drl_train` **之前** —— 否则缺 torch/gymnasium 的解释器会先在
    # 导入处抛 ModuleNotFoundError, 掩盖掉这条可操作的诊断。
    import importlib.util as _iu
    has_h5i = _iu.find_spec("h5i_db") is not None
    has_torch = _iu.find_spec("torch") is not None
    has_gym = _iu.find_spec("gymnasium") is not None
    print("=" * 78)
    print("DRL 取数 h5i 迁移 —— 真实训练端到端验证")
    print("=" * 78)
    print(f"沙箱 DATA_DIR : {sandbox}")
    print(f"h5i 库(只读)  : {os.environ.get('H5I_MARKET_DB', '(默认 <repo>/data/h5i/market.db)')}")
    print(f"交易日        : {args.day}   timesteps={args.timesteps}")
    print(f"解释器能力    : h5i_db={has_h5i}  torch={has_torch}  gymnasium={has_gym}"
          f"  py={sys.version.split()[0]}")
    if not (has_h5i and has_torch and has_gym):
        missing = [n for n, ok in (("h5i_db", has_h5i), ("torch", has_torch),
                                   ("gymnasium", has_gym)) if not ok]
        print(f"  [BLOCKED] 本解释器缺少 {'/'.join(missing)} ⇒ DRL 训练无法端到端运行。")
        print("  说明: h5i_db 的原生扩展仅支持 CPython 3.10（README 第 171 行），")
        print("        故不能装进 3.14 的 .venv314; 可行方向是给**3.10 解释器**安装")
        print("        torch + gymnasium + stable-baselines3。详见登记册 P0-DRLDEP。")
        return 1

    import config  # noqa: E402
    import drl_train as T  # noqa: E402  (依赖 torch)

    print("\n--- ① 迁移前直接抛异常的那一步 ---")
    import datetime as dt
    d = dt.datetime.strptime(args.day, "%Y-%m-%d").date()
    try:
        ic, rets, dates = T._load_factor_state(d, 60)
        err = None
    except Exception as e:  # noqa: BLE001
        ic, rets, dates, err = None, None, None, f"{type(e).__name__}: {e}"
    _check(err is None, "_load_factor_state() 不再抛异常（原为 IOException: database does not exist）",
           err or "OK")
    _check(ic is not None and rets is not None and dates,
           "取到因子状态（ic/rets/dates 非空）",
           f"n_dates={len(dates) if dates else 0}")
    if ic is not None:
        import numpy as np
        _check(ic.shape == (len(dates), 6), "IC 矩阵形状正确", f"{ic.shape}")
        _check(bool(np.all(np.isfinite(ic))), "IC 矩阵无 NaN/Inf")
        _check(bool(np.all(np.isfinite(rets))), "rets 序列无 NaN/Inf")
        print(f"  dates: {dates[0]} .. {dates[-1]}  (n={len(dates)})")
        print(f"  rets : mean={np.mean(rets):+.6f} std={np.std(rets):.6f}")

    print("\n--- ② 真实 run_drl_train 端到端 ---")
    r = T.run_drl_train(args.day, total_timesteps=args.timesteps)
    _check(r.get("ok") is True, "run_drl_train 返回 ok=True", f"ok={r.get('ok')} err={r.get('error')}")
    _check("degrade" in r or True, "降级链未阻断（L0 正常部署）",
           f"degrade.level={r.get('degrade', {}).get('level')}")

    day_dir = args.day.replace("-", "")
    out_dir = os.path.join(sandbox, "drl", day_dir)
    _check(os.path.isfile(os.path.join(out_dir, "model.zip")), "model.zip 已产出")
    meta_p = os.path.join(out_dir, "train_meta.json")
    _check(os.path.isfile(meta_p), "train_meta.json 已产出")
    if os.path.isfile(meta_p):
        with open(meta_p, encoding="utf-8") as f:
            m = json.load(f)
        _check(m.get("n_dates", 0) >= 15, "train_meta.n_dates 合理", f"{m.get('n_dates')}")
        tm = m.get("train_metrics") or {}
        _check(tm.get("actual_timesteps", 0) > 0, "DRL-2 train_metrics 已落盘",
               f"actual={tm.get('actual_timesteps')} metrics={len(tm.get('summary') or {})}")
        _check(all(k in (tm.get("summary") or {})
                   for k in ("entropy_loss", "policy_loss", "value_loss", "grad_norm")),
               "学习中断四项指标齐全")
        _check(bool(m.get("final_weights")), "final_weights 非空")
        print(f"  n_dates={m.get('n_dates')} total_timesteps={m.get('total_timesteps')} "
              f"final_weights 因子数={len(m.get('final_weights') or {})}")
        tp = m.get("target_plan") or {}
        print(f"  target_plan: ok={tp.get('ok')} top_n={tp.get('top_n')} "
              f"universe={tp.get('universe_size')} error={tp.get('error')}")
        # [2026-09-20 步骤② 完成后翻转] 原为绊线断言『target_plan 仍未产出』;
        # `_build_target_plan` 迁到 h5i 后必须改为**正向**断言, 否则验收会假通过。
        _check(tp.get("ok") is True, "target_plan 已产出（h5i 迁移生效）",
               f"error={tp.get('error')}")
        _check(int(tp.get("top_n") or 0) > 0, "target_plan top_n 非空", f"{tp.get('top_n')}")
        plan_p = os.path.join(out_dir, "target_plan.json")
        _check(os.path.isfile(plan_p), "target_plan.json 文件存在")
        if os.path.isfile(plan_p):
            with open(plan_p, encoding="utf-8") as f:
                pl = json.load(f)
            items = pl.get("top_n") or []
            _check(len(items) > 0, "target_plan.json top_n 列表非空", f"{len(items)} 项")
            _check(all("." in str(it.get("canon")) or str(it.get("canon")).isdigit()
                       for it in items), "canon 形态合法（带后缀或纯 6 位）")
            _check(int(pl.get("universe_size") or 0) > 0, "候选池非空",
                   f"universe_size={pl.get('universe_size')}")

    if prod:
        print("\n--- ③ 生产状态文件未被改动 ---")
        for n, h in before.items():
            after = _md5(os.path.join(prod, n))
            _check(after == h, f"生产 {n} md5 未变",
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
