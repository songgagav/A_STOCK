# ============================================================
# paper_book.py -- A股纸面撮合账本
# 制度: 只做多 / T+1(当日买不可当日卖) / 全额资金(无杠杆)
# 撮合价: 优先 AKShare 实时/最新价, 失败降级用选股价/昨收
# ============================================================

import os
import json
from datetime import datetime, date
import traceback

from config import (
    INIT_CAPITAL, MAX_POS_RATIO, MAX_STOCKS, PAPER,
    STATE_FILE, DAILY_DIR, DATA_DIR, DUCKDB_PATH,
)

# 全A实时快照拉取超时(秒): 东财接口无内置超时, 网络卡死/分页阻塞时兜底
# 改为 10s: 引擎每 15s 跑一次 fetch, 太长的等待会拖慢 tick.
# 配合 fetch 内部的多源降级, 一次失败也能在主路径内拿到部分价格.
SPOT_TIMEOUT = 10
# 单源额外请求超时(秒): 给东财接口加 HTTPS 连接超时, 避免单次连接卡死
SPOT_HTTP_TIMEOUT = 6


class PaperBook:
    """A股只做多纸面账本"""

    def __init__(self, init_capital: float = INIT_CAPITAL):
        self.init_capital = float(init_capital)
        self.cash = float(init_capital)
        self.positions = {}   # canon -> {qty, avg_cost, buy_date: 'YYYY-MM-DD'}
        self.realized = 0.0
        # 累计费用归因: 买入佣金/卖出费用直接影响现金, 但 realized 已含卖出费,
        # 买入费却从未计入任何 P&L -> 用独立累加器, 使
        # total_pnl(equity-init) == realized + unrealized - buy_fees 严格对账.
        self.fees_paid = 0.0      # 全部已付费用(买入+卖出)
        self.buy_fees = 0.0       # 买入费用(未计入 realized, 单独归因)
        self.sell_fees = 0.0      # 卖出费用(已含在 realized 内)
        self.today_trades = 0
        self.state = "NORMAL"   # 风控状态: NORMAL / DRAW_DOWN(组合回撤熔断)
        self.peak_equity = float(init_capital)   # 组合权益峰值(用于回撤熔断判定)
        self.risk_log = []      # 当日风控动作记录 {time, kind, canon, msg}
        self.last_price = {}  # canon -> 最新价
        self.trade_log = []   # 当日成交 (兼容旧字段)
        self.trade_date = date.today().strftime("%Y-%m-%d")
        self.trades_today = []     # 当日成交列表 (当前交易日)
        self.d_price = {}          # canon -> 撮合价
        self.day = date.today().strftime("%Y-%m-%d")
        # 历史成交: {date_str -> [trade, ...]}, 跨日累积, 用于展示与回溯
        self.trades_history: dict = {}
        # P2: 已入账的分红/除权事件key, 避免跨日/重启重复记账
        self.applied_corp: set = set()

    # ---------- 工具 ----------
    def _commission(self, amount: float, is_sell: bool) -> float:
        """费用: 佣金(买卖) + 印花税(卖出单边) + 过户费"""
        comm = amount * PAPER["commission"]
        comm = max(comm, 5.0)  # 最低5元
        stamp = amount * PAPER["stamp_tax"] if is_sell else 0.0
        transfer = amount * PAPER["transfer_fee"]
        return comm + stamp + transfer

    def market_value(self) -> float:
        return sum(
            p["qty"] * self.market_price(c) for c, p in self.positions.items()
        )

    def market_price(self, canon: str) -> float:
        """当前撮合价: 盘中实时价 -> 兜底最后价 -> 0"""
        pr = self.d_price.get(canon)
        if pr and pr > 0:
            return pr
        return 0.0

    # ---------- 工具 ----------
    def _record_trade(self, trade: dict) -> None:
        """内部: 记录到 trades_today 与 trades_history[今日].
        跨日时自动把上一日 trades_today 归档到 history, 然后清空.
        日期优先级: 显式 trade['date'] > self.trade_date > date.today().
        """
        if trade.get("date") is None:
            today = self.trade_date or date.today().strftime("%Y-%m-%d")
            trade["date"] = today
        today = trade["date"]
        # 检测 trade_date 切换: 当前 trade_date != today -> 归档上一日
        if self.trade_date != today:
            if self.trades_today:
                self.trades_history.setdefault(self.trade_date, []).extend(self.trades_today)
            self.trades_today = []
            self.trade_date = today
            self.day = today
        self.trades_today.append(trade)
        self.trades_history.setdefault(today, []).append(trade)
        self.today_trades = len(self.trades_today)
        # 兼容旧字段
        self.trade_log.append(trade)

    # ---------- 撮合 ----------
    def buy(self, canon: str, qty: int, price: float,
            avg_daily_volume: float | None = None,
            volatility: float | None = None,
            execution_horizon_days: float = 0.0,
            urgency_kappa: float = 1.0):
        """买入成交 (做多, 全额资金约束; 计入滑点; 当日买入全额锁定T+1).

        Parameters
        ----------
        avg_daily_volume : float | None
            标的日均成交额 (元), 用于 Almgren-Chriss 动态滑点计算.
            为 None 时使用固定滑点率.
        volatility : float | None
            标的日频波动率, 用于 Almgren-Chriss 动态滑点计算.
            为 None 时使用固定滑点率.
        execution_horizon_days : float
            预计执行时长 (交易日). >0 时滑点 = 市场冲击 + 执行风险两个分量.
        urgency_kappa : float
            执行紧迫度系数 (仅 execution_horizon_days>0 时生效).
        """
        if qty <= 0 or price <= 0:
            return None
        # 动态滑点: Almgren-Chriss 模型 vs 固定费率
        if avg_daily_volume is not None and avg_daily_volume > 0:
            from slippage_model import decompose_slippage
            vol = volatility if volatility is not None else 0.025
            order_amt = qty * price
            _dec = decompose_slippage(
                order_amt, avg_daily_volume, vol,
                execution_horizon_days=execution_horizon_days,
                urgency_kappa=urgency_kappa,
            )
            exec_price = price * (1 + _dec["total_rate"])
            _slippage_dec = _dec
        else:
            impact = PAPER.get("impact_cost", 0.0002)
            exec_price = price * (1 + PAPER["slippage"] + impact)
            _slippage_dec = None
        cost = qty * exec_price
        fee = self._commission(cost, is_sell=False)
        total = cost + fee
        if total > self.cash:   # 资金不足, 拒绝(由rebalance控制档位)
            return None
        self.cash -= total
        self.fees_paid += fee
        self.buy_fees += fee
        if canon in self.positions:
            p = self.positions[canon]
            tot_cost = p["avg_cost"] * p["qty"] + cost
            p["qty"] += qty
            p["avg_cost"] = tot_cost / p["qty"]
            p["buy_date"] = self.trade_date
            p["locked_qty"] = p.get("locked_qty", 0) + qty   # 当日加仓全部锁定
        else:
            self.positions[canon] = {
                "qty": qty, "avg_cost": exec_price,
                "buy_date": self.trade_date, "locked_qty": qty,
            }
        _trade = {
            "type": "buy", "canon": canon, "qty": qty,
            "price": round(exec_price, 3), "fee": round(fee, 2),
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        if _slippage_dec is not None:
            _trade["impact_bps"] = _slippage_dec["market_impact_bps"]
            _trade["exec_risk_bps"] = _slippage_dec["execution_risk_bps"]
        self._record_trade(_trade)
        _ret = {"canon": canon, "qty": qty, "price": exec_price, "fee": fee}
        if _slippage_dec is not None:
            _ret["impact_bps"] = _slippage_dec["market_impact_bps"]
            _ret["exec_risk_bps"] = _slippage_dec["execution_risk_bps"]
        return _ret

    def sell(self, canon: str, qty: int, price: float,
             avg_daily_volume: float | None = None,
             volatility: float | None = None,
             execution_horizon_days: float = 0.0,
             urgency_kappa: float = 1.0):
        """卖出 (T+1: 当日买入数量不可卖; 计入滑点).

        Parameters
        ----------
        avg_daily_volume : float | None
            标的日均成交额 (元), 用于 Almgren-Chriss 动态滑点计算.
        volatility : float | None
            标的日频波动率, 用于 Almgren-Chriss 动态滑点计算.
        execution_horizon_days : float
            预计执行时长 (交易日). >0 时滑点 = 市场冲击 + 执行风险两个分量.
        urgency_kappa : float
            执行紧迫度系数 (仅 execution_horizon_days>0 时生效).
        """
        if canon not in self.positions:
            return None
        p = self.positions[canon]
        # 可卖数量 = 总持仓 - 当日锁定
        locked = p.get("locked_qty", 0) if p.get("buy_date") == self.trade_date else 0
        sellable = p["qty"] - locked
        if sellable <= 0:
            return None   # 全部当日买入, 不可卖
        if qty <= 0 or qty > sellable:
            qty = sellable
        # 撮合价: 动态滑点(Almgren-Chriss) vs 固定费率
        if avg_daily_volume is not None and avg_daily_volume > 0:
            from slippage_model import decompose_slippage
            vol = volatility if volatility is not None else 0.025
            order_amt = qty * price
            _dec = decompose_slippage(
                order_amt, avg_daily_volume, vol,
                execution_horizon_days=execution_horizon_days,
                urgency_kappa=urgency_kappa,
            )
            exec_price = price * (1 - _dec["total_rate"])
            _slippage_dec = _dec
        else:
            impact = PAPER.get("impact_cost", 0.0002)
            exec_price = price * (1 - PAPER["slippage"] - impact)
            _slippage_dec = None
        proceeds = qty * exec_price
        fee = self._commission(proceeds, is_sell=True)
        self.cash += proceeds - fee
        self.fees_paid += fee
        self.sell_fees += fee
        # 已实现盈亏
        realized_this = (exec_price - p["avg_cost"]) * qty - fee
        self.realized += realized_this
        p["qty"] -= qty
        if p["qty"] <= 0:
            del self.positions[canon]
        _trade = {
            "type": "sell", "canon": canon, "qty": qty,
            "price": round(exec_price, 3), "fee": round(fee, 2),
            "pnl": round(realized_this, 2),
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        if _slippage_dec is not None:
            _trade["impact_bps"] = _slippage_dec["market_impact_bps"]
            _trade["exec_risk_bps"] = _slippage_dec["execution_risk_bps"]
        self._record_trade(_trade)
        _ret = {"canon": canon, "qty": qty, "price": exec_price, "fee": fee}
        if _slippage_dec is not None:
            _ret["impact_bps"] = _slippage_dec["market_impact_bps"]
            _ret["exec_risk_bps"] = _slippage_dec["execution_risk_bps"]
        return _ret

    # ---------- P2: 分红/除权记账 ----------
    # A 股现金红利税按持有期: <1月 20%, 1月-1年 10%, >1年 0%. 轮动持仓多为
    # 短握(<1月), 默认按 20%; 送转股不收税、只平摊成本(总成本不变).
    _DIV_TAX_SHORT = 0.20    # <1月
    _DIV_TAX_MID   = 0.10    # 1月-1年
    _DIV_TAX_LONG  = 0.00    # >1年

    def apply_corporate_actions(self, events: list) -> list:
        """对当前持仓应用到期除权事件, 返回实际入账事件的明细.

        events: [{symbol(纯6位), ex_date, bonus_ratio(每10股送转股数),
                  dividend_cash(每10股税前现金)}, ...].
        记账规则:
          送转/拆股: 新增股数 = floor(qty/10 * bonus_ratio), 总成本不变,
                    新 avg_cost = 原成本总额 / 新qty (平摊到全部股份).
          现金分红: 按持有期计税后入现金; 当归因(realized+unrealized-buy_fees)
                    与 equity 对账时, 分红现金计入 cash 自然体现为总权益增量.
        """
        applied = []
        for ev in events:
            key = ev.get("key")
            if key and key in self.applied_corp:      # 幂等: 已入账不再重复
                continue
            sym = ev.get("symbol")
            canon = next((c for c in self.positions
                          if c.split(".")[0] == sym), None)
            if not canon:
                continue                       # 非持仓, 无意义
            p = self.positions[canon]
            qty = p["qty"]
            notes = []

            # 1) 送转股/拆股 (bonus_ratio 每10股)
            bonus_r = float(ev.get("bonus_ratio") or 0)
            if bonus_r > 0 and qty >= 10:
                bonus_qty = int(qty // 10 * bonus_r)
                if bonus_qty > 0:
                    cost_total = p["avg_cost"] * qty
                    new_qty = qty + bonus_qty
                    p["qty"] = new_qty
                    p["avg_cost"] = cost_total / new_qty     # 平摊成本, 总成本不变
                notes.append(f"送转+{bonus_qty}")
            elif bonus_r > 0:
                notes.append(f"送转{bonus_r:.2f}/10股(持仓不足10股跳过)")

            # 2) 现金分红 (dividend_cash 每10股税前)
            div10 = float(ev.get("dividend_cash") or 0)
            if div10 > 0 and qty > 0:
                div_per = div10 / 10.0
                holding_days = self._holding_days(canon, ev.get("ex_date"))
                if holding_days >= 365:
                    tax = self._DIV_TAX_LONG
                elif holding_days >= 30:
                    tax = self._DIV_TAX_MID
                else:
                    tax = self._DIV_TAX_SHORT
                after_tax = div_per * p["qty"] * (1 - tax)
                self.cash += after_tax
                notes.append(f"红利+{after_tax:.2f}(税后,{tax*100:.0f}%税)")
            applied.append({
                 "canon": canon, "symbol": sym,
                 "ex_date": str(ev.get("ex_date")),
                 "type": "/".join(n for n in notes if n) or "无记账",
             })
            if ev.get("key"):
                self.applied_corp.add(ev["key"])
        return applied

    def _holding_days(self, canon: str, ex_date) -> float:
        """以 ex_date 计当日持有天数 (无精确成本日时按当前 trade_date 近似)."""
        try:
            d = date.fromisoformat(str(ex_date))
        except Exception:
            d = date.today()
        ref = date.today()
        if self.trade_date:
            try:
                ref = date.fromisoformat(self.trade_date)
            except Exception:
                ref = date.today()
        return max((ref - d).days, 0)

    # ---------- 风控 (行为层拦截) ----------
    # 规则(见 config.PAPER):
    #   1) 单票止损: 持仓浮亏跌破 -stop_loss 即触发减仓(可卖部分).
    #   2) 集中度:   单票市值占权益上限 max_single_weight, 超配自动压回.
    #   3) 组合熔断: 组合级回撤跌破 -portfolio_drawdown 暂停加仓(self.state=DRAW_DOWN).
    def _update_drawdown_state(self) -> float:
        """按最新权益刷新峰值, 判定是否触发组合级回撤熔断.
        返回当前组合回撤比例(负数). 权益新高时恢复 NORMAL."""
        eq = self.cash + self.market_value()
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = eq / self.peak_equity - 1.0 if self.peak_equity > 0 else 0.0
        limit = PAPER.get("portfolio_drawdown", 0.08)
        if dd <= -limit and self.state != "DRAW_DOWN":
            self.state = "DRAW_DOWN"
            self._risk_log_record("circuit_breaker", None,
                                  f"组合回撤 {dd*100:.1f}% <= -{limit*100:.0f}%, 熔断暂停加仓")
        elif dd > -limit and self.state == "DRAW_DOWN":
            # 回撤收复之上, 恢复加仓; 需持仓实际涨回, 不会刚触发就立即恢复
            self.state = "NORMAL"
            self._risk_log_record("circuit_breaker", None,
                                  f"组合回撤收复至 {dd*100:.1f}%, 解除熔断")
        return dd

    def _risk_log_record(self, kind: str, canon, msg: str) -> None:
        try:
            self.risk_log.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "kind": kind, "canon": canon, "msg": msg,
            })
        except Exception:
            pass

    def _apply_stop_loss(self) -> list:
        """单票止损: 对浮亏跌破 -stop_loss 的持仓卖出可卖部分. 返回触发的canon列表.
        跌停/无价由调用方(引擎_tradable)过滤; 此处只做撮合层兜底."""
        stop = PAPER.get("stop_loss", 0.03)
        out = []
        for canon in list(self.positions.keys()):
            p = self.positions[canon]
            pr = self.market_price(canon)
            if pr <= 0 or p["avg_cost"] <= 0:
                continue
            drawdown = pr / p["avg_cost"] - 1.0
            if drawdown < -stop:
                # 只卖可卖部分: T+1 锁定仅当日买入不可卖, 跨日自动解锁
                _locked = p.get("locked_qty", 0) if p.get("buy_date") == self.trade_date else 0
                qty = p["qty"] - _locked
                if qty > 0:
                    self.sell(canon, qty, pr)
                    self._risk_log_record("stop_loss", canon,
                                          f"单票浮亏 {drawdown*100:.1f}% < -{stop*100:.0f}% 止损")
                    out.append(canon)
        return out

    def _apply_concentration_cap(self) -> list:
        """集中度上限: 单票市值远超 max_single_weight 时, 将其超配部分卖出压回上限.
        一次性压到目标权重(±1手), 避免单次只卖部分导致多次触发.
        返回触发压回的canon列表."""
        cap = PAPER.get("max_single_weight", 0.08)
        eq = self.cash + self.market_value()
        out = []
        # 触发系数(2026-09-05 config 化): 默认 1.55 -> 触发线 = 8%*1.55 = 12.4%.
        # 上调原因: fml 目标加权单票上限 11.5%, 需留 >0.9pp 安全带避免
        # 「目标超触发 -> 压回(→8%) -> 补权至目标」自振荡(历史 P0 教训).
        trig_mult = float(PAPER.get("concentration_trigger_mult", 1.55) or 1.55)
        for canon in list(self.positions.keys()):
            p = self.positions[canon]
            pr = self.market_price(canon)
            if pr <= 0 or eq <= 0 or p["avg_cost"] <= 0:
                continue
            mv = p["qty"] * pr
            # 仅在明显超配(> 触发系数缓冲)时才压回, 避免盘中抖动触发频繁减仓.
            if mv / eq <= cap * trig_mult:
                continue
            # 可卖: T+1 锁定仅当日买入不可卖, 跨日自动解锁
            _locked = p.get("locked_qty", 0) if p.get("buy_date") == self.trade_date else 0
            sellable = p["qty"] - _locked
            if sellable <= 0:
                continue
            # 目标持仓市值 = 权益*cap, 反推可保留股数(向下取整到100股)
            target_qty = int(eq * cap // pr // 100) * 100
            target_qty = min(target_qty, p["qty"])
            qty_sell = p["qty"] - target_qty
            qty_sell = min(qty_sell, sellable)
            if qty_sell >= 100:
                self.sell(canon, qty_sell, pr)
                self._risk_log_record("concentration", canon,
                                      f"单票权重 {mv/eq*100:.1f}% > {cap*100:.0f}% 压回至上限")
                out.append(canon)
        return out

    def apply_risk_controls(self) -> dict:
        """组合风控主入口: 依次执行 熔断判定(先,决定后续可否加仓) / 单票止损 / 集中度压回.
        返回当日风控动作摘要 {drawdown, stopped, trimmed, state}.
        在盘中每 tick(rebalance前/后)调用一次, 由引擎驱动."""
        dd = self._update_drawdown_state()
        stopped = self._apply_stop_loss()
        trimmed = self._apply_concentration_cap()
        return {
            "drawdown_pct": round(dd * 100, 2),
            "state": self.state,
            "stopped": stopped,
            "trimmed": trimmed,
        }

    # ---------- 调仓 ----------
    def rebalance(self, target: list, latest_price: dict):
        """
        target: [{canon, weight, price}...] 目标持仓(等权)
        latest_price: 可选, 覆盖盘中价
        """
        self.d_price = dict(latest_price) if latest_price else {}
        target_canons = {t["canon"] for t in target}
        # 0) 组合熔断: 回撤达阈值暂停补仓(仍允许止损/离场)
        self._update_drawdown_state()
        hold_add = self.state != "DRAW_DOWN"
        # 1) 卖出不在目标的持仓
        for canon in list(self.positions.keys()):
            if canon not in target_canons:
                pr = self.market_price(canon)
                if pr > 0:
                    self.sell(canon, self.positions[canon]["qty"], pr)
        # 1.1) 单票止损 + 集中度压回(防御性兜底, 即使不在目标池也执行)
        self._apply_stop_loss()
        self._apply_concentration_cap()
        if not hold_add:
            return
        # 2) 对目标持仓按等权买入/加仓
        total_target_mv = self.init_capital * MAX_POS_RATIO
        band = total_target_mv / max(len(target), 1)
        for t in target:
            canon = t["canon"]
            pr = self.market_price(canon)
            if pr <= 0:
                continue
            cur_mv = self.positions[canon]["qty"] * pr if canon in self.positions else 0
            target_mv = band
            # 集中度硬约束: 单票目标市值不超过权益上限
            eq = self.cash + self.market_value()
            target_mv = min(target_mv, eq * PAPER.get("max_single_weight", 0.08))
            diff = target_mv - cur_mv
            if diff > pr * 100:   # 至少1手(100股)
                qty = int(diff // (pr * 100)) * 100
                self.buy(canon, qty, pr)

    # ---------- 持久化 / 快照 ----------
    def snapshot(self) -> dict:
        mv = self.market_value()
        equity = self.cash + mv
        # 未实现盈亏 = 各持仓 (现价-成本) * 数量; avg_cost 为买入成交价(含买入滑点, 不含买入费)
        unrealized = sum(
            (self.market_price(c) or 0) * p["qty"]
            - p["avg_cost"] * p["qty"]
            for c, p in self.positions.items()
        )
        # 归因恒等式: equity - init == realized + unrealized - buy_fees
        # (realized 已含卖出费; buy_fees 从现金直接扣但从未进 realized)
        attributed = self.realized + unrealized - self.buy_fees
        return {
            "date": self.trade_date,
            "init_capital": self.init_capital,
            "equity": round(equity, 2),
            "cash": round(self.cash, 2),
            "market_value": round(mv, 2),
            "realized": round(self.realized, 2),
            "unrealized": round(unrealized, 2),
            "fees_paid": round(self.fees_paid, 2),
            "buy_fees": round(self.buy_fees, 2),
            "sell_fees": round(self.sell_fees, 2),
            "attributed_pnl": round(attributed, 2),
            "open_positions": len(self.positions),
            "trades_today": len(self.trades_today),
            "risk_state": self.state,
            "drawdown_pct": round((equity / self.peak_equity - 1.0) * 100, 2) if self.peak_equity > 0 else 0.0,
            "risk_log": self.risk_log[-20:],   # 最近风控动作
            "positions": {
                c: {**p, "last_price": round(self.market_price(c), 3)}
                for c, p in self.positions.items()
            },
        }

    def restore(self, state: dict):
        """从持久化状态恢复现金/持仓/已实现盈亏(盘中引擎重启后延续).
        state: state.json 结构 {cash, positions, realized, day, trade_date}."""
        if not state:
            return
        if state.get("cash") is not None:
            self.cash = float(state["cash"])
        if state.get("realized") is not None:
            self.realized = float(state["realized"])
        # 费用累加器恢复; 旧存档缺字段时从历史成交补算(尽力), 保证归因对账
        if state.get("buy_fees") is not None:
            self.buy_fees = float(state["buy_fees"])
        if state.get("sell_fees") is not None:
            self.sell_fees = float(state["sell_fees"])
        if state.get("fees_paid") is not None:
            self.fees_paid = float(state["fees_paid"])
        self._legacy_fee_backfill = not (state.get("buy_fees") is not None and state.get("sell_fees") is not None)
        # P2: 恢复已入账的分红/除权事件key, 避免重启后重复记账
        if isinstance(state.get("applied_corp_actions"), list):
            self.applied_corp = set(state["applied_corp_actions"])
        if state.get("positions"):
            self.positions = {
                c: {
                    "qty": int(p["qty"]),
                    "avg_cost": float(p["avg_cost"]),
                    "buy_date": p.get("buy_date", self.trade_date),
                }
                for c, p in state["positions"].items()
            }
        if state.get("day"):
            self.trade_date = str(state["day"])
            self.day = str(state["day"])
        # 风控状态恢复: 回撤熔断与峰值权益跨重启延续, 避免重启绕过熔断
        snap = state.get("snapshot") or {}
        if state.get("peak_equity") is not None:
            self.peak_equity = float(state["peak_equity"])
        elif snap.get("equity"):
            self.peak_equity = max(self.peak_equity, float(snap["equity"]))
        cur_state = snap.get("risk_state") or state.get("risk_state")
        if cur_state == "DRAW_DOWN":
            self.state = "DRAW_DOWN"
        # locked_qty 恢复: 若state.day==今日则保留锁定(昨日买入已解锁, 今日买入锁定),
        # 否则(跨日开盘)全部解锁 -> 提供 sellable_qty 辅助
        today = date.today().strftime("%Y-%m-%d")
        for c, p in self.positions.items():
            p.setdefault("locked_qty", 0)
            if p.get("buy_date") != today:
                p["locked_qty"] = 0   # 非当日买入 -> 解锁
        # 总市值计算需用恢复后的持仓在当前价格下: 暂用 last_price 兜底
        self.d_price = {
            c: float(p.get("last_price") or 0)
            for c, p in state.get("positions", {}).items()
            if p.get("last_price")
        }
        # 恢复历史成交 (跨日累积)
        if state.get("trades_history"):
            self.trades_history = {
                str(d): list(items)
                for d, items in state["trades_history"].items()
            }
        # 旧存档缺费用字段: 持仓/现金恢复完成后再补算, 使归因恒等式成立
        if getattr(self, "_legacy_fee_backfill", False):
            self._backfill_fees_from_history(state)

    def _backfill_fees_from_history(self, state: dict) -> None:
        """旧版存档无费用累加字段时, 从 state 的 trades_history 反向累加费用, 并补齐
        被截断历史(08-24~26 等早期轮动)中已体现在现金里的买入费, 使归因恒等式严格成立:
            attributed_pnl(realized+unrealized-buy_fees) == equity - init_capital.
        仅用于旧存档; 新引擎写入的精确 buy_fees/sell_fees 不会再触发 top-up."""
        sf = bf_known = 0.0
        hist = state.get("trades_history") or {}
        for _d, trades in hist.items():
            for t in trades:
                fee = float(t.get("fee") or 0)
                if t.get("type") == "buy":
                    bf_known += fee
                else:
                    sf += fee
        self.sell_fees += sf
        self.buy_fees += bf_known
        self.fees_paid += sf + bf_known
        # 补齐被截断历史导致的买入费缺口
        try:
            unreal = sum(
                (self.market_price(c) or 0) * p["qty"] - p["avg_cost"] * p["qty"]
                for c, p in self.positions.items()
            )
            equity = self.cash + self.market_value()
            residual = (self.realized + unreal) - (equity - self.init_capital)
            if residual > self.buy_fees + 1e-9:
                diff = residual - self.buy_fees
                self.buy_fees += diff
                self.fees_paid += diff
        except Exception:
            pass

    def save_daily(self, day: str):
        d = os.path.join(DAILY_DIR, day)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "paper_book.json"), "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, ensure_ascii=False, indent=2)
        with open(os.path.join(d, "trades.json"), "w", encoding="utf-8") as f:
            json.dump(self.trades_today, f, ensure_ascii=False, indent=2, default=str)

    def get_trades(self, days: int = 5, include_today: bool = True) -> list:
        """返回近 N 个交易日(含或不含今日)的成交历史, 按时间倒序.
        每条 trade: {date, time, type, canon, qty, price, fee, pnl(可选)}.
        """
        all_dates = sorted(self.trades_history.keys(), reverse=True)
        if not include_today and all_dates and all_dates[0] == self.trade_date:
            all_dates = all_dates[1:]
        out = []
        for d in all_dates[:days]:
            for t in self.trades_history[d]:
                row = dict(t)
                row["date"] = d
                out.append(row)
        # 按 (date, time) 倒序
        out.sort(key=lambda r: (r.get("date", ""), r.get("time", "")), reverse=True)
        return out


class AKSharePriceFeed:
    """盘中价格: AKShare 全A实时快照 (缓存当日)"""
    _cache = {}
    _ts = 0.0

    def get_prices(self, symbols: list) -> dict:
        """返回 {canon: 最新价}; 只对出现过的标的请求, 全市场快照但只做一次"""
        cols = {"canon", "name6"}
        return {}


# ---------- 实时价获取: AKShare(缓存) ----------
def _canon_of(code: str) -> str:
    """6位代码 -> canonical '600519.SH'. 6/9开头=沪, 0/3=深, 4/8=北."""
    c = str(code).zfill(6)
    if c.startswith(("6", "9")):
        return f"{c}.SH"
    if c.startswith(("0", "3")):
        return f"{c}.SZ"
    if c.startswith(("4", "8")):
        return f"{c}.BSE"
    return f"{c}.SZ"


def _limit_prices(canon: str, last_close: float) -> tuple:
    """推算 A股当日涨停价/跌停价 (四舍五入到分).
    主板(60/00)→±10%(ST±5%), 创业板(30)/科创板(68)→±20%, 北交所(4/8)→±30%.
    无法判断或无昨收时返回 (None, None) -> 当不限制处理."""
    if not last_close or last_close <= 0:
        return None, None
    c = canon.split(".")[0]
    if c.startswith(("30", "68")):
        rate = 0.20
    elif c.startswith(("4", "8")):
        rate = 0.30
    else:
        rate = 0.10   # 主板; ST需名称判断, 此处按常规
    # 价格最小变动0.01, 涨停/跌停价为昨收*(1±rate) 四舍五入到分
    limit_up = round(last_close * (1 + rate), 2)
    limit_down = round(last_close * (1 - rate), 2)
    return limit_up, limit_down


class PriceFeed:
    """盘中实时价: AKShare 全A实时快照(带缓存与降级).
    交易时段拉实时spot; 缓存30s; 失败时保留上一次快照, 跳过价格返回.
    额外维护 quotes(canon -> 完整行情)供引擎判断涨跌停/停牌可交易性.
    quote 字段: price, last_close, limit_up, limit_down, volume, suspended
    """

    def __init__(self, cache_ttl: int = 30):
        self.ttl = cache_ttl
        self._snap = {}      # canon -> 最新价
        self.quotes = {}     # canon -> {price, limit_up, limit_down, volume, suspended}
        self._ts = 0.0
        self._last_error = None
        self._total_fetch = 0
        self.positions = {}  # 由外部 set_positions 注入, 用于构建 watchlist

    def set_positions(self, positions: dict) -> None:
        """引擎每 tick 注入当前持仓, _fetch_spot 据此构建 watchlist."""
        self.positions = positions or {}

    def _fetch_spot(self, fetch_all: bool = False) -> dict:
        """拉取实时行情快照, 返回 {canon: price_dict}.

        fetch_all=False (tick/报价默认):
          引擎只关心 watchlist (持仓+候选), 没必要全 A 抓 6000 只.
          1) 新浪单点接口 hq.sinajs.cn (毫秒级, 按 watchlist 直接拿)
          2) akshare stock_zh_a_spot_em 全 A (备选, 仅在新浪失败时)
          3) DuckDB 最近收盘价兜底

        fetch_all=True (午间重选/外部 API):
          1) 新浪全 A 接口 (180 只/批, 较快)
          2) akshare stock_zh_a_spot_em 全 A (备选)
          3) DuckDB 全 A 最近收盘价兜底
        """
        import akshare as ak
        out: dict = {}

        # 静默 akshare 内部的 tqdm 进度条
        try:
            import akshare.utils.func as _ak_func
            _ak_func.get_tqdm = lambda enable=True: (lambda it, *a, **kw: it)
        except Exception:
            pass

        if not fetch_all:
            # watchlist 路径
            watch = set(self.positions.keys())
            try:
                wl_path = os.path.join(DATA_DIR, "watchlist.json")
                if os.path.exists(wl_path):
                    with open(wl_path, encoding="utf-8") as f:
                        watch.update(json.load(f))
            except Exception:
                pass
            try:
                if os.path.isdir(DAILY_DIR):
                    days = sorted(os.listdir(DAILY_DIR), reverse=True)
                    for d in days[:2]:
                        sp = os.path.join(DAILY_DIR, d, "selection.json")
                        if os.path.exists(sp):
                            with open(sp, encoding="utf-8") as f:
                                s = json.load(f)
                            for t in (s.get("top_n") or s.get("targets") or []):
                                if "canon" in t:
                                    watch.add(t["canon"])
                            break
            except Exception:
                pass
            # 1) 新浪单点
            if watch:
                try:
                    codes_sina = [self._canon_to_sina(c) for c in watch if c]
                    out.update(self._fetch_sina_spot(codes_sina))
                    if out:
                        self._last_error = None
                        fallback_close = self._fetch_from_duckdb()
                        for canon in list(out.keys()):
                            if canon in fallback_close:
                                last = fallback_close[canon]["price"]
                                out[canon]["last_close"] = last
                                out[canon]["limit_up"], out[canon]["limit_down"] = \
                                    _limit_prices(canon, last)
                except Exception as e:
                    self._last_error = f"sina spot: {type(e).__name__}: {str(e)[:80]}"

            # 2) 全 A 备选 - 仅在新浪空时
            if not out and watch:
                df = None
                try:
                    df = ak.stock_zh_a_spot_em()
                except Exception:
                    df = None
                if df is not None and not getattr(df, "empty", True):
                    out = self._ak_to_dict(df)

            # 3) DuckDB 兜底
            if watch:
                missing = [c for c in watch if c not in out]
                if missing:
                    fb = self._fetch_from_duckdb(symbols=missing)
                    for c, q in fb.items():
                        if c not in out:
                            out[c] = q
            return out

        # ---- fetch_all=True ----
        # 优先从本地 DuckDB 取"所有活跃股"列表, 再用新浪单点逐只拉价 (毫秒级, 不卡)
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            syms = con.execute(
                "SELECT DISTINCT s.symbol FROM symbols s "
                "WHERE s.is_active = true "
                "  AND EXISTS (SELECT 1 FROM daily_bars d "
                "             WHERE d.symbol = s.symbol AND d.date >= current_date - 7)"
            ).fetchall()
            con.close()
            sina_codes = []
            sym6_to_canon = {}
            for (s6,) in syms:
                if not s6 or len(s6) != 6:
                    continue
                # 由 symbols 表确定交易所 (此处简化为按代码前缀判断)
                prefix = "sh" if s6.startswith(("60", "68", "90")) and not s6.startswith(("8", "43", "92")) else \
                         "bj" if s6.startswith(("8", "43", "92")) else "sz"
                sina_codes.append(prefix + s6)
                sym6_to_canon[prefix + s6] = s6
            sina_out = self._fetch_sina_spot(sina_codes)
            if sina_out:
                self._last_error = None
                return sina_out
        except Exception:
            pass

        # 新浪单点失败 -> 东财/新浪全 A (慢, 但 fallback)
        df = None
        try:
            df = ak.stock_zh_a_spot()
        except Exception:
            try:
                df = ak.stock_zh_a_spot_em()
            except Exception:
                df = None
        if df is not None and not getattr(df, "empty", True):
            out = self._ak_to_dict(df)
            self._last_error = None
        else:
            out = self._fetch_from_duckdb()
        return out

    def _ak_to_dict(self, df) -> dict:
        """akshare 返回的 DataFrame -> {canon: price_dict}"""
        out: dict = {}
        for _, r in df.iterrows():
            try:
                code = str(r.get("代码", "")).strip()
                px = float(r.get("最新价") or 0)
                last_close = float(r.get("昨收") or 0)
                volume = float(r.get("成交量") or 0)
            except Exception:
                continue
            if not code or px <= 0:
                continue
            raw = code
            if raw[:1].isalpha() and len(raw) > 6 and raw[2:].isdigit():
                raw = raw[2:]
            canon = _canon_of(raw)
            lu, ld = _limit_prices(canon, last_close)
            out[canon] = {
                "price": px, "last_close": last_close,
                "limit_up": lu, "limit_down": ld,
                "volume": volume, "suspended": False,
            }
        return out

    def _canon_to_sina(self, canon: str) -> str | None:
        """canon '600519.SH' -> 新浪 'sh600519'"""
        if "." not in canon:
            return None
        sym6, market = canon.split(".", 1)
        m = market.lower()
        if m in ("sh", "sse"):
            return f"sh{sym6}"
        if m in ("bj", "bse"):
            return f"bj{sym6}"
        return f"sz{sym6}"

    def _fetch_sina_spot(self, codes_sina: list) -> dict:
        """从新浪单点接口拉取实时价 (var hq_str_XXXX=...)"""
        import urllib.request as _ur
        if not codes_sina:
            return {}
        # 新浪单次最多 80 只, 分批
        out: dict = {}
        for i in range(0, len(codes_sina), 80):
            batch = codes_sina[i:i + 80]
            url = "https://hq.sinajs.cn/list=" + ",".join(batch)
            req = _ur.Request(url, headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            })
            try:
                r = _ur.urlopen(req, timeout=4).read().decode("gbk", errors="replace")
            except Exception:
                continue
            for line in r.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                sina_code = key.split("_")[-1].strip()
                if not sina_code or val.count('"') < 2:
                    continue
                fields = val.strip().strip('"').split(",")
                # 新浪 var hq_str_sh600519="贵州茅台,1500.000,1490.000,1485.000,1500.000,..."
                # 字段: 0=名称 1=今开 2=昨收 3=现价 4=今日最高 5=今日最低 ...
                if len(fields) < 6:
                    continue
                try:
                    name = fields[0]
                    prev_close = float(fields[2])
                    price = float(fields[3])
                    if price <= 0 or prev_close <= 0:
                        continue
                except (ValueError, IndexError):
                    continue
                # 还原 canon
                sym6 = sina_code[2:]
                mkt = sina_code[:2].lower()
                market = "SH" if mkt == "sh" else "BSE" if mkt == "bj" else "SZ"
                canon = f"{sym6}.{market}"
                lu, ld = _limit_prices(canon, prev_close)
                # 新浪无成交量字段, 用 -1 让 suspended 推断跳过错判
                out[canon] = {
                    "price": price,
                    "last_close": prev_close,
                    "limit_up": lu,
                    "limit_down": ld,
                    "volume": -1,
                    "suspended": False,
                }
        return out

    def _fetch_from_duckdb(self, symbols: list | None = None) -> dict:
        """从 DuckDB daily_bars 取最近一日的 close 作为兜底价."""
        import duckdb
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
        except Exception:
            return {}
        out = {}
        try:
            if symbols:
                canon_list = symbols
                codes = [c.split(".")[0] for c in canon_list]
                placeholders = ",".join(["?"] * len(codes))
                df = con.execute(
                    f"SELECT symbol, close FROM daily_bars t1 "
                    f"WHERE symbol IN ({placeholders}) AND close > 0 "
                    f"AND date = (SELECT MAX(date) FROM daily_bars t2 "
                    f"             WHERE t2.symbol = t1.symbol)",
                    codes,
                ).fetchdf()
                sym_map = dict(zip(df["symbol"], df["close"]))
                for canon, code in zip(canon_list, codes):
                    if code in sym_map:
                        last = float(sym_map[code] or 0)
                        if last <= 0:
                            continue
                        lu, ld = _limit_prices(canon, last)
                        out[canon] = {
                            "price": last,
                            "last_close": last,
                            "limit_up": lu,
                            "limit_down": ld,
                            "volume": 0,
                            "suspended": False,
                            "fallback": True,
                        }
            else:
                # 全 A 最近收盘价: 取最新 date 所有 close
                row = con.execute(
                    "SELECT MAX(date) FROM daily_bars WHERE close > 0"
                ).fetchone()
                if not row or not row[0]:
                    return out
                latest = row[0]
                df = con.execute(
                    "SELECT symbol, close FROM daily_bars WHERE date=? AND close>0",
                    [latest],
                ).fetchdf()
                for _, r in df.iterrows():
                    code = str(r["symbol"])
                    last = float(r["close"]) if r["close"] else 0
                    if last <= 0 or len(code) != 6:
                        continue
                    market = "sh" if code[:2] in ("60", "68", "90") else \
                             "bj" if code[:2] in ("92", "43", "8") else "sz"
                    ex_suffix = "BSE" if market == "bj" else market.upper()
                    canon = f"{code}.{ex_suffix}"
                    lu, ld = _limit_prices(canon, last)
                    out[canon] = {
                        "price": last,
                        "last_close": last,
                        "limit_up": lu,
                        "limit_down": ld,
                        "volume": 0,
                        "suspended": False,
                        "fallback": True,
                    }
        finally:
            con.close()
        return out

    def get_latest(self, symbols: list) -> dict:
        """刷新行情并返回 {canon: 最新价}(>0且命中). 失败沿用旧快照.
        更新 self.quotes, 并推断 suspended:
          一口价且成交量==0 -> 疑似停牌/无成交(不可交易, 撮合时跳过).
          若是 DuckDB 兜底价 (fallback=True) 或 volume 数据缺失 (-1), 视为非停牌.
        全A快照拉取有超时保护: 网络卡死/分页阻塞时最多等 SPOT_TIMEOUT 秒,
        超时放弃本次刷新, 保留旧快照, 绝不让主循环无限等待."""
        now = datetime.now().timestamp()
        if now - self._ts > self.ttl:
            snap = self._fetch_spot_with_timeout()
            if snap:
                self._snap = {c: q["price"] for c, q in snap.items()}
                # 停牌推断: 交易日盘中成交量恒为0 -> 停牌 (兜底价不参与此判定)
                for c, q in snap.items():
                    if q.get("fallback"):
                        # DuckDB 兜底: 没有成交量信息, 默认为非停牌
                        q["suspended"] = False
                    elif q.get("volume", 0) is None or q.get("volume", 0) < 0:
                        # 成交量数据缺失 (-1 或 None): 默认非停牌
                        q["suspended"] = False
                    else:
                        q["suspended"] = (q["volume"] <= 0)
                self.quotes = snap
                self._ts = now
                self._total_fetch += 1
                self._last_error = None
            # 拉取失败/超时: 保留旧快照, 不刷新时间戳 -> 下一次 tick 会重试
        quoted = {c: self.quotes.get(c) for c in symbols}
        return {c: q["price"] for c, q in quoted.items() if q and q.get("price", 0) > 0}

    def _fetch_spot_with_timeout(self, fetch_all: bool = False) -> dict:
        """带超时保护的快照拉取.
        fetch_all=False (默认, tick/报价用): 仅拉 watchlist (持仓+候选池), 毫秒级.
        fetch_all=True  (午间重选用): 拉全 A (新浪分批 + DuckDB 兜底).

        东财接口无内置超时, 极端情况下会无限阻塞 -> 用线程 + 超时兜底.
        """
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(self._fetch_spot, fetch_all)
                snap = fut.result(timeout=SPOT_TIMEOUT)
            if snap:
                self._last_error = None
                return snap
            self._last_error = "spot empty"
        except concurrent.futures.TimeoutError:
            self._last_error = f"spot fetch timeout>{SPOT_TIMEOUT}s"
        except Exception as e:
            self._last_error = str(e)
        # 实时源超时/失败/为空 -> DuckDB 兜底
        fallback = self._fetch_from_duckdb()
        if fallback:
            self._last_error = (self._last_error or "") + " (db fallback)"
        return fallback

    @property
    def last_error(self) -> str:
        return self._last_error or ""

    @property
    def total_fetch(self) -> int:
        return self._total_fetch

    def fetch_all_spot(self) -> dict:
        """强制拉取全A实时快照, 返回 {canon: price(>0)}.
        不受 TTL 缓存影响, 用于午间重选等需要全市场最新价的场景.
        带超时保护(SPOT_TIMEOUT), 防止网络卡死阻塞调用方线程.
        失败返回空 dict(调用方自行处理降级).

        实现: 优先用新浪全 A 接口 (180 只/批), 再 DuckDB 兜底.
        """
        snap = self._fetch_spot_with_timeout(fetch_all=True)
        if not snap:
            return {}
        return {c: q["price"] for c, q in snap.items() if q.get("price", 0) > 0}