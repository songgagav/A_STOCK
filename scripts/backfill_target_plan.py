# -*- coding: utf-8 -*-
"""回补 `target_plan.json`（**不重训模型** —— 用户 2026-09-20 决定）.

为什么需要它
  DRL 断供期（2026-09-05 之后）有交易日未产出 `target_plan.json`。用户决定：
  **只补 target_plan、不重训模型**，理由是"回补的目的是让台账完整，不是让模型学到那段"。
  并明确三条：产物标 `source=backfill`；**不纳入 OOS 的 n 计数**；模型重训作为独立动作解耦。

为什么不能复用 `run_drl_train` / 让 daemon 自动产出
  · `daemon._run_daily` 构造的是 `[PY, run_daily.py]`（+ `--maint`）**不传日期**，
    而 `run_daily` 是 `day = day or today` ⇒ daemon 只会写进**今天**的目录，
    **不可能**产出历史日期的 plan；
  · `run_drl_train` 会用**当日**训练出的新权重，而回补日**没有也不应有**新模型。
  故回补 = 用「严格早于该日的最近一版权重」直接调 `_build_target_plan`。

三道闸门（任一不满足即拒绝执行，不产出任何产物）
  1. **无前视**：`--weights-from` 必须**严格早于** `--day`；
  2. **截面必须存在**：显式传 `--as-of-section`（默认取 `--day`），
     该日截面在 views 中缺失时**响亮失败** —— 这正是 2026-09-08 的情形
     （views 里 0 行），此时**不得**用降级精简版冒充"那一天真的产出了"；
  3. **权重必须可查**：`--weights-from` 的 `train_meta.json` 里要有 `final_weights`。

用法
  # 先在沙箱验证（不碰生产）
  python scripts/backfill_target_plan.py --day 2026-09-07 --weights-from 20260905 \
      --out-dir %TEMP%\\bf --prod-data data --check-only

  # 只登记"某日截面缺失、无法回补"（不产出产物）
  python scripts/backfill_target_plan.py --day 2026-09-08 --weights-from 20260905 \
      --out-dir %TEMP%\\bf --prod-data data --record-unavailable

退出码: 0 = 成功/正确拒绝(带 reason); 1 = 参数或环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))


def _d8(s: str) -> str:
    return str(s or "").replace("-", "")


def _dash(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="要回补的交易日 YYYY-MM-DD")
    ap.add_argument("--weights-from", required=True,
                    help="权重来源日 YYYYMMDD（必须**严格早于** --day）")
    ap.add_argument("--as-of-section", default=None,
                    help="截面日期（默认 = --day）; 缺失则拒绝回补")
    ap.add_argument("--out-dir", required=True,
                    help="产物落盘根（沙箱或生产 data/）—— 必须显式给出, 避免误写生产")
    ap.add_argument("--prod-data", default=None,
                    help="读取端: views parquet 与权重所在的 data/（默认 = 仓库 data/）")
    ap.add_argument("--check-only", action="store_true", help="只做闸门检查, 不写盘")
    ap.add_argument("--record-unavailable", action="store_true",
                    help="截面缺失时, 往降级账本记一条『无法回补』(不产出产物)")
    ap.add_argument("--top-n", type=int, default=10)
    args = ap.parse_args()

    day8 = _d8(args.day)
    wf8 = _d8(args.weights_from)
    sec8 = _d8(args.as_of_section or args.day)
    prod = os.path.abspath(args.prod_data) if args.prod_data else os.path.join(_ROOT, "data")
    out_dir = os.path.abspath(args.out_dir)

    print("=" * 78)
    print("回补 target_plan（不重训模型）")
    print("=" * 78)
    print(f"  目标日    : {_dash(day8)}")
    print(f"  权重来源  : {wf8}  （必须严格早于目标日）")
    print(f"  截面日期  : {_dash(sec8)}")
    print(f"  读取端    : {prod}")
    print(f"  产物落盘  : {out_dir}{'  (check-only, 不写)' if args.check_only else ''}")

    # ---------------- 闸门 1: 无前视 ----------------
    if not (day8.isdigit() and len(day8) == 8):
        print("[FAIL] --day 格式应为 YYYY-MM-DD")
        return 1
    if wf8 >= day8:
        print(f"[拒绝] 权重来源日 {wf8} 不早于目标日 {day8} ⇒ 会引入**前视**（用后来的模型"
              f"回填历史信号）。已中止, 未产出任何产物。")
        return 0

    # ---------------- 闸门 3: 权重可查 ----------------
    wfp = os.path.join(prod, "drl", wf8, "train_meta.json")
    if not os.path.isfile(wfp):
        print(f"[拒绝] 权重来源的 train_meta 不存在: {wfp}")
        return 1
    with open(wfp, encoding="utf-8") as f:
        wmeta = json.load(f)
    fw = wmeta.get("final_weights") or {}
    if not fw:
        print(f"[拒绝] {wf8} 的 train_meta 里没有 final_weights")
        return 1
    print(f"  权重已取到: {len(fw)} 个因子, signal={fw.get('signal')}")

    # 让写入落 out_dir, 但**读取**必须指向 prod。
    # [2026-09-20 修正] 原实现只在 `prod != 仓库 data/` 时才覆盖 views 路径 —— 那是错的:
    # 本脚本在 import drl_train **之前**就把 config.DATA_DIR 改成了 out_dir, 于是
    # drl_train 模块级的 `_VIEW_SCORES_PARQUET = <DATA_DIR>/h5i/views/...` 指向**沙箱**,
    # 视图必然找不到 ⇒ 静默走 daily_bars 降级兜底, 连"截面缺失"的拒绝条件都不会触发
    # （实测: 0908 因此没被拒绝, 反而产出了一个降级产物）。故**无条件**覆盖。
    import config
    config.DATA_DIR = out_dir
    import drl_train as T
    T.DATA_DIR = out_dir
    T._H5I_VIEWS_DIR = os.path.join(prod, "h5i", "views")
    T._VIEW_SCORES_PARQUET = os.path.join(T._H5I_VIEWS_DIR,
                                          "v_factor_scores_daily.parquet")
    if not os.path.isfile(T._VIEW_SCORES_PARQUET):
        print(f"[拒绝] 读取端 views parquet 不存在: {T._VIEW_SCORES_PARQUET}")
        return 1
    print(f"  截面读取端: {T._VIEW_SCORES_PARQUET}")

    # ---------------- 闸门 2: 截面必须存在 ----------------
    try:
        T._load_plan_frame(as_of=sec8)
        section_ok, section_err = True, None
    except T.SectionUnavailable as e:
        section_ok, section_err = False, str(e)
    except Exception as e:  # noqa: BLE001
        section_ok, section_err = False, f"{type(e).__name__}: {e}"

    if not section_ok:
        print(f"\n[拒绝回补] 截面不可用: {section_err}")
        print("  说明: 截面缺失时若改用降级精简版, 产出的信号与『当时真的产出了』**不等价**"
              "（f_govern/f_vol/f_mom_rev 会被置 0）, 属静默降级 ⇒ 本脚本**不产出**该产物。")
        if args.record_unavailable:
            try:
                import drl_degrade as D
                D.record_event(
                    D.LEVEL_FALLBACK,
                    f"截面缺失, 无法回补 target_plan: {section_err}",
                    "**跳过回补（不产出降级产物）**, 仅登记",
                    day8, extra={"kind": "backfill_unavailable",
                                 # 明确标注: 这不是**模型**降级, 只是"该日截面不存在
                                 # 所以无法补台账"。否则读者会把两种性质混为一谈
                                 # （本项目在 P1-FML 上已吃过一次混归因的教训）。
                                 "model_degrade": False,
                                 "backfill": True, "weights_from": wf8,
                                 "as_of_section": sec8, "reason": "section_unavailable"})
                print(f"  已登记到降级账本: {D.event_ledger_path()}")
            except Exception as e:  # noqa: BLE001
                print(f"  [警告] 登记账本失败: {type(e).__name__}: {e}")
        return 0

    print("\n  [闸门通过] 截面存在且权重严格早于目标日")
    if args.check_only:
        print("\n(--check-only: 未写盘)")
        return 0

    # ---------------- 产出 ----------------
    res = T._build_target_plan(day=_dash(day8), day_dir=day8,
                               final_weights={k: float(v) for k, v in fw.items()},
                               top_n=args.top_n, as_of=sec8, source="backfill")
    outp = os.path.join(out_dir, "drl", day8, "target_plan.json")
    print(f"\n  结果: ok={res.get('ok')} top_n={res.get('top_n')} "
          f"universe={res.get('universe_size')} section_as_of={res.get('section_as_of')} "
          f"error={res.get('error')}")
    if res.get("ok") and os.path.isfile(outp):
        with open(outp, encoding="utf-8") as f:
            pl = json.load(f)
        print(f"  已写出: {outp}")
        print(f"  source={pl.get('source')} is_backfill={pl.get('is_backfill')} "
              f"oos_eligible={pl.get('oos_eligible')} section_as_of={pl.get('section_as_of')}")
        print(f"  前 3 名: {[it.get('canon') for it in (pl.get('top_n') or [])[:3]]}")
        return 0
    print("  [警告] 未产出可用产物")
    return 1


if __name__ == "__main__":
    sys.exit(main())
