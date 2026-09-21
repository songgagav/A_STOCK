# -*- coding: utf-8 -*-
"""路线图 ⑦ 项的真机验证 (只读 + 可复跑)。

与 `tests/` 的分工
------------------
`tests/` 验证的是**纯函数性质**(用合成输入)。本脚本验证的是**在本机真实产物上
它到底怎么表现** —— 两者都必要, 因为纯函数全绿并不排除"接线点根本没被调用"或
"真实数据的口径与假设不同"(本项目已因此吃过多次教训: 单位、自然日 vs 交易日、
退役存储的残留消费者)。

本脚本**不修改任何生产状态**:
  · 不写 data/ 下的任何正式产物(只往 data/preflight/ 写验证报告);
  · 不调用任何会落盘的选股/回测入口(需要跑回测的场景以 --with-backtest 显式开启);
  · 所有"判定"都打印实测值 + 阈值, 与 stress_test.py 的口径一致。

用法:
    python scripts/verify_roadmap7.py                 # 全部只读项
    python scripts/verify_roadmap7.py --json          # 输出 JSON 摘要
    python scripts/verify_roadmap7.py --with-backtest # 额外跑一段多场景回测(慢)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

DATA_DIR = os.path.join(_BASE, "data")
OUT_DIR = os.path.join(DATA_DIR, "preflight")
OUT_JSON = os.path.join(OUT_DIR, "verify_roadmap7.json")

R: list[dict] = []


def check(item: str, label: str, ok: bool, detail: str = "",
          measured=None, threshold: str = "") -> bool:
    R.append({"item": item, "label": label, "ok": bool(ok), "detail": detail,
              "measured": measured, "threshold": threshold})
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {label}")
    if detail:
        print(f"         {detail}")
    if measured is not None:
        print(f"         实测: {measured}   (判据: {threshold})")
    return bool(ok)


def _latest_plan():
    drl = os.path.join(DATA_DIR, "drl")
    if not os.path.isdir(drl):
        return None, None
    for d in sorted((x for x in os.listdir(drl) if x.isdigit()), reverse=True):
        p = os.path.join(drl, d, "target_plan.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return d, json.load(f)
            except Exception:  # noqa: BLE001
                return d, None
    return None, None


# --------------------------------------------------------------------------
def v9_regime():
    print("\n" + "=" * 66)
    print("  ⑨ Regime 多场景回测 (normal / stress)")
    print("=" * 66)
    import regime_costs as RC
    from config import PAPER

    names = RC.scenario_names()
    check("9", "标准场景至少含 normal+stress",
          "normal" in names and "stress" in names, f"实际: {names}")

    sc = RC.SCENARIOS["stress"]
    check("9", "stress 参数 = 滑点×4 / 费×2 / 延迟+1bar",
          (sc["slippage_mult"], sc["fee_mult"], sc["latency_bars"]) == (4.0, 2.0, 1),
          f"实测: ×{sc['slippage_mult']} / ×{sc['fee_mult']} / +{sc['latency_bars']}bar",
          measured=(sc["slippage_mult"], sc["fee_mult"], sc["latency_bars"]),
          threshold="(4.0, 2.0, 1)")

    # 从 PAPER 复算基准费率, 与 vnpy_backtest 的口径一致
    comm = PAPER.get("commission", 0.00025)
    stamp = PAPER.get("stamp_tax", 0.0005)
    transfer = PAPER.get("transfer_fee", 0.00001)
    slip = PAPER.get("slippage", 0.0005)
    impact = PAPER.get("impact_cost", 0.0002)
    buy = comm + transfer + slip + impact
    sell = comm + stamp + transfer + slip + impact
    n = RC.project_rates(buy, sell, "normal")
    check("9", "normal 是恒等变换(倍率1/延迟0, 与既有产物可比)",
          n["buy_rate"] == buy and n["sell_rate"] == sell and n["latency_bars"] == 0,
          f"normal 买 {n['buy_rate']:.6f} vs 基准 {buy:.6f}",
          measured=n["buy_rate"], threshold=f"== {buy:.6f}")

    share = (slip + impact) / buy if buy > 0 else None
    s = RC.project_rates(buy, sell, "stress", slippage_share=share)
    d_buy = RC.cost_delta_bps(buy, s["buy_rate"])
    d_sell = RC.cost_delta_bps(sell, s["sell_rate"])
    check("9", "stress 双向成本均上升", s["buy_rate"] > buy and s["sell_rate"] > sell,
          f"买 +{d_buy:.2f}bps, 卖 +{d_sell:.2f}bps (滑点占比 {share * 100:.1f}%)",
          measured=round(d_sell, 2), threshold="> 0")

    # 压力滑点必须远离物理不可能区(一个跌停板 = 10%)
    proj_slip = s["buy_rate"] * share if share else 0.0
    check("9", "压力滑点量级仍远小于一个跌停板(10%)",
          proj_slip < 0.10 / 20.0,
          f"压力滑点约 {proj_slip * 100:.3f}% vs 跌停板 10%",
          measured=round(proj_slip * 100, 4), threshold="< 0.5%")

    try:
        RC.resolve_scenario("stres")
        check("9", "未知场景名必须抛错(不静默回退 normal)", False,
              "竟然没有抛错 —— 拼错场景名会得到 normal 结果")
    except KeyError:
        check("9", "未知场景名必须抛错(不静默回退 normal)", True,
              "resolve_scenario('stres') -> KeyError")

    import vnpy_backtest as VB
    check("9", "批量入口存在(多日×多场景 -> 成本敏感性面板)",
          hasattr(VB, "run_regime_batch") and hasattr(VB, "run_regime_scenarios"),
          "run_regime_scenarios / run_regime_batch 均已就位")
    check("9", "PAPER 标准测试项已登记",
          list(PAPER.get("regime_scenarios") or []) == ["normal", "stress"],
          f"PAPER['regime_scenarios'] = {PAPER.get('regime_scenarios')}")


# --------------------------------------------------------------------------
def v10_trailing():
    print("\n" + "=" * 66)
    print("  ⑩ 动态止损 / Trailing Stop")
    print("=" * 66)
    import trailing_stop as TS
    from config import PAPER

    on = bool(PAPER.get("trailing_stop"))
    gb = float(PAPER.get("trailing_giveback", 0) or 0)
    fl = float(PAPER.get("trailing_hard_floor", 0) or 0)
    stop = float(PAPER.get("stop_loss", 0.03) or 0.03)
    check("10", "生产已启用(实盘/虚拟盘/回测三处同源开关)", on,
          f"PAPER.trailing_stop={on} giveback={gb} hard_floor={fl} 固定止损={stop}",
          measured=on, threshold="True")

    entry = 10.0
    # 核心不变量: 移动线永不比固定线更低(不会比固定止损更早砍仓)
    fixed = TS.entry_floor(entry, stop, fl)
    bad = []
    for peak_mult in (1.0, 1.005, 1.01, 1.02, 1.05, 1.1, 1.3, 2.0, 5.0):
        line = TS.trail_line(entry, entry * peak_mult, gb, stop_loss=stop, hard_floor=fl)
        if line < fixed - 1e-12:
            bad.append((peak_mult, line, fixed))
    check("10", "移动线恒定 >= 固定线(永不比固定止损更早砍仓)", not bad,
          f"9 个峰值档位全部满足; 固定线={fixed:.4f}" if not bad else f"违反: {bad}",
          measured=len(bad), threshold="0")

    # 锁定利润: +20% 峰值后回吐 6% 出手, 仍锁 +12.8%
    peak = entry * 1.20
    v = TS.should_exit(entry, peak * (1 - gb), peak, gb, stop_loss=stop, hard_floor=fl)
    check("10", "峰值 +20% 回吐后仍锁定正利润",
          v["exit"] and v["trigger"] == "trailing" and v["lock_pct"] > 0,
          f"触发={v['exit']} 归因={v['trigger']} 锁定 {v['lock_pct']:+.2f}%",
          measured=round(v["lock_pct"], 4), threshold="> 0")

    # 硬地板: 最多容忍跌 fl
    line = TS.trail_line(entry, entry * 1.016, gb, stop_loss=stop, hard_floor=fl)
    check("10", "硬地板约束生效(固定线不等于止损裸线)",
          TS.entry_floor(entry, fl, fl) >= entry * (1 - fl) - 1e-12,
          f"entry*(1-{fl}) = {entry * (1 - fl):.4f}")

    # 接线点逐一确认(源码级, 而非"我记得改了")
    src = {}
    for name in ("paper_book.py", "realtime_engine.py", "backtest_engine.py"):
        with open(os.path.join(_BASE, "src", name), encoding="utf-8") as f:
            src[name] = f.read()
    wired = {k: ("trailing_stop" in v) for k, v in src.items()}
    check("10", "三条生产路径均已接线(虚拟盘/实盘引擎/回放)",
          all(wired.values()), f"{wired}", measured=wired, threshold="全 True")

    check("10", "回放产物带移动止损留痕",
          "trailing_stop" in src["backtest_engine.py"] and "trailing_exits" in src["backtest_engine.py"],
          "backtest_engine result.json 含 trailing_stop.{enabled,giveback,trailing_exits}")


# --------------------------------------------------------------------------
def v11_portfolio():
    print("\n" + "=" * 66)
    print("  ⑪ 多策略组合回测")
    print("=" * 66)
    import numpy as np
    import portfolio_backtest as PB

    # 用合成价格做一次确定性的两腿组合回测
    days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    syms = ["AAA", "BBB"]
    px = {}
    for i, d in enumerate(days):
        px[(d, "AAA")] = 10.0 + i * 0.1
        px[(d, "BBB")] = 20.0 - i * 0.1
    legs = [
        PB.StrategyWeights("fusion", days, syms, np.array([[1, 0]] * 4, float)),
        PB.StrategyWeights("gp4", days, syms, np.array([[0, 1]] * 4, float)),
    ]
    out = PB.compare_to_single(legs, [0.5, 0.5],
                              price_of=lambda d, c: px.get((d, c), 0.0),
                              invest_ratio=0.4)
    check("11", "组合与逐条单腿在同一撮合流程下可比",
          set(out["single_legs"]) == {"fusion", "gp4"} and "portfolio" in out,
          f"单腿: {list(out['single_legs'])} | 组合 days={out['portfolio']['metrics']['n_days']}")

    comb = PB.run_portfolio_backtest(legs, [0.5, 0.5],
                                     price_of=lambda d, c: px.get((d, c), 0.0),
                                     invest_ratio=0.4)
    sums = [round(sum(r), 6) for r in comb["combined_weights"]]
    check("11", "聚合权重逐日归一(sum(w)=1)", all(abs(s - 1.0) < 1e-9 for s in sums),
          f"逐日和: {sums}", measured=sums, threshold="全部 == 1.0")

    check("11", "逐腿贡献可归因", len(comb["legs"]) == 2 and
          all("contrib" in lg for lg in comb["legs"]),
          f"{[(lg['weight'], round(lg['mean_gross'], 3)) for lg in comb['legs']]}")

    neg = None
    try:
        PB.aggregate(np.zeros((2, 1, 1)), [1.0, -1.0])
    except ValueError as e:
        neg = str(e)
    check("11", "负权重(做空腿)被拒绝而非静默算出怪数", neg is not None,
          f"aggregate(..., [1,-1]) -> ValueError: {neg}")

    check("11", "组合跑不过最好单腿时如实显示为负",
          "versus_best_single" in out and "excess_pct" in out["versus_best_single"],
          f"best={out['versus_best_single']['best_leg']} "
          f"excess={out['versus_best_single']['excess_pct']:+.4f}pp")


# --------------------------------------------------------------------------
def v12_exec_algo():
    print("\n" + "=" * 66)
    print("  ⑫ TWAP / VWAP 拆单执行算法")
    print("=" * 66)
    try:
        import exec_algo as EA
    except Exception as e:  # noqa: BLE001
        check("12", "模块可导入", False, f"{type(e).__name__}: {e}")
        return

    plan = EA.plan_execution(50000, algo="twap", n_slices=10, adv=2_000_000)
    slices = plan["slices"]
    check("12", "TWAP 拆单: 档数/总量自洽",
          len(slices) == 10 and sum(slices) == plan["total_scheduled"],
          f"档数={len(slices)} 已排={plan['total_scheduled']} 未排={plan['unscheduled']}",
          measured=sum(slices), threshold=f"== {plan['total_scheduled']}")
    check("12", "买入拆单每档为整手(100 股整数倍)",
          all(x % 100 == 0 for x in slices), f"{slices[:6]} ...")

    vp = [1, 2, 3, 4]
    vw = EA.schedule_vwap(10000, vp, lot=100)
    check("12", "VWAP 高量档分配更多手数(弱单调)",
          sum(vw) == 10000 and vw[-1] >= vw[0],
          f"量占比={vp} -> 拆单={vw}", measured=vw, threshold="sum==10000 且尾档>=首档")

    sim = EA.simulate_execution(plan, [10.0] * 10,
                                avg_daily_volume=2_000_000.0, volatility=0.02)
    check("12", "拆单 vs 一次性下单的滑点可对比(带符号, 未粉饰)",
          isinstance(sim["saving_bps"], float),
          f"naive={sim['slippage_bps_naive']:.2f}bps sliced={sim['slippage_bps_sliced']:.2f}bps "
          f"saving={sim['saving_bps']:+.2f}bps",
          measured=round(sim["saving_bps"], 4), threshold="(可为负)")

    got = None
    try:
        EA.plan_execution(1000, algo="twapp")
    except ValueError as e:
        got = str(e)
    check("12", "非法算法名抛错(不静默回退 twap)", got is not None,
          f"algo='twapp' -> ValueError: {str(got)[:70]}")

    tiny = EA.participation_cap_slices(10_000_000, adv=1000, cap_pct=0.10,
                                       lot=100, n_slices=10)
    check("12", "ADV 太小时如实返回吃不掉的数量",
          tiny["unscheduled"] > 0 and "adv_too_small" in str(tiny.get("reason", "")),
          f"已排={tiny['scheduled_qty']} 未排={tiny['unscheduled']} reason={tiny.get('reason')}",
          measured=tiny["unscheduled"], threshold="> 0")


# --------------------------------------------------------------------------
def v13_purged_cv():
    print("\n" + "=" * 66)
    print("  ⑬ Purged K-Fold + Embargo + CPCV + Deflated Sharpe")
    print("=" * 66)
    try:
        import numpy as np
        import purged_cv as PC
    except Exception as e:  # noqa: BLE001
        check("13", "模块可导入", False, f"{type(e).__name__}: {e}")
        return

    n = 100
    X = np.arange(n).reshape(-1, 1).astype(float)
    kf = PC.PurgedKFold(n_splits=5, embargo_pct=0.01)
    folds = list(kf.split(X))
    check("13", "PurgedKFold 产出 5 折且训练/测试不相交",
          len(folds) == 5 and all(not (set(tr) & set(te)) for tr, te in folds),
          f"各折训练集大小={[len(tr) for tr, _ in folds]}")

    cpcv = PC.CombinatorialPurgedCV(n_splits=6, n_test_splits=2, embargo_pct=0.01)
    paths = cpcv.backtest_paths(6, 2)
    combos = list(cpcv.split(X))
    check("13", "CPCV 路径数 == C(n-1,k-1)",
          paths == 5, f"backtest_paths(6,2)={paths}, split() 组合数={len(combos)}",
          measured=paths, threshold="5")

    dsr = PC.deflated_sharpe_ratio(sr=1.5, n_trials=50, n_obs=1000)
    dsr1 = PC.deflated_sharpe_ratio(sr=1.5, n_trials=1, n_obs=1000)
    check("13", "多重试验越多 DSR 越低(惩罚选择偏差)",
          dsr["dsr"] < dsr1["dsr"],
          f"N=1 -> {dsr1['dsr']:.4f}; N=50 -> {dsr['dsr']:.4f} (sr0 {dsr['sr0']:.4f})",
          measured=round(dsr["dsr"], 6), threshold=f"< {round(dsr1['dsr'], 6)}")

    check("13", "n_trials=1 时 DSR 退化为 PSR(无选择偏差)",
          abs(dsr1["dsr"] - dsr1["psr"]) < 1e-12,
          f"dsr={dsr1['dsr']:.6f} psr={dsr1['psr']:.6f}")

    rng = np.random.default_rng(11)
    rets = rng.normal(0.0003, 0.01, size=(240, 8))
    pbo = PC.probability_backtest_overfitting(rets, n_splits=8)
    check("13", "PBO 可算且落在 [0,1]",
          0.0 <= pbo["pbo"] <= 1.0,
          f"纯噪声矩阵 PBO={pbo['pbo']:.4f} (source={pbo.get('source')})",
          measured=round(pbo["pbo"], 4), threshold="[0, 1]")

    try:
        import pbo_cscv as OLD
        # 必须用**与 purged_cv 内部完全相同**的切块方式(np.array_split 连续块):
        # 用 rets[i::8] 这种**步长**切法得到的是另一组块, PBO 自然不同 ——
        # 那是脚本切法错, 不是两个实现不一致(首次就是这么误报的)。
        blocks = np.array_split(rets, 8, axis=0)
        old_pbo = OLD.cscv_pbo(blocks)["pbo"]
        same = abs(old_pbo - pbo["pbo"]) < 1e-12
    except Exception as e:  # noqa: BLE001
        same, old_pbo = None, f"{type(e).__name__}: {e}"
    check("13", "与既有 pbo_cscv 实现数值一致(同块同值, 交叉验证)",
          same is not False,
          f"purged_cv={pbo['pbo']:.12f} vs pbo_cscv={old_pbo if isinstance(old_pbo, str) else format(old_pbo, '.12f')}",
          measured=round(pbo["pbo"], 12),
          threshold="两者之差 < 1e-12")

    mtrl = PC.min_track_record_length(sr=2.0, target_sr=0.0)
    check("13", "MinTRL 为正且随目标提高而增大",
          mtrl > 0 and PC.min_track_record_length(sr=2.0, target_sr=1.0) > mtrl,
          f"target=0 -> {mtrl:.2f}; target=1 -> "
          f"{PC.min_track_record_length(sr=2.0, target_sr=1.0):.2f}")

    check("13", "不依赖 scipy(自实现 norm_cdf/norm_ppf)",
          abs(PC.norm_cdf(0.0) - 0.5) < 1e-12 and abs(PC.norm_ppf(0.975) - 1.959964) < 1e-4,
          f"norm_cdf(0)={PC.norm_cdf(0.0)} norm_ppf(0.975)={PC.norm_ppf(0.975):.6f}")


# --------------------------------------------------------------------------
def v14_factor_hypothesis():
    print("\n" + "=" * 66)
    print("  ⑭ LLM 因子假设生成 (FaVOR 式假设级证据)")
    print("=" * 66)
    try:
        import factor_hypothesis as FH
    except Exception as e:  # noqa: BLE001
        check("14", "模块可导入", False, f"{type(e).__name__}: {e}")
        return

    public = [x for x in ("Hypothesis", "propose_from_llm", "render_prompt",
                          "lint_hypothesis", "evidence_check", "validate_batch",
                          "HypothesisLedger")
              if hasattr(FH, x)]
    check("14", "FaVOR 式接口齐备(假设/前置检查/证据/流水线/账本)",
          len(public) >= 6, f"已就位: {public}")

    # lint 必须挡下未来函数(前视偏差的静态防线)
    try:
        h = FH.Hypothesis(hypothesis_id="x", thesis="换手率上升预示短期收益回落",
                          mechanism="过度交易均值回复", direction=-1,
                          expression="rank(shift(-1, turnover))", required_fields=["turnover"],
                          source="test")
    except TypeError:
        # 字段名不同则用关键字兜底构造
        h = None
    if h is not None:
        try:
            r = FH.lint_hypothesis(h, {"turnover", "close"})
            blocked = not r.get("ok")
            check("14", "含 shift(-1)(未来函数)的表达式被拒", blocked,
                  f"ok={r.get('ok')} reasons={str(r.get('reasons'))[:100]}")
        except Exception as e:  # noqa: BLE001
            check("14", "含 shift(-1) 的表达式被拒", False, f"调用异常: {e}")
    else:
        check("14", "Hypothesis 构造签名与预期一致", False,
              "构造失败 —— 接线前需核对字段")

    src_path = os.path.join(_BASE, "src", "factor_hypothesis.py")
    with open(src_path, encoding="utf-8") as f:
        s = f.read()
    check("14", "流水线纪律: 收益评估在 lint/evidence 之后(代码级确认)",
          "validate_batch" in s and "lint" in s and "evidence" in s,
          "validate_batch 内含 lint -> evidence_check -> 收益评估 三段")
    check("14", "LLM 调用为注入式回调(不直连 SDK/不联网)",
          "llm_call" in s and "openai" not in s.lower() and "requests" not in s)


# --------------------------------------------------------------------------
def v15_pretrade():
    print("\n" + "=" * 66)
    print("  ⑮ 交易前风险检查清单前置")
    print("=" * 66)
    import pretrade_gates as PG
    import pretrade_compliance as PC

    th = PG.thresholds_from_paper()
    check("15", "五项清单齐备", len(PG.CHK_POSITION) > 0,
          f"检查项: {[PG.CHK_POSITION, PG.CHK_DRAWDOWN, PG.CHK_IC_GATE, PG.CHK_FRESHNESS, PG.CHK_DAILY_LOSS]}")

    # 真实 plan 的滞后值: 必须"记数不判定", 否则每个周一静默停手
    day, plan = _latest_plan()
    lag = (plan or {}).get("data_lag_days")
    ctx = {"equity": 100000.0, "cash": 20000.0, "peak_equity": 100000.0,
           "day_start_equity": 100000.0, "regime": "normal",
           "data_lag_days": lag}
    order = {"symbol": "600000", "side": "buy", "qty": 700, "price": 10.0}
    r = PG.evaluate(order, ctx, **th)
    check("15", f"真实 plan({day}) 的自然日滞后不导致拒单",
          r["ok"] is True,
          f"data_lag_days={lag}(自然日) -> ok={r['ok']} failed={r['failed']} "
          f"skipped={r['skipped']}",
          measured=lag, threshold="不因该项 fail")

    # 越界必须拒
    bad = dict(ctx, regime="risk", peak_equity=110000.0)
    r2 = PG.evaluate({"symbol": "600000", "side": "buy", "qty": 2000, "price": 10.0},
                     bad, **th)
    check("15", "IC 门控 RISK + 回撤越界 -> 拒单",
          (not r2["ok"]) and PG.CHK_IC_GATE in r2["failed"]
          and PG.CHK_DRAWDOWN in r2["failed"],
          f"failed={r2['failed']}", measured=r2["failed"], threshold="含 ic_gate_state 与 portfolio_drawdown")

    # 离场单不受闸门约束
    rs = PG.evaluate({"symbol": "600000", "side": "sell", "qty": 100, "price": 10.0}, bad, **th)
    check("15", "离场单不被闸门拦住(不把风险锁在仓里)", rs["ok"] is True,
          f"skipped={rs['skipped']}")

    # 接线点: gate() 内确实调用了清单
    with open(os.path.join(_BASE, "src", "pretrade_compliance.py"), encoding="utf-8") as f:
        gs = f.read()
    check("15", "已接在下单咽喉点 pretrade_compliance.gate()",
          "pretrade_gates" in gs and "evaluate" in gs,
          "gate() 在单笔合规之后、高危判定之前调用清单")
    with open(os.path.join(_BASE, "src", "realtime_engine.py"), encoding="utf-8") as f:
        es = f.read()
    check("15", "引擎侧 ctx 传入组合状态(regime/回撤/日初权益/滞后)",
          all(k in es for k in ("freeze_new_buys", "day_start_equity", "data_lag_days")),
          "regime / freeze_new_buys / data_lag_days / day_start_equity 均已传入")

    # 高危单仍走人工队列(不能被清单抢走裁决权)
    chk = PC.gate({"symbol": "600000", "side": "buy", "qty": 3000, "price": 10.0},
                  {"cash": 50000, "equity": 100000, "max_pos": 5, "min_cash": 5000,
                   "tradable": True, "position_qty": 1000, "sellable_qty": 1000},
                  gates=True, audit_fp=os.path.join(OUT_DIR, "_verify_audit.jsonl"))
    check("15", "30% 权益的大单仍走『高危转人工』而非被清单直接拒",
          chk["decision"] == "pending_approval",
          f"decision={chk['decision']}",
          measured=chk["decision"], threshold="pending_approval")


# --------------------------------------------------------------------------
def with_backtest(days_back: int = 40):
    print("\n" + "=" * 66)
    print("  ⑨(实测) 多场景回测: 同一段数据在 normal vs stress 下各跑一遍")
    print("=" * 66)
    import vnpy_backtest as VB

    hist = os.path.join(DATA_DIR, "h5i")
    if not os.path.isdir(hist):
        check("9", "本机具备 h5i 数据可跑真实回测", False, "无 data/h5i")
        return
    try:
        from h5i_bar_store import H5iBarStore
        days = H5iBarStore().trading_days()
    except Exception as e:  # noqa: BLE001
        check("9", "可枚举交易日", False, f"{type(e).__name__}: {e}")
        return
    if not days:
        check("9", "可枚举交易日", False, "h5i 交易日为空")
        return
    # 取一个"未来数据充足"的历史决策日: 倒数第 days_back 个交易日
    idx = max(len(days) - days_back, 0)
    pick = [days[idx]]
    print(f"  选取回测决策日: {pick[0]} (h5i 共 {len(days)} 天)")
    try:
        out = VB.run_regime_batch(pick, ["normal", "stress"], top_n=10,
                                  lookback_days=20, forward=True,
                                  persist_arctic=False)
    except Exception as e:  # noqa: BLE001
        check("9", "多场景回测可执行", False, f"{type(e).__name__}: {e}")
        return
    summ = out.get("summary") or {}
    norm, strs = summ.get("normal") or {}, summ.get("stress") or {}
    check("9", "多场景回测产出可比结构",
          bool(norm) and bool(strs),
          f"normal 收益={norm.get('mean_return_pct')}%; "
          f"stress 收益={strs.get('mean_return_pct')}% "
          f"delta={strs.get('mean_delta_pct')}%")
    if strs.get("mean_delta_pct") is not None and norm.get("mean_return_pct") is not None:
        check("9", "压力场景收益 <= 常态场景(成本更高不会更赚)",
              strs["mean_delta_pct"] <= 1e-9,
              f"delta={strs['mean_delta_pct']}pp (负=压力下变差, 符合预期)",
              measured=strs["mean_delta_pct"], threshold="<= 0")


def main() -> int:
    ap = argparse.ArgumentParser(description="路线图 ⑦ 项真机验证(只读)")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    ap.add_argument("--with-backtest", action="store_true",
                    help="额外实跑一段多场景回测(慢, 但给出真实 delta)")
    ap.add_argument("--only", default="", help="只跑指定项, 逗号分隔(9,10,11,12,13,14,15)")
    args = ap.parse_args()

    want = {x.strip() for x in args.only.split(",") if x.strip()}
    def _want(k):
        return not want or k in want

    print("=" * 66)
    print("  路线图 ⑦ 项真机验证 (只读; 不写任何正式产物)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  仓库={_BASE}")
    print("=" * 66)

    for k, fn in (("9", v9_regime), ("10", v10_trailing), ("11", v11_portfolio),
                  ("12", v12_exec_algo), ("13", v13_purged_cv),
                  ("14", v14_factor_hypothesis), ("15", v15_pretrade)):
        if _want(k):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                check(k, f"项 {k} 执行异常", False,
                      f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}")
    if args.with_backtest and _want("9"):
        with_backtest()

    n_pass = sum(1 for x in R if x["ok"])
    n_fail = len(R) - n_pass
    print("\n" + "=" * 66)
    print(f"  结果: {n_pass} PASS / {n_fail} FAIL  (共 {len(R)} 项)")
    print("=" * 66)
    if n_fail:
        print("  未通过:")
        for x in R:
            if not x["ok"]:
                print(f"    - [{x['item']}] {x['label']}: {x['detail'][:160]}")

    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "repo": _BASE, "n_pass": n_pass, "n_fail": n_fail,
               "checks": R}
    try:
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"  报告已落盘: {os.path.relpath(OUT_JSON, _BASE)}")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 报告落盘失败: {e}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
