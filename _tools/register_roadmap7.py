# -*- coding: utf-8 -*-
"""一次性登记脚本: 把路线图 ⑦ 项写入 ops/acceptance_status.json.

为何是一次性脚本而不是常驻工具: 登记动作只做一次, 常驻脚本会变成第二个
"事实源维护者"; 而 acceptance_status.json 已明确是单一事实源, 由
scripts/render_vulnerability_register.py 渲染。用完即弃(不提交)。
"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FP = os.path.join(_BASE, "ops", "acceptance_status.json")

ITEM = {
    "id": "ROADMAP-7",
    "level": "路线图",
    "title": "路线图 ⑦ 项能力交付: Regime多场景回测 / 动态止损 / 多策略组合回测 / TWAP拆单 / CPCV+DSR / LLM因子假设 / 交易前清单",
    "status": "fixed",
    "detail": (
        "**【2026-09-22 交付】** 7 项能力一次性落地, 全部带显式开关、单测与真机证据。"
        "\n\n**⑨ Regime 多场景回测** (`regime_costs.py` + `vnpy_backtest.py` 接线): "
        "stress = 滑点×4 / 费×2 / 信号延迟+1bar; **normal 恒等**, 保证既有回测产物可比。"
        "延迟实现依据 vnpy 源码 `get_signal()` 是 `filter(datetime==dt)` 且 bar 逐根喂入 ⇒ "
        "信号行日期后移 N 根 bar 等价于延迟 N 根成交; 窗口末 N 条信号**如实丢弃并计数**"
        "(`signals_dropped_by_latency`), 不补做。`slippage_share` 使滑点/税费归因可分离"
        "(实测本机滑点占比 72.9%)。新增批量入口 `run_regime_scenarios` / `run_regime_batch`"
        "(多日×多场景, 每场景写独立目录)。实测压力滑点 0.242% = 跌停板的 1/41, 仍在物理可能区。"
        "\n\n**⑩ 动态止损** (`trailing_stop.py`, 已接 paper_book / realtime_engine / backtest_engine): "
        "止损线 = max(固定线, peak*(1-giveback))。**核心不变量: 移动线恒 >= 固定线**"
        "(= 永不比固定止损更早砍仓), 9 个峰值档位的单测锁死。参数有推导: giveback=6% = "
        "锁住 2 倍于 stop_loss(3%) 所锚定风险的利润; hard_floor=9% < 一个跌停板 10% "
        "(不会被单根 bar 跳空打穿)。`hard_floor` 只约束**固定线** —— 实现时曾写成"
        "「对峰值线也取 min」, 那会把已锁的利润重新放开, 被单测抓出。判定(纯函数)与"
        "撮合(账本)分离, 归因走返回的 `trigger` 字段而非反向解析日志文本。"
        "\n\n**⑪ 多策略组合回测** (`portfolio_backtest.py`): 多腿目标权重矩阵按权重聚合后"
        "走**同一个 PaperBook** 撮合流程, 故组合与单策略结果成本口径完全一致。对齐取**并集**"
        "(取交集会掩盖「某腿经常不出手」); 逐腿逐日归一(不归一等于让标度大的腿独占组合); "
        "负权重直接抛错; `compare_to_single` 给出 vs 每条单腿与 vs 最好单腿的 excess"
        "(跑不过时**如实为负**, 演示为 -0.90pp)。**未接 run_daily**: 不自己取数, 需调用方"
        "写 DB→权重矩阵适配器。"
        "\n\n**⑫ TWAP/VWAP 拆单** (`exec_algo.py`): 逐档滑点**复用** "
        "`slippage_model.decompose_slippage`(有测试断言同一函数对象)。三处刻意不粉饰: "
        "① `saving_bps` 可为负且不被 max(0,·) 掩盖(实测 -20.9bps 的执行风险主导场景); "
        "② `saving_bps` 与价格差不必然同号(成本口径 vs 价格路径), assumptions 写明不要互相校验; "
        "③ ADV 太小时如实返回 `unscheduled` 缺口(实测缺 9,999,000 股)。"
        "**单位陷阱**: `participation_cap_slices(adv=)` 吃股数, `simulate_execution(avg_daily_volume=)` "
        "吃成交额(元)。**未接实盘下单路径**(接线会改变下单时序, 属独立改动)。"
        "\n\n**⑬ Purged K-Fold + Embargo + CPCV + DSR** (`purged_cv.py`): 只依赖标准库+numpy"
        "(`norm_cdf`/`norm_ppf` 用 math.erf 自实现, 不引入 scipy)。与既有 `pbo_cscv.py` "
        "**并列而非替换**: `probability_backtest_overfitting` 内部优先调 `pbo_cscv.cscv_pbo`, "
        "实测同块同值逐位相等(0.528571428571)。purge 用**逐对区间重叠**而非「测试集跨度」"
        "(跨度法在 CPCV 组合 (0,5) 会把训练集剔成空集); 无 t1 时显式退化为「仅 embargo」。"
        "`n_trials=1` 特判(原式含 Z^-1(0)=-inf), 此时 DSR 精确退化为 PSR。"
        "**两条直觉被推翻并如实记录**: 纯噪声单次 PBO 不≈0.5(跨种子 std≈0.21, 故只断言多种子均值); "
        "「每块太短会抛错」不成立(真条件是所有组合 IS 度量全 NaN)。"
        "\n\n**⑭ LLM 因子假设生成 (FaVOR 式假设级证据)** (`factor_hypothesis.py`): 流水线次序"
        "`lint → evidence_check → 收益评估`, **未通过前两步者绝不进入收益评估**(测试用 mock "
        "计数锁死)。lint 含前视偏差静态防线(shift(-n)/future_*/next_* 一律拒, 刻意「宁可错杀」)。"
        "表达式与 `gp_mine_daily` 的 readable 形态同构(算子超集)。**三条如实差距**: 不 import "
        "gp_mine_daily(它会拉起 pandas/scipy/gplearn/DB), 属同形兼容; 退化输入语义**刻意不同**"
        "(gp 的 _safe_div/_safe_log/_safe_inv 返回 1.0/0.0 以保 gplearn closure, 本模块返回 NaN —— "
        "伪造的常量会把「算不出来」掩盖成「算出来了」); 残留 X0 占位符需调用前替换。"
        "**收益评估只留回调接口, 未接 factor_mine.evaluator**; `--selftest` 用的是恒通过 STUB "
        "并在输出标注, 不得读成「真实收益已验证」。"
        "\n\n**⑮ 交易前清单前置** (`pretrade_gates.py`, 接在 `pretrade_compliance.gate()` 下单咽喉点): "
        "五项 = 单笔仓位 / 组合回撤 / IC门控态 / 数据新鲜度 / 单日亏损。四条纪律: 缺字段=skip 而非 fail"
        "(否则上游字段改名就会让系统静默停手); 只拦买入(离场全 skip, 不把风险锁在仓里); 判定异常不阻断"
        "但必须响亮落审计; **不抢高危单裁决权**(本仓红线是「高危单不执行, 转人工」而非直接拒)。"
        "**两项刻意做成「记数不判定」**(本次最重要的判断): (a) 数据新鲜度 —— 用户口径 "
        "`data_lag_days==0`, 但本仓该字段是**自然日**差(依据 `scripts/check_daemon_first_day.py:257` "
        "原话「自然日差(09-18→09-21=3), 不是交易日差, 故不会是 0/1」; 实测 09-21 plan 为 3 且上游账本"
        "记录「厂商当日数据尚未发布, 20:10 仍 engine_day=09-18」), 按 ==0 判会让**每个周一/节后第一天"
        "静默停手**; (b) 单笔仓位上限 —— `max_single_weight`(8%) 是集中度**压回线**不是下单前硬上限, "
        "用它拒单会 ①目标池不足 9 只时等权 band>8% ⇒ 拒掉全部买单 ②与 `target_weighting` 的 11.5% "
        "单票目标上限自相矛盾。**这条是被测试抓出来的**: `tests/test_pending_orders.py` 9 个用例原先"
        "全部失败(`assert 'reject' == 'pending_approval'`), 因为 30% 权益的大单本应进人工队列却被 "
        "8% 闸门直接拒。两项均保留显式开关(`PRETRADE_STRICT_FRESHNESS` / "
        "`PRETRADE_MAX_POSITION_PCT`), 开启后按严格口径判定 —— 那是知情选择而非默认行为。"
    ),
    "evidence": (
        "**新增模块**: src/regime_costs.py, src/trailing_stop.py, src/pretrade_gates.py, "
        "src/portfolio_backtest.py, src/purged_cv.py, src/exec_algo.py, src/factor_hypothesis.py；"
        "**接线**: src/vnpy_backtest.py(费率投影+信号延迟+批量入口+summary.regime 留痕), "
        "src/backtest_engine.py(移动止损+result.trailing_stop 留痕), src/paper_book.py"
        "(_apply_stop_loss 移动止损 + apply_risk_controls.trailing 归因), "
        "src/realtime_engine.py(移动止损 + 组合闸门 ctx + _plan_to_targets 透传 data_lag_days/"
        "section_as_of), src/pretrade_compliance.py(gate 第二段清单), src/config.py(4 项新开关); "
        "**测试**: tests/test_regime_costs.py / test_trailing_stop.py / test_pretrade_gates.py / "
        "test_portfolio_backtest.py / test_purged_cv.py(157 用例) / test_exec_algo.py(178 用例) / "
        "test_factor_hypothesis.py(156 用例); "
        "**真机证据**: scripts/verify_roadmap7.py -> data/preflight/verify_roadmap7.json"
        "(44 项检查, 只读、不写任何正式产物); docs/roadmap7-capabilities.md(边界与未做项); "
        "ops/acceptance_status.json(本条) + docs/vulnerability-register.md(渲染产物)"
    ),
    "next": (
        "**全量测试 1767 passed / 15 skipped / 0 failed**(本机 .venv314)。"
        "**明确未做的三处(均已登记在 docs/roadmap7-capabilities.md)**: "
        "① ⑫ 拆单执行器**未接实盘下单路径** —— 接线需把 plan['slices'] 与 "
        "per_slice.horizon_days 映射到 PaperBook.buy/sell 的逐档调用并处理跨 tick 剩余未成量, "
        "会改变下单时序, 属独立改动; "
        "② ⑪ 组合回测**未接 run_daily** —— 模块刻意不自己取数, 需在调用方写 DB→权重矩阵适配器; "
        "③ ⑭ 收益评估**只留 return_evaluator 回调**, 未接 factor_mine.evaluator / ai_factor_lab, "
        "需调用方提供并显式给出 IC/ICIR 门槛。"
        "另: ⑨ 的 vnpy 侧成本仍只有 long_rate/short_rate 两个费率, 无法表达最低 5 元佣金 —— "
        "既有近似, 本次未改。"
    ),
    "acceptance_wording": (
        "七项能力均可复跑: `python scripts/verify_roadmap7.py` 输出 44 项 PASS/FAIL 并落盘证据; "
        "全量测试 0 failed; 两项「记数不判定」的检查在严格模式下行为可复现。"
    ),
}


def main() -> int:
    with open(FP, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    before = len(items)
    ids = {it.get("id") for it in items}
    if "ROADMAP-7" in ids:
        items = [it for it in items if it.get("id") != "ROADMAP-7"]
        print("[register] 已存在 ROADMAP-7 -> 替换(幂等)")
    items.append(ITEM)
    data["items"] = items
    data["generated_at"] = "2026-09-22"
    with open(FP, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[register] items {before} -> {len(items)}; 新增 ROADMAP-7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
