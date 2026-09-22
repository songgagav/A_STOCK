# -*- coding: utf-8 -*-
"""交易前风险检查清单 (路线图 #15/⑮): 把"阈值触发"改为"下单前强制校验".

与既有 `pretrade_compliance.py` 的分工(刻意的, 不是重复实现)
------------------------------------------------------------
`pretrade_compliance.check_order()` 校验的是**这一笔单本身**是否成立: 整手/价格/
数量/可交易/现金底线/可卖数量。它不需要、也拿不到组合状态。
本模块校验的是**组合层面此刻是否允许加仓**: 组合回撤、IC 门控态、数据新鲜度、
日亏损。两者输入不同、失效模式不同, 故不合并到一个函数里 —— 但**在同一个
咽喉点被一起调用**(见 `pretrade_compliance.gate()`), 所以"清单"是一份。

关键设计: 缺信息 = 跳过, 不是失败
---------------------------------
三个闸门的输入字段(`regime` / `data_lag_days` / `day_start_equity`)分别来自
IC 门控产物、target_plan 元数据、引擎日初权益。任何一处取不到时, 本模块返回
`skip` 而**不是** `fail`。理由是本仓既有纪律: "缺字段 = 无信息, 不据此拒单
(避免因缺字段静默停手)"。反过来, 一旦字段存在且越界, 一律 `fail` —— 这才是
"强制校验"的意义。

为什么这些检查只作用于**买入**
-------------------------------
把离场也闸住 = 把风险锁在仓里(跌了也出不来)。这与 `kill_switch` 的纪律一致
("只停新开仓, 绝不停离场"), 故本模块对 `side='sell'` 直接返回全 skip/pass。
"""
from __future__ import annotations

import math
import os

#: 检查项名称(供测试与面板稳定引用)
CHK_POSITION = "position_pct"       # 单笔仓位 <= 上限
CHK_DRAWDOWN = "portfolio_drawdown"  # 组合回撤 < 上限
CHK_IC_GATE = "ic_gate_state"        # IC 门控非 RISK
CHK_FRESHNESS = "data_freshness"     # 数据新鲜度(滞后天数)
CHK_DAILY_LOSS = "daily_loss"        # 单日亏损未越界

#: IC 门控中禁止加仓的档位。与 `factor_gate` 的档位词表一致:
#: risk(风险) / caution(谨慎) 中只有 risk 是"冻结新买"; caution 仍按暴露系数
#: 缩量加仓, 故不列入。`freeze_new_buys` 为真时也在 `evaluate` 中单独判。
IC_BLOCKING_STATES = ("risk",)

#: 判定结果
PASS, FAIL, SKIP = "pass", "fail", "skip"


def _num(x):
    """只接受真数值(bool 除外) —— bool 是 int 的子类, 混进来会把 True 当 1。"""
    if isinstance(x, bool):
        return None
    return float(x) if isinstance(x, (int, float)) and math.isfinite(float(x)) else None


def evaluate(order: dict, ctx: dict | None = None, *,
             max_position_pct: float | None = None,
             max_drawdown: float | None = None,
             max_data_lag_days: int | None = None,
             max_daily_loss: float | None = None) -> dict:
    """**纯函数**: 组合级交易前清单。

    order : {symbol, side, qty, price}
    ctx   : {equity, day_start_equity, drawdown_pct, regime, freeze_new_buys,
             data_lag_days, min_cash}
            全部可选; 缺一项 => 该项 `skip`。

    阈值参数缺省为 None => **该项输出 skip**(不引入任何新数字)。生产接线处显式
    传入取自 `config.PAPER` 与用户既定清单的值, 见 `thresholds_from_paper()`。

    返回 {'ok': bool, 'checks': [{'name','status','detail','measured','threshold'}],
          'failed': [str], 'skipped': [str], 'reasons': [str]}
    `ok` = 没有 fail(有 skip 仍可 ok, 但会在 `skipped` 里如实列出)。
    """
    o = order or {}
    c = ctx or {}
    side = str(o.get("side") or "").lower()
    checks: list[dict] = []

    def add(name, status, detail="", measured=None, threshold=None):
        checks.append({"name": name, "status": status, "detail": detail,
                       "measured": measured, "threshold": threshold})

    # ---- 离场单: 一律放行(见模块 docstring) ----
    if side == "sell":
        for n in (CHK_POSITION, CHK_DRAWDOWN, CHK_IC_GATE, CHK_FRESHNESS, CHK_DAILY_LOSS):
            add(n, SKIP, "离场单不参与组合加仓闸门(只停新开仓, 不停离场)")
        return _finish(checks, note="离场单")

    qty, price = _num(o.get("qty")), _num(o.get("price"))
    equity = _num(c.get("equity"))
    _notional = (qty * price) if (qty is not None and price is not None) else None
    _pct = (_notional / equity * 100.0) if (_notional is not None and equity and equity > 0) else None

    # ---- 1) 单笔仓位 <= 上限 ----
    if max_position_pct is None:
        # 记数不判定(默认): 实测占比仍写进 detail/measured, 使面板看得见它,
        # 但不据此拒单 —— 理由见 thresholds_from_paper() 对 max_single_weight 的说明。
        add(CHK_POSITION, SKIP,
            (f"记数不判定: 单笔占权益 {_pct:.2f}% (名义 {_notional:.0f})"
             if _pct is not None else "缺 qty/price/equity, 无法算仓位占比"),
            measured=(round(_pct, 4) if _pct is not None else None),
            threshold="(未设阈值)")
    elif _pct is None:
        add(CHK_POSITION, SKIP, "缺 qty/price/equity 之一, 无法算仓位占比")
    else:
        lim = abs(float(max_position_pct)) * 100.0
        # 1e-9 容差: 目标权重恰好等于上限时不应因浮点误差被拒(按百分比比较)
        ok = _pct <= lim + 1e-9
        add(CHK_POSITION, PASS if ok else FAIL,
            f"单笔名义 {_notional:.0f} / 权益 {equity:.0f} = {_pct:.2f}%",
            measured=f"{_pct:.2f}%", threshold=f"<= {lim:.2f}%")

    # ---- 2) 组合回撤 < 上限 ----
    dd = _num(c.get("drawdown_pct"))
    if dd is None and equity and _num(c.get("peak_equity")):
        pk = _num(c.get("peak_equity"))
        dd = (equity / pk - 1.0) * 100.0 if pk > 0 else None
    if max_drawdown is None:
        add(CHK_DRAWDOWN, SKIP, "未给回撤上限(阈值缺省不判定)")
    elif dd is None:
        add(CHK_DRAWDOWN, SKIP, "缺 drawdown_pct/peak_equity, 无法算组合回撤")
    else:
        lim = abs(float(max_drawdown)) * 100.0
        ok = abs(min(dd, 0.0)) < lim          # 严格小于: 与用户清单"< 8%"同口径
        add(CHK_DRAWDOWN, PASS if ok else FAIL,
            f"组合回撤 {dd:.2f}%", measured=f"{dd:.2f}%", threshold=f"< {lim:.2f}%")

    # ---- 3) IC 门控态非 RISK ----
    regime = c.get("regime")
    if regime is None:
        add(CHK_IC_GATE, SKIP, "缺 regime(IC 门控未产出), 不据此拒单")
    else:
        r = str(regime).strip().lower()
        frozen = bool(c.get("freeze_new_buys"))
        blocked = r in IC_BLOCKING_STATES or frozen
        detail = f"门控档位={r}" + (" 且 freeze_new_buys=True" if frozen else "")
        add(CHK_IC_GATE, FAIL if blocked else PASS, detail,
            measured=r, threshold=f"not in {list(IC_BLOCKING_STATES)} and not frozen")

    # ---- 4) 数据新鲜度 ----
    lag = _num(c.get("data_lag_days"))
    if max_data_lag_days is None:
        # 记数不判定(默认): 仍然把实测滞后写进 detail, 使面板/日志里看得见它,
        # 只是不据此拒单。见 thresholds_from_paper() 里对自然日口径的说明。
        add(CHK_FRESHNESS, SKIP,
            (f"记数不判定: 数据滞后 {int(lag)} 自然日(data_lag_days, 非交易日差)"
             if lag is not None else "缺 data_lag_days, 无法判新鲜度"),
            measured=(int(lag) if lag is not None else None), threshold="(未设阈值)")
    elif lag is None:
        add(CHK_FRESHNESS, SKIP, "缺 data_lag_days, 无法判新鲜度")
    else:
        lim = int(max_data_lag_days)
        ok = lag <= lim
        add(CHK_FRESHNESS, PASS if ok else FAIL,
            f"数据滞后 {int(lag)} 自然日", measured=int(lag), threshold=f"<= {lim}")

    # ---- 5) 单日亏损 ----
    dl = _num(c.get("daily_loss_pct"))
    if dl is None:
        dse, eq = _num(c.get("day_start_equity")), equity
        if dse and dse > 0 and eq:
            dl = (eq / dse - 1.0) * 100.0
    if max_daily_loss is None:
        add(CHK_DAILY_LOSS, SKIP, "未给单日亏损上限(阈值缺省不判定)")
    elif dl is None:
        add(CHK_DAILY_LOSS, SKIP, "缺 daily_loss_pct/day_start_equity, 无法算日亏损")
    else:
        lim = abs(float(max_daily_loss)) * 100.0
        ok = dl > -lim                       # 未跌破 -lim
        add(CHK_DAILY_LOSS, PASS if ok else FAIL,
            f"当日盈亏 {dl:.2f}%", measured=f"{dl:.2f}%", threshold=f"> -{lim:.2f}%")

    return _finish(checks)


def _finish(checks: list[dict], note: str = "") -> dict:
    failed = [c["name"] for c in checks if c["status"] == FAIL]
    skipped = [c["name"] for c in checks if c["status"] == SKIP]
    reasons = [f"交易前清单未通过: {c['detail']} ({c['name']})"
               for c in checks if c["status"] == FAIL]
    out = {"ok": not failed, "checks": checks, "failed": failed,
           "skipped": skipped, "reasons": reasons}
    if note:
        out["note"] = note
    return out


def thresholds_from_paper() -> dict:
    """从 `config.PAPER` 取生产阈值。**不新增数字**, 且只取**同范畴**的既有数字。

    · max_drawdown      <- `portfolio_drawdown` (组合级回撤熔断线, 现行 8%)
    · max_daily_loss    <- `portfolio_drawdown` (同一根熔断线, 不另造第二根)
    · max_position_pct  <- 默认 **None(该项不判定)**, 见下
    · max_data_lag_days <- 默认 **None(该项不判定)**, 见下

    为什么仓位上限**不能**取 `max_single_weight`(重要, 有本仓依据)
    --------------------------------------------------------------
    `max_single_weight=8%` 在本仓是**集中度压回线**(`PaperBook._apply_concentration_cap`:
    超过 `8%*1.55=12.4%` 才把超配部分卖回 8%), **不是**"单笔不得超过"的下单前硬上限。
    把它当下单前硬上限会产生两个后果:

      1. 正常补仓被误杀 —— 目标池不足 9 只时等权 band 就 > 8%,
         按"单笔 > 8% 即拒"会把**全部**买单拒掉;
      2. 与既有目标加权自相矛盾 —— `target_weighting` 给单票的目标上限是 11.5%,
         用 8% 去拒单等于让策略层的目标永远无法达成。

    为什么仓位上限**默认不判定**（这个决定有两版历史，都记下来）
    ----------------------------------------------------------
    **第一版（错，2026-09-22 上午）**: 取 `max_single_weight`(8%) 当硬上限。
    它在本仓是**集中度压回线**（超过 `8%×1.55=12.4%` 才把超配部分卖回 8%），
    不是"单笔不得超过"的下单前硬上限。后果: 目标池不足 9 只时等权 band 就 >8%,
    **全部买单被拒**; 且与 `target_weighting` 的 11.5% 单票目标自相矛盾。

    **第二版（也错，同日盘后）**: 改成派生值 `max_single_weight × trigger_mult`
    (= 12.4%)。测试立刻抓出它**抢走了高危单的人工审批通道** ——
    `tests/test_pending_orders.py` 的 9 个用例红: 一笔 30,000 元(权益 30%)的单
    本应"转人工"(本仓红线: 高危单**不执行, 转人工**), 却被清单直接 **reject**。
    两者是**互相打架的两道闸门** —— 上限比高危线严, 高危线就永远够不到。

    **第三版（当前）**: 默认不判定。理由是**硬约束已经有人管了**:
      · "买入超过账户的钱" ⇒ 现金底线(`VIOL_CASH`, ctx 传了 `cash`+`min_cash` 即生效)。
        实测一笔 999,000 元(权益的 9.8 倍)的买入被它以"买入后现金 -918,310 < 底线"拒掉;
      · "单笔超一个等权槽位" ⇒ 高危判定(`classify_risk` 的 `notional_over_slot`)转人工。
    再加一个仓位上限只会**抢走第二道的裁决权**, 而没有覆盖任何第一/二道管不到的区间。
    故本项回归"记数不判定", 把实测占比写进 detail 供面板与日志查看。
    需要时仍可由运维显式启用(见下), 但那属于收紧策略, 应连同高危线一起重新标定。

    数据新鲜度为何默认不判定(实测依据)
    ----------------------------------
    用户给的清单口径是 "data_lag_days == 0"。但本仓产物里的 `data_lag_days` 是
    **自然日**差, 不是交易日差 —— `scripts/check_daemon_first_day.py:257` 原话:
    "**自然日**差(09-18→09-21 = 3), **不是**交易日差, 故不会是 0/1";
    `scripts/record_upstream_lag.py:16` 同口径。实测 2026-09-21 的 target_plan:
    `section_as_of=2026-09-18, data_lag_days=3, source=h5i_view`。

    于是"== 0"的直接后果是: **每个周一、每个节假日后的第一个交易日, 以及厂商
    当日数据尚未发布时(实测 20:10 仍未发布), 清单都会判 fail 并拒绝一切买入**
    —— 在默认开启的情况下这是一次**静默停手**, 属本仓明令避免的失效模式。
    故默认把该项设为 None(记数不判定), 需要严格时显式开启:

        PAPER["pretrade_strict_freshness"] = True   # 或 PRETRADE_STRICT_FRESHNESS=1

    开启后该项按"自然日 == 0"判定, 届时周一/节后会拦单 —— 这是**知情选择**,
    不是默认行为。
    """
    _env_max_pos = os.environ.get("PRETRADE_MAX_POSITION_PCT", "").strip()
    try:
        from config import PAPER
    except Exception:  # noqa: BLE001
        return {"max_position_pct": None, "max_drawdown": None,
                "max_data_lag_days": None, "max_daily_loss": None}
    dd = PAPER.get("portfolio_drawdown", None)
    _strict = bool(PAPER.get("pretrade_strict_freshness", False)) or \
        str(os.environ.get("PRETRADE_STRICT_FRESHNESS", "")).strip() in ("1", "true", "True")
    _mp = PAPER.get("pretrade_max_position_pct", None)
    # 默认 None = 记数不判定。**不要**在这里从 max_single_weight 派生 ——
    # 派生值(12.4%)会抢走高危单的人工审批通道(30% 权益的单会被直接 reject
    # 而不是转人工), 有 9 个既有用例为证。硬约束由现金底线与高危槽位判定负责。
    if _env_max_pos:
        try:
            _mp = float(_env_max_pos)
        except ValueError:
            _mp = None          # 环境变量写错 => 退回不判定, 不据此拒单
    return {
        "max_position_pct": _mp,
        "max_drawdown": dd,
        "max_daily_loss": dd,
        "max_data_lag_days": (0 if _strict else None),
    }


def summary_line(res: dict) -> str:
    """一行摘要(引擎日志用): 未通过项优先, 其次跳过项, 最后通过数。"""
    if not res:
        return "清单=未执行"
    n_pass = sum(1 for c in res.get("checks") or [] if c["status"] == PASS)
    parts = [f"通过{n_pass}"]
    if res.get("failed"):
        parts.append("未通过[" + ",".join(res["failed"]) + "]")
    if res.get("skipped"):
        parts.append("跳过[" + ",".join(res["skipped"]) + "]")
    return " ".join(parts)
